from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime
import os

import engine_web as engine

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI()


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
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"]},
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
    if consent != "yes":
        return templates.TemplateResponse("index.html", {
            "request": request,
            "locations": LOCATIONS_BY_CITY["ljubljana"],
            "error": "Za nadaljevanje moraš potrditi, da se tvoj kontakt deli samo ob matchu."
        })

    if city not in LOCATIONS_BY_CITY:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "error": "Neveljavno mesto."},
        )

    allowed_locations = LOCATIONS_BY_CITY[city]

    if location not in allowed_locations:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "error": "Neveljavna lokacija."},
        )

    if time_bucket not in {"soon", "today"}:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "error": "Neveljavna izbira časa."},
        )

    if match_pref not in {"any", "female", "male"}:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "error": "Neveljavna izbira preference."},
        )

    if gender not in {"female", "male"}:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "error": "Neveljavna izbira spola."},
        )

    handle = normalize_instagram(instagram or instagram_username)
    if not handle:
        return templates.TemplateResponse("index.html", {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "error": "Vpiši veljavno Instagram uporabniško ime."})

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
    })

@app.get("/status/{rid}")
def status(rid: str):
    return engine.check_status_with_pairs(rid)
