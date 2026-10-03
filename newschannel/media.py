from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path

from PIL import Image, ImageOps

from .models import Asset

EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_SIDE = 2600


def _read_captions(folder: Path) -> dict[str, str]:
    """Optional `captions.txt` next to the images: one `filename: description` per line."""
    caps: dict[str, str] = {}
    f = folder / "captions.txt"
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                caps[k.strip().lower()] = v.strip()
    return caps


def import_image(src: Path, dest_dir: Path, kind: str, caption: str = "", credit: str = "",
                 min_side: int = 0) -> Asset | None:
    """Normalise orientation/size, reject unusable files, copy into the run folder."""
    try:
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
    except Exception:
        return None
    if min(im.size) < min_side:
        return None
    if max(im.size) > MAX_SIDE:
        im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
    aid = hashlib.sha1(f"{src}{im.size}".encode()).hexdigest()[:8]
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{kind}_{aid}.jpg"
    im.save(out, quality=93)
    return Asset(id=aid, path=str(out), kind=kind, width=im.width, height=im.height,
                 caption=caption or re.sub(r"[_\-]+", " ", src.stem), credit=credit,
                 explicit_caption=bool(caption))


def load_user_images(folders: list[Path], dest: Path, min_side: int) -> list[Asset]:
    assets: list[Asset] = []
    seen: set[str] = set()
    for folder in folders:
        if not folder.is_dir():
            continue
        caps = _read_captions(folder)
        for p in sorted(folder.iterdir()):
            if p.suffix.lower() not in EXTS or p.name in seen:
                continue
            seen.add(p.name)
            a = import_image(p, dest, "user", caps.get(p.name.lower(), ""), min_side=min_side)
            if a:
                assets.append(a)
    return assets


def thumb_b64(path: str, side: int = 640) -> str:
    with Image.open(path) as im:
        im.thumbnail((side, side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=80)
    return base64.b64encode(buf.getvalue()).decode()
