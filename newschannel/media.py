from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path

import requests
from PIL import Image, ImageOps

from .config import Config
from .models import Asset

EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_SIDE = 2600
UA = {"User-Agent": "NewsChannelBot/0.1 (contact: channel owner)"}


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


# ---------------------------------------------------------------- stock photos
def _download(url: str) -> bytes:
    r = requests.get(url, headers=UA, timeout=40)
    r.raise_for_status()
    return r.content


def search_pexels(query: str, n: int = 3) -> list[tuple[str, str, str]]:
    key = Config.env("PEXELS_API_KEY")
    if not key:
        return []
    r = requests.get("https://api.pexels.com/v1/search", headers={"Authorization": key},
                     params={"query": query, "per_page": n, "orientation": "landscape"}, timeout=20)
    r.raise_for_status()
    return [(p["src"]["large2x"], p.get("alt", query), f"Photo by {p['photographer']} on Pexels")
            for p in r.json().get("photos", [])]


_OK_LICENSE = re.compile(r"^(cc0|public domain|pd|cc[- ]by(-sa)?[- ]\d)", re.I)


def search_wikimedia(query: str, n: int = 3) -> list[tuple[str, str, str]]:
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers=UA, timeout=20, params={
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": 15, "prop": "imageinfo",
        "iiprop": "url|size|extmetadata", "iiurlwidth": 2000})
    r.raise_for_status()
    out = []
    for page in (r.json().get("query", {}).get("pages", {})).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        lic = meta.get("LicenseShortName", {}).get("value", "")
        if not _OK_LICENSE.match(lic) or info.get("width", 0) < 1400:
            continue
        artist = re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", "unknown"))
        out.append((info.get("thumburl") or info["url"], page["title"].replace("File:", ""),
                    f"{artist} / Wikimedia Commons ({lic})"))
        if len(out) >= n:
            break
    return out


def fetch_stock(query: str, dest: Path, min_side: int, n: int = 2) -> list[Asset]:
    got: list[Asset] = []
    for search in (search_pexels, search_wikimedia):
        try:
            hits = search(query, n)
        except Exception as exc:
            print(f"[stock] {search.__name__} failed: {exc}")
            continue
        for url, caption, credit in hits:
            try:
                tmp = dest / f"_dl_{hashlib.sha1(url.encode()).hexdigest()[:8]}"
                dest.mkdir(parents=True, exist_ok=True)
                tmp.write_bytes(_download(url))
                a = import_image(tmp, dest, "stock", caption, credit, min_side)
                tmp.unlink(missing_ok=True)
                if a:
                    got.append(a)
            except Exception as exc:
                print(f"[stock] download failed: {exc}")
        if len(got) >= n:
            break
    return got[:n]
