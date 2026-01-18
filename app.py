from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from datetime import datetime, timedelta
import re
import urllib.parse

import engine_web as engine

app = FastAPI()
templates = Jinja2Templates(directory="templates")

LOCATIONS = ["Center", "Rožna", "Bežigrad", "Šiška", "Vič", "Drugo"]

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
    return templates.TemplateResponse("index.html", {"request": request, "locations": LOCATIONS})

@app.post("/go", response_class=HTMLResponse)
def go(
    request: Request,
    time_choice: str = Form(...),
    location: str = Form(...),
    match_pref: str = Form(...),
    gender: str = Form(...),
    phone: str = Form(...),
    consent: str = Form(None),
):
    if consent != "yes":
        return templates.TemplateResponse("index.html", {
            "request": request,
            "locations": LOCATIONS,
            "error": "Za nadaljevanje moraš potrditi, da se tvoj kontakt deli samo ob matchu."
        })

    if location not in LOCATIONS:
        return templates.TemplateResponse("index.html", {"request": request, "locations": LOCATIONS, "error": "Neveljavna lokacija."})

    if match_pref not in {"any", "female", "male"}:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS, "error": "Neveljavna izbira preference."},
        )

    if gender not in {"female", "male"}:
        return templates.TemplateResponse(
            "index.html",
            {"request": request, "locations": LOCATIONS, "error": "Neveljavna izbira spola."},
        )

    phone_n = normalize_phone(phone)
    if len(phone_n) < 8:
        return templates.TemplateResponse("index.html", {"request": request, "locations": LOCATIONS, "error": "Vpiši veljavno WhatsApp številko."})

    offset = int(time_choice)
    when = datetime.now() + timedelta(minutes=offset)

    res = engine.add_request_with_pairs(
        location=location,
        when=when,
        phone=phone_n,
        match_pref=match_pref,
        gender=gender,
    )

    if res["status"] == "matched":
        other = res["other_phone"]
        msg = f"Hej! BoniBuddy naju je povezal za bone ({location}) okoli {when.strftime('%H:%M')}. Greva skupaj?"
        return templates.TemplateResponse("matched.html", {
            "request": request,
            "location": location,
            "when": when.strftime("%H:%M"),
            "other_phone": other,
            "wa_url": wa_link(other, msg),
        })

    # waiting
    return templates.TemplateResponse("waiting.html", {
        "request": request,
        "rid": res["rid"],
        "location": location,
        "when": when.strftime("%H:%M"),
    })

@app.get("/status/{rid}")
def status(rid: str):
    return engine.check_status_with_pairs(rid)