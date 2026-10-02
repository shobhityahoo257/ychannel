from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance

from .render import Brand, font, put, wrap


def make_thumbnail(photo: str, headline: str, brand: Brand, out: Path) -> Path:
    W, H = 1280, 720
    im = Image.open(photo).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    im = im.crop(((im.width - W) // 2, (im.height - H) // 2, (im.width + W) // 2, (im.height + H) // 2))
    im = ImageEnhance.Contrast(im).enhance(1.15)
    im = ImageEnhance.Color(im).enhance(1.15).convert("RGBA")
    shade = Image.new("RGBA", (W, H))
    sd = ImageDraw.Draw(shade)
    for x in range(W):                      # dark gradient from the left for legible text
        sd.line((x, 0, x, H), fill=(0, 0, 0, int(215 * max(0.0, 1 - x / (W * 0.85)))))
    im = Image.alpha_composite(im, shade)
    d = ImageDraw.Draw(im)
    f = font(brand.font_bold, 104)
    lines = wrap(headline, f, int(W * 0.62))[:3]
    y = H - 70 - len(lines) * 128
    for line in lines:
        put(d, (50, y), line, font=f, fill=(255, 255, 255), stroke_width=7, stroke_fill=(0, 0, 0))
        y += 128
    tag = font(brand.font_bold, 46)
    tw = int(tag.getlength("ताज़ा खबर")) + 50
    d.rectangle((0, 40, tw, 110), fill=brand.accent)
    put(d, (25, 75), "ताज़ा खबर", font=tag, fill=(255, 255, 255), anchor="lm")
    im.convert("RGB").save(out, quality=90)
    return out


def make_variants(photos: list[str], texts: list[str], brand: Brand, folder: Path, default_text: str) -> list[dict]:
    """Up to 3 thumbnails, each with a different photo and text, so you can pick (or A/B test)."""
    texts = texts or [default_text]
    out = []
    for i, text in enumerate(texts[:3]):
        photo = photos[i % len(photos)]
        path = make_thumbnail(photo, text, brand, folder / f"thumbnail_{i + 1}.jpg")
        out.append({"file": str(path), "text": text})
    return out
