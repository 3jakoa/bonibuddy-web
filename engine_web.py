from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import uuid
import os
import json
import urllib.request
import logging

# --- Web Push (VAPID) ---
# Configure via Railway env vars:
#   VAPID_PRIVATE_KEY
#   VAPID_SUBJECT (e.g. "mailto:hello@bonibuddy.app" or "https://bonibuddy.app")
VAPID_PRIVATE_KEY = os.getenv("VAPID_PRIVATE_KEY", "").strip()
VAPID_SUBJECT = os.getenv("VAPID_SUBJECT", "").strip()

logger = logging.getLogger("bonibuddy.push")

# rid -> PushSubscription JSON (as received from the browser)
PUSH_SUBSCRIPTIONS: Dict[str, Dict[str, Any]] = {}


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

# --- GA4 (Measurement Protocol) ---
# Nastavi v Railway env vars:
#   GA4_MEASUREMENT_ID=G-XXXXXXXXXX
#   GA4_API_SECRET=xxxxxxxxxxxxxxxx
GA4_MEASUREMENT_ID = os.getenv("GA4_MEASUREMENT_ID", "").strip()
GA4_API_SECRET = os.getenv("GA4_API_SECRET", "").strip()

def _ga4_send_event(event_name: str, params: Dict[str, Any]) -> None:
    """Best-effort pošlji dogodek v GA4 prek Measurement Protocol.

    - Ne pošiljamo PII (npr. phone številk).
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
    phone: str  # WhatsApp phone in E164-ish (npr 38640111222)
    city: str  # "ljubljana" | "maribor"
    gender: str  # "female" | "male"
    match_pref: str  # "any" | "female" | "male"
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


def _close_in_time(a: datetime, b: datetime) -> bool:
    return abs((a - b).total_seconds()) <= WINDOW_MIN * 60


def _cleanup() -> None:
    now = datetime.now()
    cutoff = now - timedelta(minutes=EXPIRE_MIN)
    to_delete = [rid for rid, r in requests.items() if r.created_at < cutoff]
    for rid in to_delete:
        requests.pop(rid, None)
    # počisti waiting
    alive = [rid for rid in waiting if rid in requests]
    waiting.clear()
    waiting.extend(alive)
    # počisti paired (da ne ostanejo "ghost" matchi)
    for rid in list(paired.keys()):
        if rid not in requests:
            paired.pop(rid, None)


def add_request(*, location: str, when: datetime, phone: str) -> Dict[str, Any]:
    """Dodaj request. Vrne:
    - {"status":"waiting","rid":...}
    - {"status":"matched","rid":..., "other_phone":..., "location":..., "when":...}
    """
    _cleanup()

    rid = uuid.uuid4().hex[:10]
    req = Request(
        rid=rid,
        created_at=datetime.now(),
        location=location,
        when=when,
        time_bucket="soon",
        phone=phone,
        city="ljubljana",
        gender="male",
        match_pref="any",
    )
    requests[rid] = req

    # najdi match med waiting
    for other_rid in list(waiting):
        other = requests.get(other_rid)
        if not other:
            continue
        if (
            other.location == location
            and other.city == req.city
            and other.time_bucket == req.time_bucket
            and other.phone != phone
            and _mutual_pref(req, other)
        ):
            # match found: odstrani other iz waiting, tudi novega ne dodajamo
            try:
                waiting.remove(other_rid)
            except ValueError:
                pass
            _ga4_send_event(
                "match_found",
                {"city": req.city, "location": location, "time_bucket": req.time_bucket},
            )
            return {
                "status": "matched",
                "rid": rid,
                "other_phone": other.phone,
                "location": location,
                "when": when,
                "other_rid": other_rid,
            }

    # ni matcha → v čakalnico
    waiting.append(rid)
    return {"status": "waiting", "rid": rid, "location": location, "when": when}


def check_status(rid: str) -> Dict[str, Any]:
    """Preveri ali je rid še v čakanju ali je matchan."""
    _cleanup()
    if rid not in requests:
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
# rid -> other_phone
paired: Dict[str, Dict[str, Any]] = {}

def add_request_with_pairs(
    *, city: str, location: str, when: datetime, time_bucket: str = "soon", phone: str, gender: str, match_pref: str
) -> Dict[str, Any]:
    _cleanup()

    rid = uuid.uuid4().hex[:10]
    req = Request(
        rid=rid,
        created_at=datetime.now(),
        location=location,
        when=when,
        time_bucket=time_bucket,
        phone=phone,
        city=city,
        gender=gender,
        match_pref=match_pref,
    )
    requests[rid] = req

    for other_rid in list(waiting):
        other = requests.get(other_rid)
        if not other:
            continue
        if (
            other.location == location
            and other.city == req.city
            and other.time_bucket == req.time_bucket
            and other.phone != phone
            and _mutual_pref(req, other)
        ):
            try:
                waiting.remove(other_rid)
            except ValueError:
                pass
            paired[rid] = {
                "other_phone": other.phone,
                "city": city,
                "location": location,
                "when": when,
                "time_bucket": req.time_bucket,
            }
            paired[other_rid] = {
                "other_phone": phone,
                "city": city,
                "location": location,
                "when": when,
                "time_bucket": req.time_bucket,
            }
            # Best-effort web push (no PII in payload)
            payload = {
                "title": "BoniBuddy",
                "body": "Našli smo družbo! Odpri app in klikni za WhatsApp.",
                "url": "/",
            }
            logger.info("match_push_attempt rid=%s other_rid=%s", rid, other_rid)
            send_push_to_rid(rid, payload)
            send_push_to_rid(other_rid, payload)
            _ga4_send_event(
                "match_found",
                {
                    "city": city,
                    "location": location,
                    "time_bucket": req.time_bucket,
                },
            )
            return {"status": "matched", "rid": rid, **paired[rid]}

    waiting.append(rid)
    return {
        "status": "waiting",
        "rid": rid,
        "city": city,
        "location": location,
        "when": when,
        "time_bucket": req.time_bucket,
    }

def check_status_with_pairs(rid: str) -> Dict[str, Any]:
    _cleanup()
    if rid in paired:
        return {"status": "matched", "rid": rid, **paired[rid]}
    if rid not in requests:
        return {"status": "expired"}
    r = requests[rid]
    return {
        "status": "waiting",
        "rid": rid,
        "city": r.city,
        "location": r.location,
        "when": r.when,
        "time_bucket": r.time_bucket,
    }
