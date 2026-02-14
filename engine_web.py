from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List
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

REQUEST_TTL_SECONDS = 60 * 60  # Legacy; no longer used for expiry logic.
GRACE_MINUTES = 30  # extra time after target_time before a request expires

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
DEFAULT_RESTAURANT_PATHS = [BASE_DIR / "data" / "restaurants.json", BASE_DIR / "restaurants.json"]

ACTIVE_WINDOW_MINUTES = 30


# ----------------- Waiting board MVP (no forced matches) -----------------
@dataclass
class Location:
    id: str
    name: str


@dataclass
class Restaurant:
    id: str
    name: str
    city: str
    address: Optional[str] = None
    rating: Optional[float] = None
    subsidy_price: Optional[float] = None
    meal_price: Optional[float] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    details_url: Optional[str] = None
    subtitle: Optional[str] = None
    location_id: Optional[str] = None  # kept for backward compatibility


@dataclass
class WaitingSlot:
    id: str
    restaurant_id: str
    created_at: datetime


@dataclass
class SlotMember:
    slot_id: str
    user_id: str  # instagram handle for MVP
    joined_at: datetime
    target_time: datetime  # UTC-aware selected go_time


# In-memory storage for MVP; simplest possible and mirrors existing style.
waiting_slots: Dict[str, WaitingSlot] = {}  # key: restaurant_id
slot_members: Dict[str, List[SlotMember]] = {}  # key: slot_id -> members


def _slugify(value: str) -> str:
    """Lightweight slugifier for IDs (keeps ASCII only)."""
    value = (value or "").strip().lower()
    cleaned = []
    prev_hyphen = False
    for ch in value:
        if ch.isalnum():
            cleaned.append(ch)
            prev_hyphen = False
        else:
            if not prev_hyphen:
                cleaned.append("-")
                prev_hyphen = True
    slug = "".join(cleaned).strip("-")
    return slug or "restaurant"


def _normalize_city(raw: str) -> str:
    return (raw or "").strip().lower()


def _extract_restaurant_id(item: dict) -> Optional[str]:
    """Prefer stable ID from details_url, fallback to name slug."""
    url = (item.get("details_url") or "").strip()
    if url:
        slug = _slugify(url.rstrip("/").split("/")[-1])
        if slug:
            return slug
    name = (item.get("name") or "").strip()
    if name:
        return _slugify(name)
    return None


def _load_restaurants_from_seed() -> List[Restaurant]:
    """Load restaurants from the new JSON dataset, scoped to Ljubljana."""
    for path in DEFAULT_RESTAURANT_PATHS:
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = json.load(fh)
            loaded: List[Restaurant] = []
            seen_ids: set[str] = set()
            if isinstance(raw, list):
                for item in raw:
                    name = (item.get("name") or "").strip()
                    city = (item.get("city") or "").strip()
                    if not (name and city):
                        continue
                    # Hidden default filter: only keep Ljubljana
                    if _normalize_city(city) != "ljubljana":
                        continue
                    rid = _extract_restaurant_id(item)
                    if not rid:
                        continue
                    base_rid = rid
                    # ensure unique ids even if names repeat
                    idx = 2
                    while rid in seen_ids:
                        rid = f"{base_rid}-{idx}"
                        idx += 1
                    seen_ids.add(rid)
                    address = (item.get("address") or "").strip() or None
                    subtitle = address
                    loaded.append(
                        Restaurant(
                            id=rid,
                            name=name,
                            city=city,
                            address=address,
                            rating=item.get("rating"),
                            subsidy_price=item.get("subsidy_price"),
                            meal_price=item.get("meal_price"),
                            latitude=item.get("latitude"),
                            longitude=item.get("longitude"),
                            details_url=(item.get("details_url") or "").strip() or None,
                            subtitle=subtitle,
                            location_id=_normalize_city(city) or None,
                        )
                    )
            if loaded:
                logger.info("restaurants_loaded count=%s path=%s", len(loaded), path)
                return loaded
            logger.warning("restaurants_file_empty path=%s", path)
        except Exception:
            logger.exception("restaurants_file_load_failed path=%s", path)
    logger.warning("restaurants_file_missing using empty list")
    return []


# Static locations for waiting board MVP (UI driven).
locations: List[Location] = [Location(id=k, name=v) for k, v in LOCATION_LABELS.items()]

# Loaded from curated seed JSON.
restaurants: List[Restaurant] = _load_restaurants_from_seed()


def list_locations() -> List[Location]:
    return locations


def list_restaurants(*, city: str | None = "ljubljana", search: str | None = None) -> List[Restaurant]:
    """Return restaurants filtered by city (default Ljubljana) and optional name search."""
    result = restaurants
    if city:
        city_norm = _normalize_city(city)
        result = [r for r in result if _normalize_city(r.city) == city_norm]
    if search:
        q = search.strip().lower()
        if q:
            result = [r for r in result if q in r.name.lower()]
    return result


def get_restaurant(restaurant_id: str) -> Optional[Restaurant]:
    rid = (restaurant_id or "").strip().lower()
    for r in restaurants:
        if r.id.lower() == rid:
            return r
    return None


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _window_end(member: SlotMember) -> datetime:
    return _to_utc(member.target_time) + timedelta(minutes=ACTIVE_WINDOW_MINUTES)


def _is_member_active_for_time(member: SlotMember, selected_time: datetime) -> bool:
    selected_utc = _to_utc(selected_time)
    start = _to_utc(member.target_time)
    return start <= selected_utc < _window_end(member)


def _normalize_members(handles: List[str]) -> List[str]:
    normalized: List[str] = []
    seen: set[str] = set()
    for handle in handles:
        s = (handle or "").strip().lstrip("@")
        if not s:
            continue
        if s in seen:
            continue
        seen.add(s)
        normalized.append(s)
    return normalized


def _get_or_create_slot(restaurant_id: str) -> WaitingSlot:
    key = (restaurant_id or "").strip().lower()
    slot = waiting_slots.get(key)
    if slot:
        return slot
    slot = WaitingSlot(
        id=uuid.uuid4().hex[:10],
        restaurant_id=key,
        created_at=datetime.now(timezone.utc),
    )
    waiting_slots[key] = slot
    return slot


def _get_members(slot_id: str) -> List[SlotMember]:
    return slot_members.get(slot_id, [])


def _find_member_slot(restaurant_id: str, user_norm: str) -> Optional[tuple[WaitingSlot, SlotMember]]:
    """Return current slot + member if user is already active for this restaurant."""
    rid = (restaurant_id or "").strip().lower()
    slot = waiting_slots.get(rid)
    if not slot:
        return None
    now = datetime.now(timezone.utc)
    for m in _get_members(slot.id):
        if _normalize_instagram(m.user_id) == user_norm and _window_end(m) > now:
            return slot, m
    return None


def cleanup_waiting_board(now: datetime | None = None) -> None:
    if now is None:
        now = datetime.now(timezone.utc)

    with match_lock:
        for slot_id in list(slot_members.keys()):
            members = slot_members.get(slot_id, [])
            fresh_members: List[SlotMember] = [m for m in members if _window_end(m) > now]
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


def join_slot(*, user_id: str, restaurant_id: str, target_time: datetime, referrer: str | None = None) -> dict:
    """Join or update waiting slot for exact go_time.

    referrer: optional instagram handle of inviter for attribution (not persisted yet, only logged).
    """
    if not target_time:
        return {"ok": False, "error": "invalid_go_time"}
    restaurant_id = (restaurant_id or "").strip().lower()
    user_norm = _normalize_instagram(user_id)
    target_time_utc = _to_utc(target_time)
    now = datetime.now(timezone.utc)
    floor_now = now.replace(second=0, microsecond=0)
    if target_time_utc + timedelta(minutes=ACTIVE_WINDOW_MINUTES) <= floor_now:
        return {"ok": False, "error": "go_time_in_past"}

    cleanup_waiting_board()

    with match_lock:
        existing = _find_member_slot(restaurant_id, user_norm)
        existing_target_iso = None
        if existing:
            slot_prev, member_prev = existing
            existing_target_iso = _to_utc(member_prev.target_time).isoformat()
            slot_members[slot_prev.id] = [m for m in _get_members(slot_prev.id) if m is not member_prev]

        slot = _get_or_create_slot(restaurant_id)
        members = slot_members.setdefault(slot.id, [])
        before_count = sum(1 for m in members if _is_member_active_for_time(m, target_time_utc))

        members.append(
            SlotMember(
                slot_id=slot.id,
                user_id=user_norm,
                joined_at=datetime.now(timezone.utc),
                target_time=target_time_utc,
            )
        )

    waiting_board_logger.info(
        "join_slot restaurant=%s target_time=%s user=%s moved_from=%s referrer=%s",
        restaurant_id,
        target_time_utc.isoformat(),
        user_norm,
        existing_target_iso,
        _normalize_instagram(referrer) if referrer else None,
    )
    return {"ok": True, "slot_id": slot.id, "moved": bool(existing), "previous_count": before_count}


def leave_slot(*, user_id: str, restaurant_id: str) -> dict:
    restaurant_id = (restaurant_id or "").strip().lower()
    user_norm = _normalize_instagram(user_id)

    cleanup_waiting_board()

    with match_lock:
        slot = waiting_slots.get(restaurant_id)
        if not slot:
            return {"ok": True, "slot_id": None}

        members = slot_members.get(slot.id, [])
        before = len(members)
        members = [m for m in members if _normalize_instagram(m.user_id) != user_norm]
        slot_members[slot.id] = members
    waiting_board_logger.info(
        "leave_slot restaurant=%s user=%s removed=%s",
        restaurant_id,
        user_norm,
        before != len(members),
    )
    return {"ok": True, "slot_id": slot.id}


def get_waiting_board(restaurant_id: str, selected_time: datetime | None = None) -> dict:
    """Return counts for selected window or grouped active windows."""
    restaurant_id = (restaurant_id or "").strip().lower()

    cleanup_waiting_board()

    with match_lock:
        slot = waiting_slots.get(restaurant_id)
        members = _get_members(slot.id) if slot else []
        if selected_time is not None:
            selected_utc = _to_utc(selected_time)
            filtered_members = [m for m in members if _is_member_active_for_time(m, selected_utc)]
            handles = _normalize_members([m.user_id for m in filtered_members])
            return {
                "count": len(handles),
                "members": handles,
                "target_time_iso": selected_utc.isoformat(),
                "window_end_iso": (selected_utc + timedelta(minutes=ACTIVE_WINDOW_MINUTES)).isoformat(),
            }

        now = datetime.now(timezone.utc)
        grouped: Dict[str, dict[str, Any]] = {}
        for m in members:
            start = _to_utc(m.target_time).replace(second=0, microsecond=0)
            if start + timedelta(minutes=ACTIVE_WINDOW_MINUTES) <= now:
                continue
            key = start.isoformat()
            group = grouped.setdefault(
                key,
                {
                    "target_time_iso": key,
                    "window_end_iso": (start + timedelta(minutes=ACTIVE_WINDOW_MINUTES)).isoformat(),
                    "members": [],
                },
            )
            group["members"].append(m.user_id)

        output: Dict[str, dict[str, Any]] = {}
        for key in sorted(grouped.keys()):
            handles = _normalize_members(grouped[key]["members"])
            output[key] = {
                "target_time_iso": grouped[key]["target_time_iso"],
                "window_end_iso": grouped[key]["window_end_iso"],
                "count": len(handles),
                "members": handles,
            }
        return output


def get_waiting_total(restaurant_id: str) -> int:
    board = get_waiting_board(restaurant_id)
    return sum(int(info.get("count") or 0) for info in board.values())


def get_total_waiting_all() -> int:
    total = 0
    for r in restaurants:
        total += get_waiting_total(r.id)
    return total


def get_waiting_count_all(selected_time: datetime) -> int:
    total = 0
    for r in restaurants:
        total += get_waiting_count(r.id, selected_time)
    return total


def get_waiting_members(restaurant_id: str, selected_time: datetime) -> List[str]:
    board = get_waiting_board(restaurant_id, selected_time=selected_time)
    return list(board.get("members") or [])


def get_waiting_count(restaurant_id: str, selected_time: datetime) -> int:
    board = get_waiting_board(restaurant_id, selected_time=selected_time)
    return int(board.get("count") or 0)


def get_top_active_restaurants(selected_time: datetime, limit: int = 5, city: str | None = "ljubljana", search: str | None = None) -> List[dict[str, Any]]:
    rows = []
    candidate_restaurants = list_restaurants(city=city, search=search)
    for r in candidate_restaurants:
        cnt = get_waiting_count(r.id, selected_time)
        if cnt <= 0:
            continue
        rows.append({"restaurant": r, "count": cnt, "members": get_waiting_members(r.id, selected_time)})
    rows.sort(key=lambda x: (-x["count"], x["restaurant"].name.lower()))
    return rows[:limit]


def get_top_active_restaurants_total(limit: int = 3) -> List[dict[str, Any]]:
    """Top restaurants by total currently active cards."""
    rows: List[dict[str, Any]] = []
    for r in restaurants:
        total = get_waiting_total(r.id)
        if total <= 0:
            continue
        rows.append({"restaurant": r, "total_waiting": total})
    rows.sort(key=lambda x: (-x["total_waiting"], x["restaurant"].name.lower()))
    return rows[:limit]


def get_user_bucket(restaurant_id: str, user_id: str) -> Optional[str]:
    """Backward-compatible helper; now returns go_time (HH:MM)."""
    restaurant_id = (restaurant_id or "").strip().lower()
    user_norm = _normalize_instagram(user_id)
    found = _find_member_slot(restaurant_id, user_norm)
    if not found:
        return None
    return _to_utc(found[1].target_time).strftime("%H:%M")


def get_user_membership(user_id: str) -> Optional[dict[str, Any]]:
    """Return active restaurant + target time for this user."""
    user_norm = _normalize_instagram(user_id)
    if not user_norm:
        return None

    cleanup_waiting_board()
    now = datetime.now(timezone.utc)

    with match_lock:
        slot_lookup: dict[str, str] = {slot.id: rid for rid, slot in waiting_slots.items()}
        for slot_id, members in slot_members.items():
            for m in members:
                if _normalize_instagram(m.user_id) != user_norm:
                    continue
                if _window_end(m) <= now:
                    continue
                rid = slot_lookup.get(slot_id)
                if rid:
                    target = _to_utc(m.target_time)
                    return {
                        "restaurant_id": rid,
                        "target_time": target,
                        "target_time_iso": target.isoformat(),
                        "go_time": target.strftime("%H:%M"),
                        "window_end_iso": (target + timedelta(minutes=ACTIVE_WINDOW_MINUTES)).isoformat(),
                    }
    return None


def get_waiting_summary_for_location(location_id: str) -> List[dict[str, Any]]:
    """Return list of restaurants in location with aggregated counts."""
    loc_norm = _normalize_location(location_id)
    result: List[dict[str, Any]] = []
    for r in list_restaurants(city=loc_norm):
        board = get_waiting_board(r.id)
        total = sum((int(info.get("count") or 0) for info in board.values()))
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
    target_time: datetime
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


def _expires_at(req: "Request") -> datetime:
    """Return absolute expiry timestamp for a request (target_time + grace)."""
    return req.target_time + timedelta(minutes=GRACE_MINUTES)


def _is_request_expired(req: "Request", now: datetime) -> bool:
    expires = _expires_at(req)
    return now > expires or not getattr(req, "active", True)


def cleanup_expired() -> None:
    """Expire requests whose target window + grace has passed."""
    now = datetime.now()
    for rid in list(waiting):
        req = requests.get(rid)
        if not req:
            _remove_from_waiting(rid)
            continue
        if _is_request_expired(req, now):
            req.active = False
            _remove_from_waiting(rid)
            match_logger.info("request_expired rid=%s instagram=%s", rid, req.instagram)
    _cleanup()


def _close_in_time(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) <= WINDOW_MIN * 60


def _cleanup() -> None:
    now = datetime.now()
    to_delete = [rid for rid, r in requests.items() if _is_request_expired(r, now)]
    for rid in to_delete:
        requests.pop(rid, None)
    # počisti waiting
    alive = [
        rid
        for rid in waiting
        if rid in requests and requests[rid].active and not _is_request_expired(requests[rid], now)
    ]
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
        target_time=when,
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
    now = datetime.now()
    if rid in paired:
        return {"status": "matched", "rid": rid, **paired[rid]}
    if rid not in requests:
        return {"status": "expired"}
    if not requests[rid].active or _is_request_expired(requests[rid], now):
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
        target_time=when,
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
