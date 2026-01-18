from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import uuid

WINDOW_MIN = 15
EXPIRE_MIN = 90  # po koliko min request poteče

@dataclass
class Request:
    rid: str
    created_at: datetime
    location: str
    when: datetime
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
            and _close_in_time(other.when, when)
            and other.phone != phone
            and _mutual_pref(req, other)
        ):
            # match found: odstrani other iz waiting, tudi novega ne dodajamo
            try:
                waiting.remove(other_rid)
            except ValueError:
                pass
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
    *, city: str, location: str, when: datetime, phone: str, gender: str, match_pref: str
) -> Dict[str, Any]:
    _cleanup()

    rid = uuid.uuid4().hex[:10]
    req = Request(
        rid=rid,
        created_at=datetime.now(),
        location=location,
        when=when,
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
            and _close_in_time(other.when, when)
            and other.phone != phone
            and _mutual_pref(req, other)
        ):
            try:
                waiting.remove(other_rid)
            except ValueError:
                pass

            paired[rid] = {"other_phone": other.phone, "city": city, "location": location, "when": when}
            paired[other_rid] = {"other_phone": phone, "city": city, "location": location, "when": when}
            return {"status": "matched", "rid": rid, **paired[rid]}

    waiting.append(rid)
    return {"status": "waiting", "rid": rid, "city": city, "location": location, "when": when}

def check_status_with_pairs(rid: str) -> Dict[str, Any]:
    _cleanup()
    if rid in paired:
        return {"status": "matched", "rid": rid, **paired[rid]}
    if rid not in requests:
        return {"status": "expired"}
    r = requests[rid]
    return {"status": "waiting", "rid": rid, "city": r.city, "location": r.location, "when": r.when}
