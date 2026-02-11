from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode
import os

import engine_web as engine

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()

# Default the waiting board experience ON; can still be disabled via env.
FEATURE_WAITING_BOARD = os.getenv("FEATURE_WAITING_BOARD", "true").lower() in {"1", "true", "yes"}
IG_USERNAME = "bonibuddy"
LOCAL_TZ = ZoneInfo("Europe/Ljubljana")
GO_TIME_STEP_MINUTES = 5
ACTIVE_WINDOW_MINUTES = 30
LEGACY_T_OFFSETS = {"now": 0, "30": 30, "60": 60}


def _now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def _to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)
    return value.astimezone(LOCAL_TZ)


def _parse_iso_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat((value or "").strip())
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_go_time(value: datetime) -> str:
    return _to_local(value).strftime("%H:%M")


def _window_label_for(value: datetime) -> str:
    start = _to_local(value)
    end = start + timedelta(minutes=ACTIVE_WINDOW_MINUTES)
    return f"{start:%H:%M}–{end:%H:%M}"


def _default_go_time(now_local: datetime | None = None) -> datetime:
    now_local = now_local or _now_local()
    rounded = now_local.replace(second=0, microsecond=0)
    remainder = rounded.minute % GO_TIME_STEP_MINUTES
    if remainder:
        rounded += timedelta(minutes=GO_TIME_STEP_MINUTES - remainder)
    if rounded.date() != now_local.date():
        rounded = now_local.replace(second=0, microsecond=0)
    return rounded


def _parse_go_time(raw: str, now_local: datetime | None = None) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    now_local = now_local or _now_local()
    try:
        parsed = datetime.strptime(value, "%H:%M")
    except ValueError:
        return None
    return now_local.replace(hour=parsed.hour, minute=parsed.minute, second=0, microsecond=0)


def _legacy_t_to_go_time(raw_t: str | None, now_local: datetime | None = None) -> str | None:
    t = (raw_t or "").strip().lower()
    if t not in LEGACY_T_OFFSETS:
        return None
    now_local = now_local or _now_local()
    return _format_go_time(now_local + timedelta(minutes=LEGACY_T_OFFSETS[t]))


def _resolve_selected_go_time(
    *,
    go_time_raw: str | None,
    legacy_t_raw: str | None = None,
    allow_past: bool = False,
    default_to_now: bool = True,
) -> tuple[datetime | None, str | None, bool, str | None]:
    now_local = _now_local()
    source_legacy = False
    resolved = (go_time_raw or "").strip()
    if not resolved and legacy_t_raw:
        mapped = _legacy_t_to_go_time(legacy_t_raw, now_local=now_local)
        if not mapped:
            return None, None, False, "invalid_time"
        resolved = mapped
        source_legacy = True

    if not resolved:
        if not default_to_now:
            return None, None, source_legacy, None
        selected = _default_go_time(now_local=now_local)
        return selected, _format_go_time(selected), source_legacy, None

    selected = _parse_go_time(resolved, now_local=now_local)
    if not selected:
        return None, None, source_legacy, "invalid_time"

    floor_now = now_local.replace(second=0, microsecond=0)
    if not allow_past and selected < floor_now:
        return None, _format_go_time(_default_go_time(now_local=now_local)), source_legacy, "past_time"

    return selected, _format_go_time(selected), source_legacy, None


def _with_query(path: str, **params: str | None) -> str:
    clean = {k: v for k, v in params.items() if v is not None and v != ""}
    if not clean:
        return path
    return f"{path}?{urlencode(clean)}"


def _build_feed_items(now: datetime | None = None) -> list[dict]:
    """Collect active cards (count>0) across all restaurants/windows."""
    rows: list[dict] = []
    for r in engine.list_restaurants():
        board = engine.get_waiting_board(r.id)
        for info in (board or {}).values():
            count = int(info.get("count") or 0)
            if count <= 0:
                continue
            target_time = _parse_iso_datetime(info.get("target_time_iso") or "")
            if not target_time:
                continue
            rows.append(
                {
                    "restaurant_id": r.id,
                    "restaurant_name": r.name,
                    "go_time": _format_go_time(target_time),
                    "count": count,
                    "window_label": _window_label_for(target_time),
                    "sort_time": _to_local(target_time),
                }
            )
    rows.sort(
        key=lambda x: (
            x["sort_time"],
            -x["count"],
            x["restaurant_name"].lower(),
        )
    )
    for row in rows:
        row.pop("sort_time", None)
    return rows

def _get_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    return v


def send_push_to_rid(rid: str, payload: dict) -> bool:
    """Compatibility wrapper: delegate to engine (engine_web.py)."""
    try:
        return bool(engine.send_push_to_rid(rid, payload))
    except Exception:
        return False


class PushSubscribeIn(BaseModel):
    rid: str
    subscription: dict

# Minimal sanity-test endpoint for web push
class PushTestIn(BaseModel):
    rid: str
    title: str | None = None
    body: str | None = None
    url: str | None = None


@app.post("/api/push/subscribe")
def push_subscribe(body: PushSubscribeIn):
    rid = (body.rid or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="Missing rid")

    sub = body.subscription
    if not isinstance(sub, dict) or not sub.get("endpoint"):
        raise HTTPException(status_code=400, detail="Invalid subscription")

    engine.set_push_subscription(rid, sub)
    return {"ok": True}

# Minimal sanity-test endpoint for web push
@app.post("/api/push/test")
def push_test(body: PushTestIn):
    rid = (body.rid or "").strip()
    if not rid:
        raise HTTPException(status_code=400, detail="Missing rid")

    payload = {
        "title": body.title or "BoniBuddy test",
        "body": body.body or "Če vidiš to, push dela 🎉",
        "url": body.url or "/",
    }

    ok = send_push_to_rid(rid, payload)
    return {"ok": bool(ok)}

# Use absolute paths so it works reliably on Railway
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Serve static assets under /static
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

LOCATION_LABELS = {
    "center": "Center",
    "kardeljeva": "Kardeljeva",
    "rozna": "Rožna",
    "mestni_log": "Mestni log",
}
LOCATIONS_BY_CITY = {
    "ljubljana": list(LOCATION_LABELS.keys()),
    "maribor": list(LOCATION_LABELS.keys()),
}

def normalize_instagram(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("@"):
        s = s[1:]
    return s


def _get_active_plan(user_id: str | None) -> dict | None:
    if not user_id:
        return None
    uid_norm = normalize_instagram(user_id).lower()
    plan = engine.get_user_membership(uid_norm)
    if not plan:
        return None
    restaurant_id = (plan.get("restaurant_id") or "").strip().lower()
    if not restaurant_id:
        return None
    restaurant = engine.get_restaurant(restaurant_id)
    if not restaurant:
        return None
    target_time = plan.get("target_time")
    if not isinstance(target_time, datetime):
        target_time = _parse_iso_datetime(plan.get("target_time_iso") or "")
    if not target_time:
        return None
    target_local = _to_local(target_time)
    window_end = target_local + timedelta(minutes=ACTIVE_WINDOW_MINUTES)
    return {
        "restaurant": restaurant,
        "go_time": f"{target_local:%H:%M}",
        "window_label": f"{target_local:%H:%M}–{window_end:%H:%M}",
        "expires_label": f"do {window_end:%H:%M}",
    }

# --- PWA convenience routes (some browsers request these at the root) ---
@app.get("/manifest.webmanifest")
def pwa_manifest_root():
    return RedirectResponse(url="/static/manifest.webmanifest")

@app.get("/sw.js")
def pwa_sw_root():
    # Serve the SW script from the root so it can control scope '/'
    return FileResponse(
        path=str(BASE_DIR / "static" / "sw.js"),
        media_type="application/javascript",
    )

@app.get("/icons/{path:path}")
def pwa_icons_root(path: str):
    return RedirectResponse(url=f"/static/icons/{path}")

@app.get("/apple-touch-icon.png")
def pwa_apple_touch_icon_root():
    return RedirectResponse(url="/static/icons/icon-192.png")

@app.get("/apple-touch-icon-120x120.png")
def pwa_apple_touch_icon_120():
    return RedirectResponse(url="/static/icons/icon-192.png")

@app.get("/apple-touch-icon-120x120-precomposed.png")
def pwa_apple_touch_icon_120_precomposed():
    return RedirectResponse(url="/static/icons/icon-192.png")

@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if FEATURE_WAITING_BOARD:
        query_raw = (request.query_params.get("q") or "").strip()
        selected_time, selected_go_time, mapped_legacy, err = _resolve_selected_go_time(
            go_time_raw=request.query_params.get("go_time"),
            legacy_t_raw=request.query_params.get("t"),
            allow_past=False,
            default_to_now=True,
        )
        msg = (request.query_params.get("msg") or "").strip()
        if err:
            if err == "past_time":
                msg = "Izberi prihodnji čas odhoda."
            elif err == "invalid_time":
                msg = "Neveljaven čas. Uporabi obliko HH:MM."
            return RedirectResponse(
                url=_with_query("/", go_time=selected_go_time, q=query_raw or None, msg=msg or None),
                status_code=303,
            )
        if mapped_legacy:
            return RedirectResponse(
                url=_with_query("/", go_time=selected_go_time, q=query_raw or None, msg=msg or None),
                status_code=303,
            )

        candidate_restaurants = list(engine.list_restaurants(search=query_raw))
        rows = []
        total_waiting = 0
        for r in candidate_restaurants:
            cnt = engine.get_waiting_count(r.id, selected_time)
            total_waiting += cnt
            rows.append(
                {
                    "restaurant": r,
                    "count": cnt,
                    "members": engine.get_waiting_members(r.id, selected_time),
                }
            )

        rows.sort(key=lambda x: (-int(x["count"] > 0), -x["count"], x["restaurant"].name.lower()))

        cookie_uid = request.cookies.get("bb_uid")
        active_plan = _get_active_plan(cookie_uid)

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "feature_waiting_board": True,
                "total_waiting": total_waiting,
                "rows": rows,
                "selected_go_time": selected_go_time,
                "selected_window_label": _window_label_for(selected_time),
                "query": query_raw,
                "msg": msg,
                "ig_username": IG_USERNAME,
                "active_plan": active_plan,
            },
        )
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "locations": LOCATIONS_BY_CITY["ljubljana"],
            "feature_waiting_board": False,
            "ig_username": IG_USERNAME,
        },
    )


@app.get("/choose", response_class=HTMLResponse)
def choose(request: Request):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    selected_time, selected_go_time, mapped_legacy, err = _resolve_selected_go_time(
        go_time_raw=request.query_params.get("go_time"),
        legacy_t_raw=request.query_params.get("t"),
        allow_past=False,
        default_to_now=True,
    )
    q_raw = request.query_params.get("q", "") or ""
    if err:
        return RedirectResponse(
            url=_with_query("/choose", go_time=selected_go_time, q=q_raw.strip() or None),
            status_code=303,
        )
    if mapped_legacy:
        return RedirectResponse(
            url=_with_query("/choose", go_time=selected_go_time, q=q_raw.strip() or None),
            status_code=303,
        )

    q = q_raw.strip().lower()
    restaurants = engine.list_restaurants(search=q_raw.strip())
    items = []
    for r in restaurants:
        if q and q not in r.name.lower():
            continue
        total = engine.get_waiting_total(r.id)
        items.append({"restaurant": r, "total_waiting": total})
    items.sort(key=lambda x: (-x["total_waiting"], x["restaurant"].name.lower()))

    return templates.TemplateResponse(
        "choose.html",
        {
            "request": request,
            "feature_waiting_board": True,
            "restaurants_with_counts": items,
            "query": q_raw,
            "selected_go_time": selected_go_time,
            "selected_window_label": _window_label_for(selected_time),
            "ig_username": IG_USERNAME,
            "active_plan": _get_active_plan(request.cookies.get("bb_uid")),
        },
    )


@app.get("/feed", response_class=HTMLResponse)
def feed(request: Request):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    items = _build_feed_items()
    return templates.TemplateResponse(
        "feed.html",
        {
            "request": request,
            "items": items,
        },
    )


@app.get("/api/feed")
def api_feed():
    if not FEATURE_WAITING_BOARD:
        raise HTTPException(status_code=404, detail="feature_disabled")
    items = _build_feed_items()
    return {
        "items": items,
        "generated_at_iso": datetime.now(LOCAL_TZ).isoformat(),
    }

@app.post("/go", response_class=HTMLResponse)
def go(
    request: Request,
    time_bucket: str = Form(...),
    city: str = Form(...),
    location: str = Form(...),
    match_pref: str = Form(...),
    gender: str = Form(...),
    instagram: str = Form(None),
    instagram_username: str = Form(None),
    consent: str = Form(None),
):
    if FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    if consent != "yes":
        return templates.TemplateResponse("index.html", {
            "request": request,
            "locations": LOCATIONS_BY_CITY["ljubljana"],
            "feature_waiting_board": False,
            "error": "Za nadaljevanje moraš potrditi, da se tvoj kontakt deli samo ob matchu."
        })

    if city not in LOCATIONS_BY_CITY:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "feature_waiting_board": False, "error": "Neveljavno mesto."},
        )

    allowed_locations = LOCATIONS_BY_CITY[city]

    if location not in allowed_locations:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "feature_waiting_board": False, "error": "Neveljavna lokacija."},
        )

    if time_bucket not in {"soon", "today"}:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "feature_waiting_board": False, "error": "Neveljavna izbira časa."},
        )

    if match_pref not in {"any", "female", "male"}:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "feature_waiting_board": False, "error": "Neveljavna izbira preference."},
        )

    if gender not in {"female", "male"}:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "feature_waiting_board": False, "error": "Neveljavna izbira spola."},
        )

    handle = normalize_instagram(instagram or instagram_username)
    if not handle:
        return templates.TemplateResponse("index.html", {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "feature_waiting_board": False, "error": "Vpiši veljavno Instagram uporabniško ime."})

    # Interno še vedno uporabljamo datetime za shranjevanje; UI prikazuje samo bucket (kmalu/danes).
    when = datetime.now()

    res = engine.add_request_with_pairs(
        location=location,
        when=when,
        time_bucket=time_bucket,
        instagram=handle,
        match_pref=match_pref,
        gender=gender,
        city=city,
    )

    if res["status"] == "matched":
        return templates.TemplateResponse("matched.html", {
            "request": request,
            "location": LOCATION_LABELS.get(location, location),
            "match_instagram": res["other_instagram"],
            "city": city,
            "time_bucket": time_bucket,
        })

    # waiting
    return templates.TemplateResponse("waiting.html", {
        "request": request,
        "rid": res["rid"],
        "location": LOCATION_LABELS.get(location, location),
        "city": city,
        "time_bucket": time_bucket,
        "vapid_public_key": _get_env("VAPID_PUBLIC_KEY"),
        "feature_waiting_board": False,
    })

@app.get("/status/{rid}")
def status(rid: str):
    return engine.check_status_with_pairs(rid)


# ----------------- Waiting board MVP -----------------
@app.get("/locations")
def locations_list():
    return [{"id": loc.id, "name": loc.name} for loc in engine.list_locations()]


@app.get("/waiting/{restaurant_id}", response_class=HTMLResponse)
def waiting_board(request: Request, restaurant_id: str, user_id: str | None = None):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    restaurant = engine.get_restaurant(restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="restaurant_not_found")
    city_label = (getattr(restaurant, "city", "") or "").title()
    loc_label = restaurant.address or city_label or restaurant.location_id or restaurant.id
    cookie_uid = (request.cookies.get("bb_uid") or user_id or "").strip()
    membership = engine.get_user_membership(cookie_uid) if cookie_uid else None
    user_go_time = None
    restaurant_id_norm = (restaurant_id or "").strip().lower()
    if membership and membership.get("restaurant_id") == restaurant_id_norm:
        target = membership.get("target_time")
        if isinstance(target, datetime):
            user_go_time = _format_go_time(target)
        else:
            parsed_target = _parse_iso_datetime(membership.get("target_time_iso") or "")
            if parsed_target:
                user_go_time = _format_go_time(parsed_target)

    go_time_param = request.query_params.get("go_time")
    legacy_t = request.query_params.get("t")
    if not go_time_param and user_go_time:
        selected_time = _parse_go_time(user_go_time)
        selected_go_time = user_go_time
        mapped_legacy = False
        err = None
    else:
        selected_time, selected_go_time, mapped_legacy, err = _resolve_selected_go_time(
            go_time_raw=go_time_param,
            legacy_t_raw=legacy_t,
            allow_past=False,
            default_to_now=True,
        )
    if not selected_time or not selected_go_time:
        selected_time = _default_go_time()
        selected_go_time = _format_go_time(selected_time)

    join_focus = request.query_params.get("join")
    msg = (request.query_params.get("msg") or "").strip()
    ref = (request.query_params.get("ref") or "").strip()
    if err:
        if err == "past_time":
            msg = "Izberi prihodnji čas odhoda."
        elif err == "invalid_time":
            msg = "Neveljaven čas. Uporabi obliko HH:MM."
        return RedirectResponse(
            url=_with_query(
                f"/waiting/{restaurant_id}",
                go_time=selected_go_time,
                join="1" if join_focus else None,
                ref=ref or None,
                msg=msg or None,
                user_id=user_id or None,
            ),
            status_code=303,
        )
    if mapped_legacy:
        return RedirectResponse(
            url=_with_query(
                f"/waiting/{restaurant_id}",
                go_time=selected_go_time,
                join="1" if join_focus else None,
                ref=ref or None,
                msg=msg or None,
                user_id=user_id or None,
            ),
            status_code=303,
        )

    board = engine.get_waiting_board(restaurant_id, selected_time=selected_time) or {}
    active_plan = _get_active_plan(cookie_uid)
    return templates.TemplateResponse(
        "waiting.html",
        {
            "request": request,
            "feature_waiting_board": True,
            "restaurant_id": restaurant_id,
            "restaurant": restaurant,
            "location_label": loc_label,
            "board": board,
            "user_id": cookie_uid or "",
            "user_go_time": user_go_time,
            "msg": msg,
            "join_focus": bool(join_focus),
            "selected_go_time": selected_go_time,
            "selected_window_label": _window_label_for(selected_time),
            "selected_count": int(board.get("count") or 0),
            "selected_members": board.get("members") or [],
            "active_plan": active_plan,
        },
    )

@app.get("/api/waiting_board/{restaurant_id}")
def waiting_board_api(request: Request, restaurant_id: str):
    """Return live waiting board counts for polling on the client."""
    if not FEATURE_WAITING_BOARD:
        raise HTTPException(status_code=404)
    restaurant = engine.get_restaurant(restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="restaurant_not_found")
    go_time_raw = request.query_params.get("go_time")
    if go_time_raw:
        selected_time, selected_go_time, _mapped_legacy, err = _resolve_selected_go_time(
            go_time_raw=go_time_raw,
            allow_past=True,
            default_to_now=False,
        )
        if err or not selected_time or not selected_go_time:
            raise HTTPException(status_code=400, detail="invalid_go_time")
        board = engine.get_waiting_board(restaurant_id, selected_time=selected_time) or {}
        return {
            "go_time": selected_go_time,
            "window_label": _window_label_for(selected_time),
            "count": int(board.get("count") or 0),
            "members": board.get("members") or [],
        }

    grouped = engine.get_waiting_board(restaurant_id) or {}
    output = {}
    for key, info in grouped.items():
        target_dt = _parse_iso_datetime(info.get("target_time_iso") or "")
        if not target_dt:
            continue
        output[key] = {
            "go_time": _format_go_time(target_dt),
            "window_label": _window_label_for(target_dt),
            "count": int(info.get("count") or 0),
            "members": info.get("members") or [],
        }
    return output


@app.get("/done/{restaurant_id}", response_class=HTMLResponse)
def done_screen(
    request: Request,
    restaurant_id: str,
    go_time: str | None = None,
    t: str | None = None,
    u: str | None = None,
):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    selected_time, selected_go_time, mapped_legacy, err = _resolve_selected_go_time(
        go_time_raw=go_time,
        legacy_t_raw=t,
        allow_past=True,
        default_to_now=True,
    )
    if not selected_time or not selected_go_time:
        selected_time = _default_go_time()
        selected_go_time = _format_go_time(selected_time)
    if err:
        return RedirectResponse(url=_with_query(f"/done/{restaurant_id}", go_time=selected_go_time, u=u or None), status_code=303)
    if mapped_legacy:
        return RedirectResponse(url=_with_query(f"/done/{restaurant_id}", go_time=selected_go_time, u=u or None), status_code=303)
    user = (u or request.cookies.get("bb_uid") or "").strip()
    if not user:
        return RedirectResponse(url=_with_query(f"/waiting/{restaurant_id}", go_time=selected_go_time), status_code=303)

    restaurant = engine.get_restaurant(restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="restaurant_not_found")

    members = engine.get_waiting_members(restaurant_id, selected_time)
    normalized_user = engine._normalize_instagram(user) if hasattr(engine, "_normalize_instagram") else user.lower()
    others = [m for m in members if (m or "").lower().lstrip("@") != normalized_user]
    created_param = request.query_params.get("created")
    if created_param is not None:
        joined_existing = created_param == "0"
    else:
        joined_existing = len(others) >= 1
    primary_other = others[0] if others else ""
    go_time_label = _format_go_time(selected_time)
    window_label = _window_label_for(selected_time)
    instagram_url = f"https://instagram.com/{quote(primary_other)}" if primary_other else ""
    share_url = _with_query(
        f"/waiting/{restaurant_id}",
        go_time=selected_go_time,
        join="1",
        ref=user,
    )
    copy_message_for_dm = f"Hej! Vidim na BoniBuddy, da greš jest v {restaurant.name} ob {go_time_label}. A greva skupaj? 😊"
    copy_invite_message = f"Greš na bone? Grem v {restaurant.name} ob {go_time_label}. Pridruži se: {share_url}"

    return templates.TemplateResponse(
        "done.html",
        {
            "request": request,
            "restaurant": restaurant,
            "go_time": selected_go_time,
            "go_time_label": go_time_label,
            "window_label": window_label,
            "members": ["@" + m.lstrip("@") for m in members],
            "joined_existing": joined_existing,
            "primary_other": "@" + primary_other.lstrip("@") if primary_other else "",
            "other_count": max(len(others) - 1, 0),
            "instagram_url": instagram_url,
            "share_url": share_url,
            "copy_message_for_dm": copy_message_for_dm,
            "copy_invite_message": copy_invite_message,
            "user_id": user,
        },
    )


@app.post("/waiting/join")
def waiting_join(
    request: Request,
    restaurant_id: str = Form(...),
    go_time: str = Form(...),
    user_id: str = Form(...),
    ref: str | None = Form(None),
):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    user_id = (user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id")
    selected_time, selected_go_time, _mapped_legacy, err = _resolve_selected_go_time(
        go_time_raw=go_time,
        allow_past=False,
        default_to_now=False,
    )
    if err or not selected_time or not selected_go_time:
        msg = "Izberi prihodnji čas odhoda."
        back = _with_query(f"/waiting/{restaurant_id}", go_time=go_time, msg=msg)
        return RedirectResponse(url=back, status_code=303)
    existing_plan = engine.get_user_membership(user_id)
    if existing_plan and existing_plan.get("restaurant_id") != (restaurant_id or "").strip().lower():
        msg = "Imaš že aktiven plan. Najprej ga prekliči."
        back = _with_query(f"/waiting/{restaurant_id}", go_time=selected_go_time, msg=msg)
        return RedirectResponse(url=back, status_code=303)
    res = engine.join_slot(user_id=user_id, restaurant_id=restaurant_id, target_time=selected_time, referrer=ref)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "join_failed"))
    prev_count = int(res.get("previous_count", 1))
    created_new = prev_count == 0
    back = _with_query(
        f"/done/{restaurant_id}",
        go_time=selected_go_time,
        u=user_id,
        created="1" if created_new else "0",
    )
    resp = RedirectResponse(url=back, status_code=303)
    resp.set_cookie(
        "bb_uid",
        value=user_id,
        max_age=60 * 60 * 24 * 30,  # 30 dni
        samesite="lax",
    )
    return resp


@app.post("/waiting/leave")
def waiting_leave(
    request: Request,
    restaurant_id: str = Form(...),
    user_id: str = Form(...),
    go_time: str | None = Form(None),
):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    user_id = (user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id")
    engine.leave_slot(user_id=user_id, restaurant_id=restaurant_id)
    _, selected_go_time, _mapped_legacy, _err = _resolve_selected_go_time(
        go_time_raw=go_time,
        allow_past=True,
        default_to_now=True,
    )
    back = _with_query(f"/waiting/{restaurant_id}", user_id=user_id, go_time=selected_go_time)
    return RedirectResponse(url=back, status_code=303)


@app.post("/plan/cancel")
def plan_cancel(
    request: Request,
    restaurant_id: str | None = Form(None),
):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    user_id = request.cookies.get("bb_uid", "").strip()
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    target_restaurant = restaurant_id
    if not target_restaurant:
        plan = _get_active_plan(user_id)
        if plan:
            target_restaurant = plan["restaurant"].id
    if target_restaurant:
        engine.leave_slot(user_id=user_id, restaurant_id=target_restaurant)
    return RedirectResponse(url="/", status_code=303)


@app.get("/waiting/new", response_class=HTMLResponse)
def waiting_new(request: Request, restaurant_id: str | None = None, loc: str | None = None):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    restaurant = engine.get_restaurant(restaurant_id) if restaurant_id else None
    loc_label = None
    if restaurant:
        loc_label = engine.LOCATION_LABELS.get(restaurant.location_id, restaurant.location_id)
    elif loc:
        loc_label = engine.LOCATION_LABELS.get(loc, loc)
    return templates.TemplateResponse(
        "new_waiting.html",
        {
            "request": request,
            "restaurant": restaurant,
            "loc_label": loc_label,
        },
    )
