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


def sources_card(ledger: Ledger, spec: dict[str, Any], brand: Brand, lang: str, W: int, H: int, out: Path) -> Path:
    """Outlet names + headlines behind the claims: shows the reporting without using anyone's photographs."""
    srcs = []
    for cid_ in spec["claim_ids"]:
        c = ledger.claim(cid_)
        for sid in (c.source_ids if c else []):
            s = ledger.source(sid)
            if s and s.outlet != "user notes" and s not in srcs:
                srcs.append(s)
    srcs = sorted(srcs, key=lambda s: s.tier)[:4]
    if not srcs:
        raise ValueError("no sources behind these claims")
    im = _background(W, H, brand)
    d = ImageDraw.Draw(im)
    put(d, (W * 0.08, H * 0.10), spec.get("label") or t(lang, "sources_title"), font=font(brand.font_bold, int(H * 0.065)),
        fill=(255, 255, 255), anchor="lm")
    top, step = H * 0.22, (H * 0.70) / max(1, len(srcs))
    hf, nf, tf = font(brand.font_bold, int(H * 0.048)), font(brand.font_bold, int(H * 0.036)), font(brand.font_regular, int(H * 0.034))
    for i, s in enumerate(srcs):
        y = top + i * step
        d.rectangle((W * 0.08, y, W * 0.08 + 8, y + step * 0.78), fill=brand.accent if s.tier == 1 else (120, 130, 160))
        put(d, (W * 0.11, y + 4), friendly(s.outlet), font=hf, fill=(255, 255, 255), anchor="la")
        tier = t(lang, "tier1") if s.tier == 1 else t(lang, "tier2") if s.tier == 2 else t(lang, "tier3")
        put(d, (W * 0.92, y + 10), tier, font=nf, fill=brand.accent if s.tier == 1 else (190, 196, 215), anchor="ra")
        title = re.sub(r"\s*[|\-–]\s*[^|\-–]{2,30}$", "", s.title).strip()
        for k, ln in enumerate(wrap(title, tf, int(W * 0.8))[:2]):
            put(d, (W * 0.11, y + H * 0.075 + k * H * 0.045), ln, font=tf, fill=(220, 225, 240), anchor="la")
        if s.published:
            put(d, (W * 0.11, y + H * 0.17), s.published[:10], font=font(brand.font_regular, int(H * 0.03)), fill=(160, 168, 190), anchor="la")
    im.save(out, quality=95)
    return out


def _tick(lo: float, hi: float, n: int = 4) -> list[float]:
    """Round-numbered gridlines between lo and hi."""
    import math
    span = (hi - lo) or abs(hi) or 1.0
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.floor(lo / step) * step
    out, v = [], first
    while v <= hi + step * 0.01:
        out.append(round(v, 10))
        v += step
    return out


def _fmt_val(v: float, unit: str) -> str:
    if unit == "pct":
        return f"{v:g}%"
    if unit in ("usd", "people"):
        for div, word in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
            if abs(v) >= div:
                return f"{'$' if unit == 'usd' else ''}{v / div:g}{word}"
        return f"{'$' if unit == 'usd' else ''}{v:g}"
    return f"{v:g}"


def chart_card(ledger: Ledger, spec: dict[str, Any], brand: Brand, lang: str, W: int, H: int, out: Path) -> Path:
    """Line chart drawn straight from the World Bank series behind the cited claims (up to 3 countries, same indicator)."""
    from .deep import chart_series
    srcs = chart_series(ledger, [c for c in (ledger.claim(i) for i in spec["claim_ids"]) if c])
    if not srcs:
        raise ValueError("no data series behind these claims")
    first = srcs[0].series
    im = _background(W, H, brand)
    d = ImageDraw.Draw(im)
    put(d, (W * 0.08, H * 0.09), spec.get("label") or first["label"], font=font(brand.font_bold, int(H * 0.058)),
        fill=(255, 255, 255), anchor="lm")
    put(d, (W * 0.92, H * 0.045), t(lang, "ex_data").upper(), font=font(brand.font_bold, int(H * 0.03)), fill=brand.accent, anchor="ra")
    left, right, top, bottom = W * 0.12, W * 0.92, H * 0.22, H * 0.80
    pts_all = [p for s in srcs for p in s.series["points"]]
    x0, x1 = min(p[0] for p in pts_all), max(p[0] for p in pts_all)
    lo, hi = min(p[1] for p in pts_all), max(p[1] for p in pts_all)
    if first["unit"] in ("pct", "usd", "people") and lo > 0 and lo < hi * 0.4:
        lo = 0.0
    lo, hi = min(lo, 0.0) if lo < 0 else lo, max(hi, 0.0) if hi < 0 else hi
    ticks = _tick(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def X(x: float) -> float:
        return left + (right - left) * ((x - x0) / ((x1 - x0) or 1))

    def Y(v: float) -> float:
        return bottom - (bottom - top) * ((v - lo) / ((hi - lo) or 1))

    gf = font(brand.font_regular, int(H * 0.030))
    for tv in ticks:
        y = Y(tv)
        d.line((left, y, right, y), fill=(70, 80, 110) if tv else (150, 160, 190), width=2 if tv == 0 else 1)
        put(d, (left - 14, y), _fmt_val(tv, first["unit"]), font=gf, fill=(190, 198, 220), anchor="rm")
    years = sorted({p[0] for p in pts_all})
    for yr in years[::max(1, len(years) // 7)]:
        put(d, (X(yr), bottom + 14), str(yr), font=gf, fill=(190, 198, 220), anchor="ma")
    palette = [brand.accent, (88, 196, 255), (255, 200, 87)]
    lf = font(brand.font_bold, int(H * 0.036))
    for k, s in enumerate(srcs):
        col = palette[k % len(palette)]
        xy = [(X(x), Y(v)) for x, v in s.series["points"]]
        d.line(xy, fill=col, width=max(4, int(H * 0.007)), joint="curve")
        d.ellipse((xy[-1][0] - 9, xy[-1][1] - 9, xy[-1][0] + 9, xy[-1][1] + 9), fill=col)
        put(d, (xy[-1][0] - 16, xy[-1][1] - 34), _fmt_val(s.series["points"][-1][1], first["unit"]), font=lf, fill=col, anchor="rs")
        if len(srcs) > 1:
            lx = left + k * W * 0.2
            d.rectangle((lx, H * 0.155, lx + 28, H * 0.155 + 10), fill=col)
            put(d, (lx + 40, H * 0.16), s.series["country"], font=gf, fill=(235, 238, 248), anchor="lm")
    src = " · ".join(dict.fromkeys(friendly(s.outlet) for s in srcs))
    put(d, (W * 0.08, H * 0.93), f"{t(lang, 'source')}: {src}" + (f"  ·  {srcs[0].published}" if srcs[0].published else ""),
        font=font(brand.font_regular, int(H * 0.03)), fill=(200, 205, 220), anchor="lm")
    im.save(out, quality=95)
    return out


BUILDERS = {"quote": quote_card, "number": number_card, "timeline": timeline_card, "sources": sources_card,
            "chart": chart_card}


def build_card(ledger: Ledger, spec: dict[str, Any], brand: Brand, lang: str, W: int, H: int, out: Path) -> Path | None:
    """Render a card; returns None (no card) if the ledger cannot support it."""
    try:
        return BUILDERS[spec["type"]](ledger, spec, brand, lang, W, H, out)
    except Exception as exc:
        print(f"[cards] skipped {spec.get('type')} card: {exc}")
        return None
