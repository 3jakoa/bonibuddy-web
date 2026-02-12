from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote, urlencode
import os
import re

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
INSTAGRAM_HANDLE_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


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


def render_template(request: Request, template_name: str, context: dict):
    """Render a template with request context."""
    context_with_request = {
        "request": request,
        **context,
    }
    return templates.TemplateResponse(template_name, context_with_request)

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


class PublishSlotIn(BaseModel):
    restaurant_id: str
    go_time: str
    user_id: str
    ref: str | None = None


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


def _normalize_and_validate_instagram(raw: str) -> str | None:
    handle = normalize_instagram(raw)
    if not handle:
        return None
    if not INSTAGRAM_HANDLE_RE.fullmatch(handle):
        return None
    return handle


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


def _publish_waiting_slot(
    *,
    restaurant_id: str,
    go_time_raw: str,
    user_id_raw: str,
    referrer_raw: str | None = None,
) -> tuple[dict | None, str | None]:
    user_id = _normalize_and_validate_instagram(user_id_raw)
    if not user_id:
        return None, "invalid_user_id"

    restaurant_id_norm = (restaurant_id or "").strip().lower()
    if not restaurant_id_norm:
        return None, "missing_restaurant_id"
    if not engine.get_restaurant(restaurant_id_norm):
        return None, "restaurant_not_found"

    selected_time, selected_go_time, _mapped_legacy, err = _resolve_selected_go_time(
        go_time_raw=go_time_raw,
        allow_past=False,
        default_to_now=False,
    )
    if err or not selected_time or not selected_go_time:
        return None, "invalid_go_time"

    existing_plan = engine.get_user_membership(user_id)
    if existing_plan and existing_plan.get("restaurant_id") != restaurant_id_norm:
        return None, "active_plan_exists"

    res = engine.join_slot(
        user_id=user_id,
        restaurant_id=restaurant_id_norm,
        target_time=selected_time,
        referrer=normalize_instagram(referrer_raw or ""),
    )
    if not res.get("ok"):
        return None, res.get("error", "join_failed")

    previous_count = int(res.get("previous_count") or 0)
    created_new = previous_count == 0
    window_count = max(previous_count + 1, 1)
    return (
        {
            "restaurant_id": restaurant_id_norm,
            "user_id": user_id,
            "go_time": selected_go_time,
            "window_label": _window_label_for(selected_time),
            "created_new": created_new,
            "other_count": max(previous_count, 0),
            "window_count": window_count,
        },
        None,
    )

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
        selected_restaurant_param = (request.query_params.get("restaurant_id") or "").strip().lower()
        ref_param = normalize_instagram(request.query_params.get("ref") or "")
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
                url=_with_query(
                    "/",
                    go_time=selected_go_time,
                    restaurant_id=selected_restaurant_param or None,
                    ref=ref_param or None,
                    msg=msg or None,
                ),
                status_code=303,
            )
        if mapped_legacy:
            return RedirectResponse(
                url=_with_query(
                    "/",
                    go_time=selected_go_time,
                    restaurant_id=selected_restaurant_param or None,
                    ref=ref_param or None,
                    msg=msg or None,
                ),
                status_code=303,
            )

        restaurants_for_picker = [
            {
                "id": r.id,
                "name": r.name,
                "subtitle": r.address or r.city or "",
            }
            for r in engine.list_restaurants()
        ]
        restaurants_for_picker.sort(key=lambda x: x["name"].lower())

        cookie_uid = normalize_instagram(request.cookies.get("bb_uid") or "")
        active_plan = _get_active_plan(cookie_uid)

        return render_template(
            request,
            "index.html",
            {
                "feature_waiting_board": True,
                "restaurants": restaurants_for_picker,
                "selected_go_time": selected_go_time,
                "selected_window_label": _window_label_for(selected_time),
                "selected_restaurant_id": selected_restaurant_param,
                "ref": ref_param,
                "user_id": cookie_uid,
                "msg": msg,
                "ig_username": IG_USERNAME,
                "active_plan": active_plan,
            },
        )
    return render_template(
        request,
        "index.html",
        {
            "locations": LOCATIONS_BY_CITY["ljubljana"],
            "feature_waiting_board": False,
            "ig_username": IG_USERNAME,
        },
    )


@app.get("/choose", response_class=HTMLResponse)
def choose(request: Request):
    # Legacy route: selection + publishing now lives on "/".
    return RedirectResponse(
        url=_with_query(
            "/",
            go_time=(request.query_params.get("go_time") or "").strip() or None,
            restaurant_id=(request.query_params.get("restaurant_id") or "").strip().lower() or None,
        ),
        status_code=303,
    )


@app.get("/feed", response_class=HTMLResponse)
def feed(request: Request):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    cookie_uid = normalize_instagram(request.cookies.get("bb_uid") or "")
    active_plan = _get_active_plan(cookie_uid)
    own_restaurant = active_plan["restaurant"].id if active_plan else ""
    own_go_time = active_plan["go_time"] if active_plan else ""
    items = [
        item
        for item in _build_feed_items()
        if not (own_restaurant and own_go_time and item.get("restaurant_id") == own_restaurant and item.get("go_time") == own_go_time)
    ]
    return render_template(
        request,
        "feed.html",
        {
            "items": items,
            "active_plan": active_plan,
            "msg": (request.query_params.get("msg") or "").strip(),
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


@app.post("/api/waiting/publish")
def waiting_publish_api(body: PublishSlotIn):
    if not FEATURE_WAITING_BOARD:
        raise HTTPException(status_code=404, detail="feature_disabled")

    published, err = _publish_waiting_slot(
        restaurant_id=body.restaurant_id,
        go_time_raw=body.go_time,
        user_id_raw=body.user_id,
        referrer_raw=body.ref,
    )
    if err or not published:
        detail = "join_failed"
        status_code = 400
        if err == "invalid_user_id":
            detail = "Vpiši veljavno Instagram uporabniško ime."
        elif err == "invalid_go_time":
            detail = "Izberi prihodnji čas odhoda."
        elif err == "active_plan_exists":
            detail = "Imaš že aktiven plan. Najprej ga prekliči."
            status_code = 409
        elif err == "restaurant_not_found":
            detail = "Restavracija ni bila najdena."
            status_code = 404
        elif err == "missing_restaurant_id":
            detail = "Izberi restavracijo."
        return JSONResponse(
            status_code=status_code,
            content={"ok": False, "error": err or "join_failed", "message": detail},
        )

    if published["created_new"]:
        message = "Plan objavljen."
    else:
        message = "Plan posodobljen."

    resp = JSONResponse(
        content={
            "ok": True,
            "message": message,
            "restaurant_id": published["restaurant_id"],
            "go_time": published["go_time"],
            "window_label": published["window_label"],
            "created_new": published["created_new"],
            "other_count": published["other_count"],
            "window_count": published["window_count"],
            "user_id": published["user_id"],
        }
    )
    resp.set_cookie(
        "bb_uid",
        value=published["user_id"],
        max_age=60 * 60 * 24 * 30,
        samesite="lax",
    )
    return resp


@app.get("/waiting/{restaurant_id}/quick-join")
def waiting_quick_join(
    request: Request,
    restaurant_id: str,
    go_time: str,
):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)

    user_id = normalize_instagram(request.cookies.get("bb_uid") or "")
    if not user_id:
        return RedirectResponse(
            url=_with_query(
                "/",
                restaurant_id=(restaurant_id or "").strip().lower() or None,
                go_time=(go_time or "").strip() or None,
                msg="Najprej ustvari svoj plan in vpiši Instagram uporabnika.",
            ),
            status_code=303,
        )

    published, err = _publish_waiting_slot(
        restaurant_id=restaurant_id,
        go_time_raw=go_time,
        user_id_raw=user_id,
    )
    if err or not published:
        msg = "Pridružitev ni uspela."
        if err == "invalid_go_time":
            msg = "Neveljaven čas."
        elif err == "active_plan_exists":
            msg = "Imaš že aktiven plan. Najprej ga prekliči."
        elif err == "restaurant_not_found":
            msg = "Restavracija ni bila najdena."
        return RedirectResponse(url=_with_query("/feed", msg=msg), status_code=303)

    created_flag = "1" if published["created_new"] else "0"
    back = _with_query(
        f"/done/{published['restaurant_id']}",
        go_time=published["go_time"],
        u=published["user_id"],
        created=created_flag,
    )
    resp = RedirectResponse(url=back, status_code=303)
    resp.set_cookie(
        "bb_uid",
        value=published["user_id"],
        max_age=60 * 60 * 24 * 30,
        samesite="lax",
    )
    return resp

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
        return render_template(
            request,
            "index.html",
            {
                "locations": LOCATIONS_BY_CITY["ljubljana"],
                "feature_waiting_board": False,
                "error": "Za nadaljevanje moraš potrditi, da se tvoj kontakt deli samo ob matchu.",
            },
        )

    if city not in LOCATIONS_BY_CITY:
        return render_template(
            request,
            "index.html",
            {
                "locations": LOCATIONS_BY_CITY["ljubljana"],
                "feature_waiting_board": False,
                "error": "Neveljavno mesto.",
            },
        )

    allowed_locations = LOCATIONS_BY_CITY[city]

    if location not in allowed_locations:
        return render_template(
            request,
            "index.html",
            {
                "locations": LOCATIONS_BY_CITY["ljubljana"],
                "feature_waiting_board": False,
                "error": "Neveljavna lokacija.",
            },
        )

    if time_bucket not in {"soon", "today"}:
        return render_template(
            request,
            "index.html",
            {
                "locations": LOCATIONS_BY_CITY["ljubljana"],
                "feature_waiting_board": False,
                "error": "Neveljavna izbira časa.",
            },
        )

    if match_pref not in {"any", "female", "male"}:
        return render_template(
            request,
            "index.html",
            {
                "locations": LOCATIONS_BY_CITY["ljubljana"],
                "feature_waiting_board": False,
                "error": "Neveljavna izbira preference.",
            },
        )

    if gender not in {"female", "male"}:
        return render_template(
            request,
            "index.html",
            {
                "locations": LOCATIONS_BY_CITY["ljubljana"],
                "feature_waiting_board": False,
                "error": "Neveljavna izbira spola.",
            },
        )

    handle = normalize_instagram(instagram or instagram_username)
    if not handle:
        return render_template(
            request,
            "index.html",
            {
                "locations": LOCATIONS_BY_CITY["ljubljana"],
                "feature_waiting_board": False,
                "error": "Vpiši veljavno Instagram uporabniško ime.",
            },
        )

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
        return render_template(
            request,
            "matched.html",
            {
                "location": LOCATION_LABELS.get(location, location),
                "match_instagram": res["other_instagram"],
                "city": city,
                "time_bucket": time_bucket,
            },
        )

    # waiting
    return render_template(
        request,
        "waiting.html",
        {
            "rid": res["rid"],
            "location": LOCATION_LABELS.get(location, location),
            "city": city,
            "time_bucket": time_bucket,
            "vapid_public_key": _get_env("VAPID_PUBLIC_KEY"),
            "feature_waiting_board": False,
        },
    )

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
    ref = normalize_instagram(request.query_params.get("ref") or "")
    if err:
        if err == "past_time":
            msg = "Izberi prihodnji čas odhoda."
        elif err == "invalid_time":
            msg = "Neveljaven čas. Uporabi obliko HH:MM."
        if join_focus:
            return RedirectResponse(
                url=_with_query(
                    "/",
                    restaurant_id=restaurant_id,
                    go_time=selected_go_time,
                    ref=ref or None,
                    msg=msg or None,
                ),
                status_code=303,
            )
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
        if join_focus:
            return RedirectResponse(
                url=_with_query(
                    "/",
                    restaurant_id=restaurant_id,
                    go_time=selected_go_time,
                    ref=ref or None,
                    msg=msg or None,
                ),
                status_code=303,
            )
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
    if join_focus:
        return RedirectResponse(
            url=_with_query(
                "/",
                restaurant_id=restaurant_id,
                go_time=selected_go_time,
                ref=ref or None,
                msg=msg or None,
            ),
            status_code=303,
        )

    board = engine.get_waiting_board(restaurant_id, selected_time=selected_time) or {}
    active_plan = _get_active_plan(cookie_uid)
    return render_template(
        request,
        "waiting.html",
        {
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
        "/",
        go_time=selected_go_time,
        restaurant_id=restaurant_id,
        ref=user,
    )
    copy_message_for_dm = f"Hej! Vidim na BoniBuddy, da greš jest v {restaurant.name} ob {go_time_label}. A greva skupaj? 😊"
    copy_invite_message = f"Greš na bone? Grem v {restaurant.name} ob {go_time_label}. Pridruži se: {share_url}"

    return render_template(
        request,
        "done.html",
        {
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
    published, err = _publish_waiting_slot(
        restaurant_id=restaurant_id,
        go_time_raw=go_time,
        user_id_raw=user_id,
        referrer_raw=ref,
    )
    if err or not published:
        msg = "Objava ni uspela."
        if err == "invalid_user_id":
            msg = "Vpiši veljavno Instagram uporabniško ime."
        elif err == "invalid_go_time":
            msg = "Izberi prihodnji čas odhoda."
        elif err == "active_plan_exists":
            msg = "Imaš že aktiven plan. Najprej ga prekliči."
        elif err == "restaurant_not_found":
            msg = "Restavracija ni bila najdena."
        back = _with_query(
            "/",
            go_time=go_time,
            restaurant_id=(restaurant_id or "").strip().lower() or None,
            ref=normalize_instagram(ref or "") or None,
            msg=msg,
        )
        return RedirectResponse(url=back, status_code=303)

    back = _with_query(
        "/",
        go_time=published["go_time"],
        restaurant_id=published["restaurant_id"],
        ref=normalize_instagram(ref or "") or None,
    )
    resp = RedirectResponse(url=back, status_code=303)
    resp.set_cookie(
        "bb_uid",
        value=published["user_id"],
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
    next_url: str | None = Form(None),
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
    dest = "/"
    if next_url and next_url.startswith("/"):
        dest = next_url
    return RedirectResponse(url=dest, status_code=303)


@app.get("/waiting/new", response_class=HTMLResponse)
def waiting_new(request: Request, restaurant_id: str | None = None, loc: str | None = None):
    # Legacy route: slot creation moved to home.
    return RedirectResponse(
        url=_with_query(
            "/",
            restaurant_id=(restaurant_id or "").strip().lower() or None,
        ),
        status_code=303,
    )
