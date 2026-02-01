from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
from pathlib import Path
from pathlib import Path
import uuid
import os
import json
import urllib.request
import logging
import threading

# --- Web Push (VAPID) ---
# Configure via Railway env vars:
#   VAPID_PRIVATE_KEY
#   VAPID_SUBJECT (e.g. "mailto:hello@bonibuddy.app" or "https://bonibuddy.app")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "").strip()

logger = logging.getLogger("bonibuddy.push")
match_logger = logging.getLogger("bonibuddy.match")
waiting_board_logger = logging.getLogger("bonibuddy.waiting_board")

# rid -> PushSubscription JSON (as received from the browser)
PUSH_SUBSCRIPTIONS: Dict[str, Dict[str, Any]] = {}

REQUEST_TTL_SECONDS = 60 * 60  # 60 minutes

# Serializes access to the waiting pool + pairing to avoid double matches.
match_lock = threading.Lock()


def set_push_subscription(rid: str, subscription: Dict[str, Any]) -> None:
    """Store/replace a push subscription for this rid (MVP: in-memory)."""
    if not rid:
        return
    if not isinstance(subscription, dict) or not subscription.get("endpoint"):
        return
    logger.info("push_subscribed rid=%s", rid)
    PUSH_SUBSCRIPTIONS[rid] = subscription


def send_push_to_rid(rid: str, payload: Dict[str, Any]) -> bool:
    """Best-effort send a web push notification to the stored subscription for rid."""
    sub = PUSH_SUBSCRIPTIONS.get(rid)
    if not sub:
        logger.info("push_skip no_subscription rid=%s", rid)
        return False
    if not (VAPID_PRIVATE_KEY and VAPID_SUBJECT):
        logger.info("push_skip missing_vapid rid=%s", rid)
        return False

    try:
        from pywebpush import webpush  # type: ignore
    except Exception:
        logger.exception("push_skip pywebpush_import_failed rid=%s", rid)
        return False

    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
            timeout=10,
        )
        logger.info("push_sent rid=%s", rid)
        return True
    except TypeError:
        # Fallback: send a payload-less push (no encryption step) so users still
        # get notified even if the library stack is mismatched.
        try:
            webpush(
                subscription_info=sub,
                data=None,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
                timeout=10,
            )
            logger.info("push_sent_no_payload rid=%s", rid)
            return True
        except Exception:
            logger.exception("push_failed rid=%s", rid)
            return False
    except Exception:
        # Never break matching flow.
        logger.exception("push_failed rid=%s", rid)
        return False

WINDOW_MIN = 15
EXPIRE_MIN = 90  # po koliko min request poteče
WAITING_MEMBER_TTL_MIN = 90  # minutes a waiting-board member stays alive

# Waiting board locations (keep in sync with app UI)
LOCATION_LABELS = {
    "center": "Center",
    "kardeljeva": "Kardeljeva",
    "rozna": "Rožna",
    "mestni_log": "Mestni log",
    "siska": "Šiška",
    "vic": "Vič",
}

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_SEED_PATHS = [BASE_DIR / "data" / "restaurants_seed.json", BASE_DIR / "restaurants_seed.json"]

ALLOWED_TIME_BUCKETS = {"now", "30", "60"}


# ----------------- Waiting board MVP (no forced matches) -----------------
@dataclass
class Location:
    id: str
    name: str


@dataclass
class Restaurant:
    id: str
    name: str
    location_id: str
    area_id: str
    subtitle: Optional[str] = None


@dataclass
class WaitingSlot:
    id: str
    restaurant_id: str
    time_bucket: str  # "now" | "30" | "60"
    created_at: datetime


@dataclass
class SlotMember:
    slot_id: str
    user_id: str  # instagram handle for MVP
    joined_at: datetime


# In-memory storage for MVP; simplest possible and mirrors existing style.
waiting_slots: Dict[tuple[str, str], WaitingSlot] = {}  # key: (restaurant_id, time_bucket)
slot_members: Dict[str, List[SlotMember]] = {}  # key: slot_id -> members

def _load_restaurants_from_seed() -> List[Restaurant]:
    """Load curated restaurants from the JSON seed file."""
    for path in DEFAULT_SEED_PATHS:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            loaded: List[Restaurant] = []
            if isinstance(raw, list):
                for item in raw:
                    rid = (item.get("id") or "").strip().lower()
                    name = (item.get("name") or "").strip()
                    area_id = (item.get("area_id") or "").strip().lower()
                    if not (rid and name and area_id):
                        continue
                    location_id = (item.get("location_id") or area_id).strip().lower()
                    subtitle = (item.get("subtitle") or None) or None
                    loaded.append(
                        Restaurant(
                            id=rid,
                            name=name,
                            location_id=location_id,
                            area_id=area_id,
                            subtitle=subtitle,
                        )
                    )
            if loaded:
                logger.info("restaurants_loaded count=%s path=%s", len(loaded), path)
                return loaded
            logger.warning("restaurants_seed_empty path=%s", path)
        except Exception:
            logger.exception("restaurants_seed_load_failed path=%s", path)
    logger.warning("restaurants_seed_missing using empty list")
    return []


# Static locations for waiting board MVP (UI driven).
locations: List[Location] = [Location(id=k, name=v) for k, v in LOCATION_LABELS.items()]

# Loaded from curated seed JSON.
restaurants: List[Restaurant] = _load_restaurants_from_seed()


def list_locations() -> List[Location]:
    return locations


def list_restaurants(location_id: str | None = None, area_id: str | None = None) -> List[Restaurant]:
    result = restaurants
    if location_id:
        loc_norm = _normalize_location(location_id)
        result = [r for r in result if _normalize_location(r.location_id) == loc_norm]
    if area_id:
        area_norm = _normalize_location(area_id)
        result = [r for r in result if _normalize_location(r.area_id) == area_norm]
    return result


def get_restaurant(restaurant_id: str) -> Optional[Restaurant]:
    rid = (restaurant_id or "").strip().lower()
    for r in restaurants:
        if r.id.lower() == rid:
            return r
    return None


def _get_or_create_slot(restaurant_id: str, time_bucket: str) -> WaitingSlot:
    key = (restaurant_id, time_bucket)
    slot = waiting_slots.get(key)
    if slot:
        return slot
    slot = WaitingSlot(
        id=uuid.uuid4().hex[:10],
        restaurant_id=restaurant_id,
        time_bucket=time_bucket,
        created_at=datetime.now(timezone.utc),
    )
    waiting_slots[key] = slot
    return slot


def _get_members(slot_id: str) -> List[SlotMember]:
    return slot_members.get(slot_id, [])


def _find_member_slot(restaurant_id: str, user_norm: str) -> Optional[tuple[WaitingSlot, SlotMember]]:
    """Return current slot + member if user is already in any bucket for the restaurant."""
    for tb in ["now", "30", "60"]:
        slot = waiting_slots.get((restaurant_id, tb))
        if not slot:
            continue
        for m in _get_members(slot.id):
            if _normalize_instagram(m.user_id) == user_norm:
                return slot, m
    return None


def cleanup_waiting_board(now: datetime | None = None) -> None:
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=WAITING_MEMBER_TTL_MIN)

    with match_lock:
        for slot_id in list(slot_members.keys()):
            members = slot_members.get(slot_id, [])
            fresh_members: List[SlotMember] = []
            for m in members:
                joined_at = m.joined_at
                if joined_at.tzinfo is None:
                    joined_at = joined_at.replace(tzinfo=timezone.utc)
                if joined_at >= cutoff:
                    fresh_members.append(m)
            if fresh_members:
                slot_members[slot_id] = fresh_members
            else:
                slot_members.pop(slot_id, None)

        for key in list(waiting_slots.keys()):
            slot = waiting_slots.get(key)
            if not slot:
                continue
            members = slot_members.get(slot.id)
            if not members:
                waiting_slots.pop(key, None)


def join_slot(*, user_id: str, restaurant_id: str, time_bucket: str) -> dict:
    """Join (or move) a waiting slot; idempotent; auto-move across buckets."""
    if time_bucket not in ALLOWED_TIME_BUCKETS:
        return {"ok": False, "error": "invalid_time_bucket"}
    restaurant_id = (restaurant_id or "").strip().lower()
    user_norm = _normalize_instagram(user_id)

    cleanup_waiting_board()

    with match_lock:
        # Remove from other bucket if present
        existing = _find_member_slot(restaurant_id, user_norm)
        if existing:
            slot_prev, member_prev = existing
            slot_members[slot_prev.id] = [m for m in _get_members(slot_prev.id) if m is not member_prev]

        slot = _get_or_create_slot(restaurant_id, time_bucket)
        members = slot_members.setdefault(slot.id, [])

        for m in members:
            if _normalize_instagram(m.user_id) == user_norm:
                return {"ok": True, "slot_id": slot.id, "already": True, "moved": bool(existing)}

        members.append(SlotMember(slot_id=slot.id, user_id=user_norm, joined_at=datetime.now(timezone.utc)))
    waiting_board_logger.info(
        "join_slot restaurant=%s time_bucket=%s user=%s moved_from=%s",
        restaurant_id,
        time_bucket,
        user_norm,
        existing[0].time_bucket if existing else None,
    )
    return {"ok": True, "slot_id": slot.id, "moved": bool(existing)}


def leave_slot(*, user_id: str, restaurant_id: str, time_bucket: str) -> dict:
    if time_bucket not in ALLOWED_TIME_BUCKETS:
        return {"ok": False, "error": "invalid_time_bucket"}
    restaurant_id = (restaurant_id or "").strip().lower()
    key = (restaurant_id, time_bucket)
    user_norm = _normalize_instagram(user_id)

    cleanup_waiting_board()

    with match_lock:
        slot = waiting_slots.get(key)
        if not slot:
            return {"ok": True, "slot_id": None}

        members = slot_members.get(slot.id, [])
        before = len(members)
        members = [m for m in members if _normalize_instagram(m.user_id) != user_norm]
        slot_members[slot.id] = members
    waiting_board_logger.info(
        "leave_slot restaurant=%s time_bucket=%s user=%s removed=%s",
        restaurant_id,
        time_bucket,
        user_norm,
        before != len(members),
    )
    return {"ok": True, "slot_id": slot.id}


def get_waiting_board(restaurant_id: str) -> dict:
    """Return per-time-bucket member counts and handles for a restaurant."""
    restaurant_id = (restaurant_id or "").strip().lower()

    cleanup_waiting_board()

    with match_lock:
        board = {}
        for tb in ["now", "30", "60"]:
            slot = _get_or_create_slot(restaurant_id, tb)
            members = _get_members(slot.id)
            board[tb] = {
                "slot_id": slot.id,
                "count": len(members),
                "members": [m.user_id for m in members],
            }
    return board


def get_waiting_total(restaurant_id: str) -> int:
    board = get_waiting_board(restaurant_id)
    return sum(board[tb]["count"] for tb in board)


def get_total_waiting_all() -> int:
    total = 0
    for r in restaurants:
        total += get_waiting_total(r.id)
    return total


def get_waiting_count_all(time_bucket: str) -> int:
    if time_bucket not in ALLOWED_TIME_BUCKETS:
        return 0
    total = 0
    for r in restaurants:
        total += get_waiting_count(r.id, time_bucket)
    return total


def get_waiting_members(restaurant_id: str, time_bucket: str) -> List[str]:
    if time_bucket not in ALLOWED_TIME_BUCKETS:
        return []
    board = get_waiting_board(restaurant_id)
    slot = board.get(time_bucket) or {}
    members = slot.get("members") or []
    # normalize to single '@' prefix when shown
    normalized = []
    for m in members:
        s = (m or "").strip()
        if s.startswith("@"):
            s = s.lstrip("@")
        normalized.append(s)
    return normalized


def get_waiting_count(restaurant_id: str, time_bucket: str) -> int:
    if time_bucket not in ALLOWED_TIME_BUCKETS:
        return 0
    board = get_waiting_board(restaurant_id)
    slot = board.get(time_bucket) or {}
    return int(slot.get("count") or 0)


def get_top_active_restaurants(time_bucket: str, limit: int = 5, area_id: str | None = None) -> List[dict[str, Any]]:
    if time_bucket not in ALLOWED_TIME_BUCKETS:
        return []
    rows = []
    candidate_restaurants = list_restaurants(area_id=area_id) if area_id else restaurants
    for r in candidate_restaurants:
        cnt = get_waiting_count(r.id, time_bucket)
        if cnt <= 0:
            continue
        rows.append({"restaurant": r, "count": cnt, "members": get_waiting_members(r.id, time_bucket)})
    rows.sort(key=lambda x: (-x["count"], x["restaurant"].name.lower()))
    return rows[:limit]


# Top restaurants by total waiting across all buckets (now+30+60)
def get_top_active_restaurants_total(limit: int = 3) -> List[dict[str, Any]]:
    """Top restaurants by total waiting across all buckets (now+30+60)."""
    rows: List[dict[str, Any]] = []
    for r in restaurants:
        total = get_waiting_total(r.id)
        if total <= 0:
            continue
        rows.append({"restaurant": r, "total_waiting": total})
    rows.sort(key=lambda x: (-x["total_waiting"], x["restaurant"].name.lower()))
    return rows[:limit]


def get_user_bucket(restaurant_id: str, user_id: str) -> Optional[str]:
    """Return the bucket (now/30/60) where user is present for this restaurant."""
    restaurant_id = (restaurant_id or "").strip().lower()
    user_norm = _normalize_instagram(user_id)
    found = _find_member_slot(restaurant_id, user_norm)
    return found[0].time_bucket if found else None


def get_waiting_summary_for_location(location_id: str) -> List[dict[str, Any]]:
    """Return list of restaurants in location with aggregated counts."""
    loc_norm = _normalize_location(location_id)
    result: List[dict[str, Any]] = []
    for r in list_restaurants(loc_norm):
        board = get_waiting_board(r.id)
        total = sum((board[tb]["count"] for tb in board))
        result.append(
            {
                "restaurant": r,
                "board": board,
                "total_waiting": total,
            }
        )
    return result

# --- GA4 (Measurement Protocol) ---
# Nastavi v Railway env vars:
#   GA4_MEASUREMENT_ID=G-XXXXXXXXXX
#   GA4_API_SECRET=xxxxxxxxxxxxxxxx
GA4_MEASUREMENT_ID = os.getenv("GA4_MEASUREMENT_ID", "").strip()
GA4_API_SECRET = os.getenv("GA4_API_SECRET", "").strip()

def _ga4_send_event(event_name: str, params: Dict[str, Any]) -> None:
    """Best-effort pošlji dogodek v GA4 prek Measurement Protocol.

    - Ne pošiljamo PII (kontakti ipd.).
    - Če ni konfiguracije ali če pride do napake, tiho ignoriramo.
    """

    if not GA4_MEASUREMENT_ID or not GA4_API_SECRET:
        return

    try:
        url = (
            "https://www.google-analytics.com/mp/collect"
            f"?measurement_id={GA4_MEASUREMENT_ID}&api_secret={GA4_API_SECRET}"
        )

        payload = {
            # client_id mora biti non-PII; uporabimo naključen UUID na dogodek
            "client_id": uuid.uuid4().hex,
            "events": [
                {
                    "name": event_name,
                    "params": {
                        **params,
                        "engagement_time_msec": 1,
                    },
                }
            ],
        }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=2) as _:
            pass
    except Exception:
        # analytics je best-effort; nikoli ne sme podreti matchanja
        return

@dataclass
class Request:
    rid: str
    created_at: datetime
    location: str
    when: datetime
    time_bucket: str  # "soon" | "today"
    instagram: str  # Instagram handle (brez @)
    city: str  # "ljubljana" | "maribor"
    gender: str  # "female" | "male"
    match_pref: str  # "any" | "female" | "male"
    active: bool = True
# Helper for mutual match preference
def _mutual_pref(a: Request, b: Request) -> bool:
    """Return True if both users' preferences are satisfied."""

    def wants(x: Request, y: Request) -> bool:
        return x.match_pref == "any" or x.match_pref == y.gender

    return wants(a, b) and wants(b, a)

# rid -> Request
requests: Dict[str, Request] = {}

# waiting list of rid
waiting: List[str] = []


def _normalize_instagram(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("@"):
        s = s[1:]
    return s.lower()


def _normalize_location(raw: str) -> str:
    """Keep location keys consistent across requests."""
    return (raw or "").strip().lower()


def _remove_from_waiting(rid: str) -> None:
    try:
        waiting.remove(rid)
    except ValueError:
        pass


def _invalidate_waiting_by_instagram(instagram: str) -> None:
    for other_rid in list(waiting):
        other = requests.get(other_rid)
        if not other:
            _remove_from_waiting(other_rid)
            continue
        other_instagram = _normalize_instagram(other.instagram)
        other.instagram = other_instagram
        if other_instagram == instagram and other.active:
            other.active = False
            _remove_from_waiting(other_rid)
            match_logger.info("request_invalidated rid=%s instagram=%s", other_rid, instagram)


def cleanup_expired() -> None:
    """Expire waiting requests older than REQUEST_TTL_SECONDS."""
    now = datetime.now()
    cutoff = now - timedelta(seconds=REQUEST_TTL_SECONDS)
    for rid in list(waiting):
        req = requests.get(rid)
        if not req:
            _remove_from_waiting(rid)
            continue
        if not getattr(req, "active", True):
            _remove_from_waiting(rid)
            continue
        if req.created_at < cutoff:
            req.active = False
            _remove_from_waiting(rid)
            match_logger.info("request_expired rid=%s instagram=%s", rid, req.instagram)
    _cleanup()


def _close_in_time(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) <= WINDOW_MIN * 60


def _cleanup() -> None:
    now = datetime.now()
    cutoff = now - timedelta(minutes=EXPIRE_MIN)
    to_delete = [rid for rid, r in requests.items() if r.created_at < cutoff]
    for rid in to_delete:
        requests.pop(rid, None)
    # počisti waiting
    alive = [rid for rid in waiting if rid in requests and requests[rid].active]
    waiting.clear()
    waiting.extend(alive)
    # počisti paired (da ne ostanejo "ghost" matchi)
    for rid in list(paired.keys()):
        if rid not in requests:
            paired.pop(rid, None)


def add_request(*, location: str, when: datetime, instagram: str) -> Dict[str, Any]:
    """Dodaj request. Vrne:
    - {"status":"waiting","rid":...}
    - {"status":"matched","rid":..., "other_instagram":..., "location":..., "when":...}
    """
    cleanup_expired()

    instagram_norm = _normalize_instagram(instagram)
    location_norm = _normalize_location(location)

    rid = uuid.uuid4().hex[:10]
    req = Request(
        rid=rid,
        created_at=datetime.now(),
        location=location_norm,
        when=when,
        time_bucket="soon",
        instagram=instagram_norm,
        city="ljubljana",
        gender="male",
        match_pref="any",
    )
    requests[rid] = req

    match_logger.info("request_new rid=%s instagram=%s location=%s", rid, instagram_norm, location_norm)

    with match_lock:
        cleanup_expired()
        _invalidate_waiting_by_instagram(instagram_norm)

        # najdi match med waiting
        for other_rid in list(waiting):
            other = requests.get(other_rid)
            if not other or not other.active:
                _remove_from_waiting(other_rid)
                continue
            other_instagram = _normalize_instagram(other.instagram)
            other.instagram = other_instagram
            if (
                other.location == location_norm
                and other.city == req.city
                and other.time_bucket == req.time_bucket
                and other_instagram != instagram_norm
                and _mutual_pref(req, other)
            ):
                _remove_from_waiting(other_rid)
                if not (req.active and other.active):
                    continue
                req.active = False
                other.active = False
                paired[rid] = {
                    "other_instagram": other.instagram,
                    "city": req.city,
                    "location": location_norm,
                    "when": when,
                    "time_bucket": req.time_bucket,
                }
                paired[other_rid] = {
                    "other_instagram": instagram_norm,
                    "city": req.city,
                    "location": location_norm,
                    "when": when,
                    "time_bucket": req.time_bucket,
                }
                match_logger.info("match_created rid=%s other_rid=%s location=%s", rid, other_rid, location_norm)
                _ga4_send_event(
                    "match_found",
                    {"city": req.city, "location": location_norm, "time_bucket": req.time_bucket},
                )
                return {
                    "status": "matched",
                    "rid": rid,
                    "other_instagram": other.instagram,
                    "location": location_norm,
                    "when": when,
                    "other_rid": other_rid,
                }

        # ni matcha → v čakalnico
        if req.active:
            waiting.append(rid)
    return {"status": "waiting", "rid": rid, "location": location_norm, "when": when}


def check_status(rid: str) -> Dict[str, Any]:
    """Preveri ali je rid še v čakanju ali je matchan."""
    cleanup_expired()
    if rid in paired:
        return {"status": "matched", "rid": rid, **paired[rid]}
    if rid not in requests:
        return {"status": "expired"}
    if not requests[rid].active:
        return {"status": "expired"}

    # če ni v waiting, pomeni: ali je matchan ali je bil odstranjen
    # za MVP: status matched hranimo na frontu prek 'match token' – poenostavimo:
    # Če je v waiting → waiting, sicer rečemo 'still_waiting_or_matched_unknown'
    if rid in waiting:
        r = requests[rid]
        return {"status": "waiting", "location": r.location, "when": r.when}

    # Če ni v waiting, še ne vemo kdo je match (ker se match vrne ob add_request).
    # Zato v MVP načinu: po add_request takoj dobiš matched; waiting page samo osvežuje,
    # ko se pojavi match za nekoga drugega, ne zna sama ugotoviti.
    # Rešitev: shranimo "pair" mapo. (glej spodaj izboljšavo)
    return {"status": "unknown"}


# --- Minimal pairing storage (da waiting page lahko ugotovi match) ---
# rid -> other_instagram
paired: Dict[str, Dict[str, Any]] = {}

def add_request_with_pairs(
    *, city: str, location: str, when: datetime, time_bucket: str = "soon", instagram: str, gender: str, match_pref: str
) -> Dict[str, Any]:
    cleanup_expired()

    instagram_norm = _normalize_instagram(instagram)
    location_norm = _normalize_location(location)

    rid = uuid.uuid4().hex[:10]
    req = Request(
        rid=rid,
        created_at=datetime.now(),
        location=location_norm,
        when=when,
        time_bucket=time_bucket,
        instagram=instagram_norm,
        city=city,
        gender=gender,
        match_pref=match_pref,
    )
    requests[rid] = req
    match_logger.info(
        "request_new rid=%s instagram=%s city=%s location=%s time_bucket=%s",
        rid,
        instagram_norm,
        city,
        location_norm,
        time_bucket,
    )

    push_targets: Optional[List[str]] = None
    match_data: Optional[Dict[str, Any]] = None

    with match_lock:
        cleanup_expired()
        _invalidate_waiting_by_instagram(instagram_norm)

        for other_rid in list(waiting):
            other = requests.get(other_rid)
            if not other or not other.active:
                _remove_from_waiting(other_rid)
                continue
            other_instagram = _normalize_instagram(other.instagram)
            other.instagram = other_instagram
            if (
                other.location == location_norm
                and other.city == req.city
                and other.time_bucket == req.time_bucket
                and other_instagram != instagram_norm
                and _mutual_pref(req, other)
            ):
                _remove_from_waiting(other_rid)
                if not (req.active and other.active):
                    continue
                req.active = False
                other.active = False
                paired[rid] = {
                    "other_instagram": other.instagram,
                    "city": city,
                    "location": location_norm,
                    "when": when,
                    "time_bucket": req.time_bucket,
                }
                paired[other_rid] = {
                    "other_instagram": instagram_norm,
                    "city": city,
                    "location": location_norm,
                    "when": when,
                    "time_bucket": req.time_bucket,
                }
                match_logger.info("match_created rid=%s other_rid=%s location=%s", rid, other_rid, location_norm)
                match_data = {"status": "matched", "rid": rid, **paired[rid]}
                push_targets = [rid, other_rid]
                break

        if match_data is None and req.active:
            waiting.append(rid)
            match_data = {
                "status": "waiting",
                "rid": rid,
                "city": city,
                "location": location_norm,
                "when": when,
                "time_bucket": req.time_bucket,
            }

    if push_targets:
        # Best-effort web push (no PII in payload)
        payload = {
            "title": "BoniBuddy",
            "body": "Našli smo družbo! Odpri app in klikni za WhatsApp.",
            "url": "/",
        }
        logger.info("match_push_attempt rid=%s other_rid=%s", push_targets[0], push_targets[1])
        send_push_to_rid(push_targets[0], payload)
        send_push_to_rid(push_targets[1], payload)
        _ga4_send_event(
            "match_found",
            {
                "city": city,
                "location": location_norm,
                "time_bucket": req.time_bucket,
            },
        )

    return match_data

def check_status_with_pairs(rid: str) -> Dict[str, Any]:
    cleanup_expired()
    if rid in paired:
        return {"status": "matched", "rid": rid, **paired[rid]}
    r = requests.get(rid)
    if not r or not r.active:
        return {"status": "expired"}
    return {
        "status": "waiting",
        "rid": rid,
        "city": r.city,
        "location": r.location,
        "when": r.when,
        "time_bucket": r.time_bucket,
    }
