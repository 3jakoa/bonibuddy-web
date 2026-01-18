from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
import re
import urllib.parse

import engine_web as engine

app = FastAPI()
templates = Jinja2Templates(directory="templates")

LOCATIONS_BY_CITY = {
    "ljubljana": ["Center", "Rožna", "Bežigrad", "Šiška", "Vič", "Drugo"],
    "maribor": ["Center", "Tabor", "Studenci", "Drugo"],
}

def normalize_phone(raw: str) -> str:
    # Sprejmi npr: +386 40 111 222 ali 040111222
    s = re.sub(r"\D+", "", raw.strip())
    if s.startswith("00"):
        s = s[2:]
    if s.startswith("386"):
        return s
    # če je slovenska 0xxxxxxxx, pretvori v 386...
    if s.startswith("0") and len(s) in (9, 10):
        return "386" + s[1:]
    return s  # fallback

def wa_link(phone: str, text: str) -> str:
    q = urllib.parse.quote(text)
    return f"https://wa.me/{phone}?text={q}"

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
    phone: str = Form(...),
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

    phone_n = normalize_phone(phone)
    if len(phone_n) < 8:
        return templates.TemplateResponse("index.html", {"request": request, "locations": LOCATIONS_BY_CITY["ljubljana"], "error": "Vpiši veljavno WhatsApp številko."})

    # Interno še vedno uporabljamo datetime za shranjevanje; UI prikazuje samo bucket (kmalu/danes).
    when = datetime.now()

    res = engine.add_request_with_pairs(
        location=location,
        when=when,
        time_bucket=time_bucket,
        phone=phone_n,
        match_pref=match_pref,
        gender=gender,
        city=city,
    )

    if res["status"] == "matched":
        other = res["other_phone"]
        msg = f"Hej! BoniBuddy naju je povezal za bone ({location}). Greva skupaj {'danes' if time_bucket == 'today' else 'kmalu'}?"
        return templates.TemplateResponse("matched.html", {
            "request": request,
            "location": location,
            "other_phone": other,
            "wa_url": wa_link(other, msg),
            "city": city,
            "time_bucket": time_bucket,
        })

    # waiting
    return templates.TemplateResponse("waiting.html", {
        "request": request,
        "rid": res["rid"],
        "location": location,
        "city": city,
        "time_bucket": time_bucket,
    })

@app.get("/status/{rid}")
def status(rid: str):
    return engine.check_status_with_pairs(rid)