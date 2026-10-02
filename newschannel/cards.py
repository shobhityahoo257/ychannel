"""On-screen graphic cards for analysis videos. Content comes from the fact ledger (never from the AI's
free text), so a quote, number or date shown on screen is exactly what the source says."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw

from .i18n import t
from .ledger import Ledger, friendly
from .render import Brand, font, put, wrap

_NUM = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(%|percent|per cent|crore|lakh|billion|million|thousand|trillion)?", re.I)


def _background(W: int, H: int, brand: Brand) -> Image.Image:
    arr = np.zeros((H, W, 3), np.uint8)
    g = np.linspace(1.0, 0.3, H)[:, None]
    for c in range(3):
        arr[..., c] = (brand.dark[c] * g * 1.15).clip(0, 255)
    return Image.fromarray(arr)


def _source_line(ledger: Ledger, claims: list, lang: str) -> str:
    outs: list[str] = []
    for c in claims:
        for o in ledger.outlets_for(c):
            n = friendly(o)
            if n not in outs and o != "user notes":
                outs.append(n)
    date = next((c.date for c in claims if c.date), "")
    return f"{t(lang, 'source')}: {' · '.join(outs[:3])}" + (f"  ·  {date}" if date else "")


def quote_card(ledger: Ledger, spec: dict[str, Any], brand: Brand, lang: str, W: int, H: int, out: Path) -> Path:
    c = ledger.claim(spec["claim_ids"][0])
    im = _background(W, H, brand)
    d = ImageDraw.Draw(im)
    put(d, (W * 0.08, H * 0.16), "“", font=font(brand.font_bold, int(H * 0.30)), fill=brand.accent, anchor="la")
    text = c.quote.strip()
    size = int(H * (0.066 if len(text) < 140 else 0.054 if len(text) < 260 else 0.044))
    f = font(brand.font_bold, size)
    lines = wrap(text, f, int(W * 0.78))[:7]
    y = H * 0.30
    for ln in lines:
        put(d, (W * 0.12, y), ln, font=f, fill=(255, 255, 255), anchor="la")
        y += size * 1.35
    d.rectangle((W * 0.12, y + 14, W * 0.12 + W * 0.10, y + 20), fill=brand.accent)
    put(d, (W * 0.12, y + 40), c.speaker, font=font(brand.font_bold, int(H * 0.05)), fill=(255, 255, 255), anchor="la")
    put(d, (W * 0.12, H * 0.90), _source_line(ledger, [c], lang), font=font(brand.font_regular, int(H * 0.034)),
        fill=(200, 205, 220), anchor="la")
    put(d, (W * 0.92, H * 0.08), t(lang, "quote").upper(), font=font(brand.font_bold, int(H * 0.034)),
        fill=brand.accent, anchor="ra")
    im.save(out, quality=95)
    return out


def _headline_number(claim) -> str:
    m = _NUM.search(claim.text)
    if m:
        unit = (m.group(2) or "").strip()
        return f"{m.group(1)}{'%' if unit.lower() in ('%', 'percent', 'per cent') else (' ' + unit if unit else '')}"
    return claim.numbers[0] if claim.numbers else ""


def number_card(ledger: Ledger, spec: dict[str, Any], brand: Brand, lang: str, W: int, H: int, out: Path) -> Path:
    c = next(ledger.claim(i) for i in spec["claim_ids"] if ledger.claim(i) and ledger.claim(i).numbers)
    im = _background(W, H, brand)
    d = ImageDraw.Draw(im)
    put(d, (W * 0.92, H * 0.08), t(lang, "numbers").upper(), font=font(brand.font_bold, int(H * 0.034)),
        fill=brand.accent, anchor="ra")
    big = _headline_number(c)
    size = int(H * (0.30 if len(big) <= 6 else 0.22 if len(big) <= 10 else 0.16))
    put(d, (W / 2, H * 0.42), big, font=font(brand.font_bold, size), fill=(255, 255, 255), anchor="mm")
    d.rectangle((W / 2 - W * 0.06, H * 0.62, W / 2 + W * 0.06, H * 0.62 + 7), fill=brand.accent)
    label = spec.get("label") or c.text
    lf = font(brand.font_bold, int(H * 0.05))
    y = H * 0.68
    for ln in wrap(label, lf, int(W * 0.7))[:3]:
        put(d, (W / 2, y), ln, font=lf, fill=(235, 238, 248), anchor="ma")
        y += H * 0.065
    put(d, (W / 2, H * 0.91), _source_line(ledger, [c], lang), font=font(brand.font_regular, int(H * 0.034)),
        fill=(200, 205, 220), anchor="ma")
    im.save(out, quality=95)
    return out


def timeline_card(ledger: Ledger, spec: dict[str, Any], brand: Brand, lang: str, W: int, H: int, out: Path) -> Path:
    cs = [c for c in (ledger.claim(i) for i in spec["claim_ids"]) if c and c.date][:6]
    im = _background(W, H, brand)
    d = ImageDraw.Draw(im)
    put(d, (W * 0.08, H * 0.09), t(lang, "timeline").upper(), font=font(brand.font_bold, int(H * 0.06)),
        fill=(255, 255, 255), anchor="lm")
    top, step = H * 0.20, (H * 0.68) / max(1, len(cs))
    x_line = W * 0.27
    d.line((x_line, top, x_line, top + step * (len(cs) - 0.4)), fill=brand.accent, width=5)
    df, tf = font(brand.font_bold, int(H * 0.043)), font(brand.font_regular, int(H * 0.040))
    for i, c in enumerate(cs):
        y = top + i * step
        d.ellipse((x_line - 13, y + 8, x_line + 13, y + 34), fill=brand.accent)
        put(d, (x_line - 40, y + 21), c.date, font=df, fill=(255, 255, 255), anchor="rm")
        ev = c.text if len(c.text) <= 90 else c.text[:87] + "..."
        for k, ln in enumerate(wrap(ev, tf, int(W * 0.58))[:2]):
            put(d, (x_line + 40, y + 4 + k * H * 0.05), ln, font=tf, fill=(225, 230, 242), anchor="la")
    put(d, (W * 0.08, H * 0.95), _source_line(ledger, cs, lang), font=font(brand.font_regular, int(H * 0.03)),
        fill=(200, 205, 220), anchor="lm")
    im.save(out, quality=95)
    return out


BUILDERS = {"quote": quote_card, "number": number_card, "timeline": timeline_card}


def build_card(ledger: Ledger, spec: dict[str, Any], brand: Brand, lang: str, W: int, H: int, out: Path) -> Path | None:
    """Render a card; returns None (no card) if the ledger cannot support it."""
    try:
        return BUILDERS[spec["type"]](ledger, spec, brand, lang, W, H, out)
    except Exception as exc:
        print(f"[cards] skipped {spec.get('type')} card: {exc}")
        return None
