from __future__ import annotations

from pathlib import Path


PAGE_WIDTH = 612
PAGE_HEIGHT = 792
LEFT_MARGIN = 54
TOP_Y = 748


def pdf_escape(text: str) -> str:
    return text.replace('\\', r'\\').replace('(', r'\(').replace(')', r'\)')


def wrap_text(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return [""]

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def build_lines() -> list[tuple[str, int, int, str]]:
    # (font_id, size, y, text)
    lines: list[tuple[str, int, int, str]] = []
    y = TOP_Y

    def add(font: str, size: int, text: str, step: int) -> None:
        nonlocal y
        lines.append((font, size, y, text))
        y -= step

    add("F2", 18, "BoniBuddy App Summary (One Page)", 26)

    add("F2", 12, "What it is", 18)
    what_it_is = (
        "BoniBuddy is a FastAPI web app for finding meal company and coordinating a shared go-time at student-subsidy restaurants. "
        "It currently defaults to a waiting-board workflow, with a legacy one-to-one matching flow still present in code."
    )
    for line in wrap_text(what_it_is, 105):
        add("F1", 10, line, 14)

    y -= 2
    add("F2", 12, "Who it is for", 18)
    who_for = (
        "Primary persona: students using Slovenian student meal subsidies (boni), especially users coordinating lunch plans around Ljubljana listings."
    )
    for line in wrap_text(who_for, 105):
        add("F1", 10, line, 14)

    y -= 2
    add("F2", 12, "What it does", 18)
    features = [
        "- Browses and searches restaurants loaded from data/restaurants.json.",
        "- Shows active counts and handles for a selected go-time window per restaurant.",
        "- Lets users publish a plan, join others, and cancel via form posts.",
        "- Provides a live feed and waiting-board updates via JSON polling endpoints.",
        "- Supports legacy mutual-preference matching with waiting/matched/expired states.",
        "- Supports installable PWA behavior and optional web push notifications.",
        "- Sends best-effort GA4 analytics events for core interactions.",
    ]
    for bullet in features:
        for line in wrap_text(bullet, 101):
            add("F1", 10, line, 14)

    y -= 2
    add("F2", 12, "How it works (repo evidence)", 18)
    architecture = [
        "- UI: Jinja templates in templates/ plus vanilla JS polling (/api/feed, /api/waiting_board/{id}, /status/{rid}).",
        "- Web/API: FastAPI routes in app.py render pages, accept form actions, and serve JSON.",
        "- Domain logic: engine_web.py handles restaurant loading, waiting slots, cleanup, and matching rules.",
        "- Data/state: seed data from data/restaurants.json; runtime state kept in in-memory dict/list structures.",
        "- Integrations: optional VAPID web push (pywebpush) and GA4 Measurement Protocol env vars.",
        "- Flow: Browser request -> FastAPI route -> engine query/mutation -> HTML or JSON -> client refresh.",
    ]
    for bullet in architecture:
        for line in wrap_text(bullet, 101):
            add("F1", 10, line, 14)

    y -= 2
    add("F2", 12, "How to run (minimal)", 18)
    run_steps = [
        "1. Use Python 3.11 (runtime.txt pins 3.11.9) and create/activate a virtual environment.",
        "2. Install dependencies: pip install -r requirements.txt.",
        "3. Start app (inferred from FastAPI app object and uvicorn dependency): uvicorn app:app --reload --port 8000.",
        "4. Open http://127.0.0.1:8000 in a browser.",
        "5. Optional env vars for extras: FEATURE_WAITING_BOARD, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT, GA4_MEASUREMENT_ID, GA4_API_SECRET.",
        "6. Official README/start script: Not found in repo.",
    ]
    for step in run_steps:
        for line in wrap_text(step, 101):
            add("F1", 10, line, 14)

    if y < 48:
        raise RuntimeError(f"Layout overflow: final y={y}")

    return lines


def build_pdf(lines: list[tuple[str, int, int, str]]) -> bytes:
    content_parts = []
    for font, size, y, text in lines:
        t = pdf_escape(text)
        content_parts.append(f"BT /{font} {size} Tf {LEFT_MARGIN} {y} Td ({t}) Tj ET")
    content = "\n".join(content_parts).encode("latin-1", errors="replace")

    objects: list[bytes] = []

    def add_obj(data: bytes) -> int:
        objects.append(data)
        return len(objects)

    catalog = add_obj(b"<< /Type /Catalog /Pages 2 0 R >>")
    pages = add_obj(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    page = add_obj(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>"
    )
    font_regular = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    font_bold = add_obj(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    content_stream = add_obj(
        b"<< /Length " + str(len(content)).encode("ascii") + b" >>\nstream\n" + content + b"\nendstream"
    )

    assert [catalog, pages, page, font_regular, font_bold, content_stream] == [1, 2, 3, 4, 5, 6]

    out = bytearray()
    out.extend(b"%PDF-1.4\n")

    offsets = [0]
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii"))
        out.extend(obj)
        out.extend(b"\nendobj\n")

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))

    out.extend(
        (
            "trailer\n"
            f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            "startxref\n"
            f"{xref_pos}\n"
            "%%EOF\n"
        ).encode("ascii")
    )

    return bytes(out)


def main() -> None:
    lines = build_lines()
    pdf_bytes = build_pdf(lines)

    out_path = Path("output/pdf/bonibuddy-app-summary.pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(pdf_bytes)
    print(out_path.resolve())


if __name__ == "__main__":
    main()
