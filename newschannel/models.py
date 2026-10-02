from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Story:
    id: str
    title: str
    summary: str
    link: str
    source: str
    published: float = 0.0  # unix seconds


@dataclass
class Topic:
    """One news event, possibly reported by several outlets."""
    title: str
    stories: list[Story] = field(default_factory=list)
    why: str = ""

    @property
    def sources(self) -> list[str]:
        seen: list[str] = []
        for s in self.stories:
            if s.source not in seen:
                seen.append(s.source)
        return seen

    @property
    def slug(self) -> str:
        import hashlib
        return hashlib.sha1(self.title.encode()).hexdigest()[:8]


@dataclass
class Scene:
    narration: str            # Hindi, spoken
    headline: str             # Hindi, <= ~55 chars, shown on screen
    visual_query: str = ""    # generic English stock-photo query (never a person's name)
    label: str = ""           # small tag on the strap, e.g. "ताज़ा खबर", "विश्लेषण"
    kind: str = "news"        # news | analysis | outro | clip
    clip_id: int | None = None  # for kind == "clip": index of the supplied video clip


@dataclass
class Script:
    title: str
    description: str
    tags: list[str]
    scenes: list[Scene]
    sources: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return sum(len(s.narration.split()) for s in self.scenes)


@dataclass
class Word:
    text: str
    start: float
    end: float


@dataclass
class SceneAudio:
    path: str
    duration: float
    words: list[Word]
    lead: float = 0.25        # silence before / after (0 for original clip audio)
    tail: float = 0.45


@dataclass
class Asset:
    id: str
    path: str
    kind: str                 # user | stock | graphic
    width: int = 0
    height: int = 0
    caption: str = ""
    credit: str = ""


@dataclass
class Shot:
    asset_id: str
    scene: int
    start: float = 0.0
    end: float = 0.0
    motion: str = "zoom_in"   # zoom_in | zoom_out | pan_left | pan_right | still
    focus_x: float = 0.5
    focus_y: float = 0.5


def to_json(obj: Any) -> str:
    return json.dumps(asdict(obj) if hasattr(obj, "__dataclass_fields__") else obj,
                      ensure_ascii=False, indent=2)


def save_json(path: Path, obj: Any) -> None:
    path.write_text(to_json(obj), encoding="utf-8")


def script_from_dict(d: dict) -> Script:
    return Script(
        title=d["title"], description=d.get("description", ""), tags=list(d.get("tags", [])),
        scenes=[Scene(**s) for s in d["scenes"]], sources=list(d.get("sources", [])),
    )
