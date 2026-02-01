from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
from urllib.parse import quote
import os

import engine_web as engine

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()

FEATURE_WAITING_BOARD = os.getenv("FEATURE_WAITING_BOARD", "false").lower() in {"1", "true", "yes"}

AREA_OPTIONS = [
    {"id": "all", "label": "Vse"},
    {"id": "center", "label": "Center"},
    {"id": "kardeljeva", "label": "Kardeljeva"},
    {"id": "rozna", "label": "Rožna"},
    {"id": "mestni_log", "label": "Mestni log"},
    {"id": "vic", "label": "Vič"},
    {"id": "siska", "label": "Šiška"},
]
AREA_LABELS = {a["id"]: a["label"] for a in AREA_OPTIONS}

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
    "rozna": "Rožna dolina",
    "kardeljeva": "Kardeljeva ploščad",
    "center": "Center",
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
    for r in engine.list_restaurants():
        bucket = engine.get_user_bucket(r.id, uid_norm)
        if bucket:
            return {"restaurant": r, "bucket": bucket}
    return None

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
        t = (request.query_params.get("t") or "now").strip().lower()
        if t not in {"now", "30", "60"}:
            return RedirectResponse(url="/?t=now", status_code=303)

        area_raw = (request.query_params.get("area") or "all").strip().lower()
        area = area_raw if area_raw in AREA_LABELS else "all"

        total_waiting = engine.get_waiting_count_all(t) if area == "all" else sum(
            engine.get_waiting_count(r.id, t) for r in engine.list_restaurants(area_id=area)
        )
        top_active = engine.get_top_active_restaurants(t, 5, area_id=None if area == "all" else area)

        # fallback list if no active: show first 5 restaurants with counts
        fallback = []
        if not top_active:
            for r in engine.list_restaurants(area_id=None if area == "all" else area):
                fallback.append(
                    {
                        "restaurant": r,
                        "count": engine.get_waiting_count(r.id, t),
                        "members": engine.get_waiting_members(r.id, t),
                    }
                )
            fallback = fallback[:5]

        cookie_uid = request.cookies.get("bb_uid")
        active_plan = _get_active_plan(cookie_uid)

        return templates.TemplateResponse(
            "index.html",
            {
                "request": request,
                "feature_waiting_board": True,
                "total_waiting": total_waiting,
                "top_active": top_active,
                "fallback_list": fallback,
                "selected_t": t,
                "selected_area": area,
                "area_options": AREA_OPTIONS,
                "area_labels": AREA_LABELS,
                "active_plan": active_plan,
            },
        )
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "feature_waiting_board": False},
    )


@app.get("/choose", response_class=HTMLResponse)
def choose(request: Request):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    t = (request.query_params.get("t") or "").strip().lower()
    if t not in {"now", "30", "60"}:
        return RedirectResponse(url="/", status_code=303)

    q_raw = request.query_params.get("q", "") or ""
    q = q_raw.strip().lower()
    area_raw = (request.query_params.get("area") or "all").strip().lower()
    area = area_raw if area_raw in AREA_LABELS else "all"
    restaurants = engine.list_restaurants(area_id=None if area == "all" else area)
    items = []
    for r in restaurants:
        if area != "all" and getattr(r, "area_id", None) != area:
            continue
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
            "location_labels": engine.LOCATION_LABELS,
            "time_bucket": t,
            "area_options": AREA_OPTIONS,
            "selected_area": area,
            "area_labels": AREA_LABELS,
            "active_plan": _get_active_plan(request.cookies.get("bb_uid")),
        },
    )

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
    board = engine.get_waiting_board(restaurant_id)
    loc_label = engine.LOCATION_LABELS.get(restaurant.location_id, restaurant.location_id)
    user_bucket = engine.get_user_bucket(restaurant_id, user_id) if user_id else None
    join_focus = request.query_params.get("join")
    pref_time_bucket = request.query_params.get("t")
    selected_bucket = user_bucket or (pref_time_bucket if pref_time_bucket in {"now", "30", "60"} else "now")
    cookie_uid = request.cookies.get("bb_uid") or user_id
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
            "user_id": user_id or "",
            "user_bucket": user_bucket,
            "msg": request.query_params.get("msg", ""),
            "join_focus": bool(join_focus),
            "selected_bucket": selected_bucket,
            "active_plan": active_plan,
        },
    )


@app.get("/done/{restaurant_id}", response_class=HTMLResponse)
def done_screen(request: Request, restaurant_id: str, t: str = "now", u: str | None = None):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    bucket = (t or "now").strip().lower()
    if bucket not in {"now", "30", "60"}:
        return RedirectResponse(url=f"/done/{restaurant_id}?t=now&u={quote(u or '')}", status_code=303)
    user = (u or request.cookies.get("bb_uid") or "").strip()
    if not user:
        return RedirectResponse(url=f"/waiting/{restaurant_id}?t={bucket}", status_code=303)

    restaurant = engine.get_restaurant(restaurant_id)
    if not restaurant:
        raise HTTPException(status_code=404, detail="restaurant_not_found")

    members = engine.get_waiting_members(restaurant_id, bucket)
    normalized_user = engine._normalize_instagram(user) if hasattr(engine, "_normalize_instagram") else user.lower()
    others = [m for m in members if (m or "").lower().lstrip("@") != normalized_user]
    joined_existing = len(members) >= 2
    primary_other = others[0] if others else ""
    bucket_label = {"now": "zdaj", "30": "čez 30 min", "60": "čez 60 min"}.get(bucket, bucket)
    instagram_url = f"https://instagram.com/{quote(primary_other)}" if primary_other else ""
    share_url = f"/waiting/{restaurant_id}?t={bucket}&join=1"
    copy_message_for_dm = f"Hej! Vidim na BoniBuddy, da greš jest v {restaurant.name} {bucket_label}. A greva skupaj? 😊"
    copy_invite_message = f"Greš na bone? Grem v {restaurant.name} {bucket_label}. Pridruži se: {share_url}"

    return templates.TemplateResponse(
        "done.html",
        {
            "request": request,
            "restaurant": restaurant,
            "bucket": bucket,
            "bucket_label": bucket_label,
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
    time_bucket: str = Form(...),
    user_id: str = Form(...),
):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    user_id = (user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id")
    res = engine.join_slot(user_id=user_id, restaurant_id=restaurant_id, time_bucket=time_bucket)
    if not res.get("ok"):
        raise HTTPException(status_code=400, detail=res.get("error", "join_failed"))
    back = f"/done/{restaurant_id}?t={quote(time_bucket)}&u={quote(user_id)}"
    return RedirectResponse(url=back, status_code=303)


@app.post("/waiting/leave")
def waiting_leave(
    request: Request,
    restaurant_id: str = Form(...),
    time_bucket: str = Form(...),
    user_id: str = Form(...),
):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    user_id = (user_id or "").strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing user_id")
    engine.leave_slot(user_id=user_id, restaurant_id=restaurant_id, time_bucket=time_bucket)
    back = f"/waiting/{restaurant_id}?user_id={quote(user_id)}"
    return RedirectResponse(url=back, status_code=303)


@app.post("/plan/cancel")
def plan_cancel(
    request: Request,
    restaurant_id: str | None = Form(None),
    time_bucket: str | None = Form(None),
):
    if not FEATURE_WAITING_BOARD:
        return RedirectResponse(url="/", status_code=303)
    user_id = request.cookies.get("bb_uid", "").strip()
    if not user_id:
        return RedirectResponse(url="/", status_code=303)
    target_restaurant = restaurant_id
    target_bucket = time_bucket
    if not target_restaurant or not target_bucket:
        plan = _get_active_plan(user_id)
        if plan:
            target_restaurant = plan["restaurant"].id
            target_bucket = plan["bucket"]
    if target_restaurant and target_bucket:
        engine.leave_slot(user_id=user_id, restaurant_id=target_restaurant, time_bucket=target_bucket)
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
