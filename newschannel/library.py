"""Photo inventory: every photo you add is kept here, de-duplicated, searchable and reusable.

library/
  library.json          index (caption, tags, credit, usage)
  images/<id>.jpg       normalised full-size copy (EXIF-rotated, max 2600px)
  thumbs/<id>.jpg       small preview for the UI and for AI tagging
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from .llm import call_tool
from .media import EXTS, MAX_SIDE, thumb_b64
from .models import Asset

THUMB = 360
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass
class Entry:
    id: str
    file: str                 # relative to the library folder
    caption: str = ""
    tags: list[str] = field(default_factory=list)
    credit: str = ""          # shown in the video description (photographer / agency)
    source: str = "user"      # user | stock
    width: int = 0
    height: int = 0
    ahash: str = ""           # 64-bit average hash, for duplicate detection
    added: float = 0.0
    used: int = 0
    last_used: float = 0.0
    ai_tagged: bool = False


def ahash(im: Image.Image) -> str:
    g = im.convert("L").resize((8, 8), Image.LANCZOS)
    px = list(g.tobytes())
    avg = sum(px) / 64
    return "".join("1" if p >= avg else "0" for p in px)


def hamming(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b))


def words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


class Library:
    def __init__(self, root: Path):
        self.root = root
        self.index = root / "library.json"
        self._lock = threading.RLock()
        for d in ("images", "thumbs"):
            (root / d).mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- storage
    def _load(self) -> dict[str, Entry]:
        try:
            raw = json.loads(self.index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {e["id"]: Entry(**e) for e in raw}

    def _save(self, entries: dict[str, Entry]) -> None:
        tmp = self.index.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(e) for e in entries.values()], ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.index)

    def all(self) -> list[Entry]:
        with self._lock:
            return sorted(self._load().values(), key=lambda e: -e.added)

    def get(self, eid: str) -> Entry | None:
        with self._lock:
            return self._load().get(eid)

    def path(self, e: Entry) -> Path:
        return self.root / e.file

    def thumb_path(self, e: Entry) -> Path:
        return self.root / "thumbs" / f"{e.id}.jpg"

    # ---------------------------------------------------------------- adding
    def add_file(self, src: Path, caption: str = "", credit: str = "", source: str = "user",
                 tags: list[str] | None = None, min_side: int = 0) -> tuple[Entry | None, bool]:
        """Add a photo. Returns (entry, is_new). A near-duplicate returns the existing entry, is_new=False."""
        if src.suffix.lower() not in EXTS:
            return None, False
        try:
            with Image.open(src) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
        except Exception:
            return None, False
        if min(im.size) < min_side:
            return None, False
        h = ahash(im)
        with self._lock:
            entries = self._load()
            for e in entries.values():
                if hamming(e.ahash, h) <= 4:
                    changed = False                      # fill gaps in what we know, never overwrite
                    if caption and not e.caption:
                        e.caption, changed = caption, True
                    if credit and not e.credit:
                        e.credit, changed = credit, True
                    if changed:
                        self._save(entries)
                    return e, False
            eid = uuid.uuid4().hex[:10]
            if max(im.size) > MAX_SIDE:
                im.thumbnail((MAX_SIDE, MAX_SIDE), Image.LANCZOS)
            rel = f"images/{eid}.jpg"
            im.save(self.root / rel, quality=93)
            th = im.copy()
            th.thumbnail((THUMB, THUMB))
            th.save(self.root / "thumbs" / f"{eid}.jpg", quality=82)
            cap = caption or re.sub(r"[_\-]+", " ", src.stem)
            e = Entry(eid, rel, cap if caption else "", [t.strip() for t in tags or [] if t.strip()], credit,
                      source, im.width, im.height, h, time.time())
            entries[eid] = e
            self._save(entries)
            return e, True

    def add_folder(self, folder: Path, min_side: int = 0) -> list[Entry]:
        caps: dict[str, str] = {}
        cf = folder / "captions.txt"
        if cf.exists():
            for line in cf.read_text(encoding="utf-8").splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    caps[k.strip().lower()] = v.strip()
        out = []
        for p in sorted(folder.iterdir()):
            e, _ = self.add_file(p, caps.get(p.name.lower(), ""), min_side=min_side)
            if e:
                out.append(e)
        return out

    # ---------------------------------------------------------------- editing
    def update(self, eid: str, caption: str | None = None, tags: list[str] | None = None,
               credit: str | None = None) -> Entry | None:
        with self._lock:
            entries = self._load()
            e = entries.get(eid)
            if not e:
                return None
            if caption is not None:
                e.caption = caption.strip()
            if tags is not None:
                e.tags = [t.strip() for t in tags if t.strip()]
            if credit is not None:
                e.credit = credit.strip()
            self._save(entries)
            return e

    def delete(self, eid: str) -> bool:
        with self._lock:
            entries = self._load()
            e = entries.pop(eid, None)
            if not e:
                return False
            self.path(e).unlink(missing_ok=True)
            self.thumb_path(e).unlink(missing_ok=True)
            self._save(entries)
            return True

    def mark_used(self, ids: list[str]) -> None:
        with self._lock:
            entries = self._load()
            for i in ids:
                if i in entries:
                    entries[i].used += 1
                    entries[i].last_used = time.time()
            self._save(entries)

    # ---------------------------------------------------------------- finding
    @staticmethod
    def text_of(e: Entry) -> str:
        return f"{e.caption} {' '.join(e.tags)}"

    def search(self, q: str = "", source: str = "") -> list[Entry]:
        items = [e for e in self.all() if not source or e.source == source]
        qw = words(q)
        if not qw:
            return items
        scored = [(len(qw & words(self.text_of(e))), e) for e in items]
        return [e for s, e in sorted(scored, key=lambda x: -x[0]) if s > 0]

    def to_asset(self, e: Entry, dest: Path) -> Asset:
        """Copy a library photo into a run folder as a normal Asset (so the library is never modified by a render)."""
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"lib_{e.id}.jpg"
        shutil.copyfile(self.path(e), out)
        return Asset(id=e.id, path=str(out), kind="user" if e.source == "user" else "stock",
                     width=e.width, height=e.height, caption=self.text_of(e).strip(), credit=e.credit)

    def suggest(self, client: Any, model: str, story_text: str, n: int, exclude: set[str] | None = None) -> list[Entry]:
        """Photos from the library that fit this story. Prefers well-described, less-used photos."""
        exclude = exclude or set()
        pool = [e for e in self.all() if e.id not in exclude and self.text_of(e).strip()]
        if not pool or n <= 0:
            return []
        sw = words(story_text)
        scored = sorted(pool, key=lambda e: (-len(sw & words(self.text_of(e))), e.used, -e.added))
        cands = scored[:60]
        if client is not None:
            try:
                listing = "\n".join(f"{e.id} | {self.text_of(e)[:160]}" for e in cands)
                res = call_tool(client, model, SUGGEST_SYSTEM,
                                f"Story:\n{story_text[:1500]}\n\nPick up to {n} photo ids.\n\nPhotos:\n{listing}",
                                "pick_photos", SUGGEST_SCHEMA, 600)
                by_id = {e.id: e for e in cands}
                picked = [by_id[i] for i in res.get("ids", []) if i in by_id][:n]
                if picked:
                    return picked
            except Exception as exc:
                print(f"[library] AI suggestion failed, using keyword match: {exc}")
        return [e for e in cands if sw & words(self.text_of(e))][:n]

    # ---------------------------------------------------------------- AI descriptions
    def autotag(self, client: Any, model: str, limit: int = 24, batch: int = 6) -> int:
        """Describe photos that have no caption yet. Describes what is VISIBLE only; never names people."""
        todo = [e for e in self.all() if not e.ai_tagged and not e.caption][:limit]
        done = 0
        for i in range(0, len(todo), batch):
            group = todo[i:i + batch]
            content: list[dict] = [{"type": "text", "text": "Describe each photo."}]
            for e in group:
                content.append({"type": "text", "text": f"\nphoto id={e.id}"})
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                            "data": thumb_b64(str(self.thumb_path(e)), THUMB)}})
            try:
                res = call_tool(client, model, TAG_SYSTEM, content, "describe_photos", TAG_SCHEMA, 1500)
            except Exception as exc:
                print(f"[library] auto-tag failed: {exc}")
                break
            with self._lock:
                entries = self._load()
                for d in res.get("photos", []):
                    e = entries.get(d.get("id", ""))
                    if e and not e.caption:
                        e.caption = d.get("caption", "").strip()
                        e.tags = [t.strip().lower() for t in d.get("tags", []) if t.strip()][:8]
                        e.ai_tagged = True
                        done += 1
                self._save(entries)
        return done


SUGGEST_SYSTEM = """You pick photos for a news video. Choose only photos whose description clearly fits the story;
fewer is better than irrelevant. Never choose a photo that could mislead (e.g. a different person or event). Return ids only."""
SUGGEST_SCHEMA = {"type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
                  "required": ["ids"]}
TAG_SYSTEM = """Describe each news photo in one short neutral English sentence (setting, objects, crowd, text on
signs, time of day) and 3-8 lowercase search tags. Describe only what is visible. NEVER identify or name real people
from their faces; say "a man in a white kurta at a podium" instead. No guessing of events or locations unless a sign says so."""
TAG_SCHEMA = {"type": "object", "properties": {"photos": {"type": "array", "items": {
    "type": "object", "properties": {"id": {"type": "string"}, "caption": {"type": "string"},
                                     "tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["id", "caption", "tags"]}}}, "required": ["photos"]}
