"""Shared routines used by the CLI, the web UI and the scheduler."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import curate as C
from . import sources
from .config import Config
from .llm import make_client
from .pipeline import produce
from .review import Store, Telegram
from .tts import make_tts


def notify(cfg: Config, meta: dict[str, Any]) -> None:
    tg = Telegram()
    if cfg["review"].get("telegram") and tg.enabled:
        try:
            tg.send_for_review(meta, Path(meta["video"]))
        except Exception as exc:       # a Telegram hiccup must not lose the finished video
            print(f"[telegram] could not send preview: {exc}")


def run_daily(cfg: Config, log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    """Fetch -> curate -> produce the day's Shorts + long video. Returns the metas produced."""
    client, tts = make_client(cfg), make_tts(cfg)
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    stories = sources.fetch_stories(cfg["feeds"], cfg["curation"]["max_age_hours"])
    topics = C.curate(client, cfg.models()[0], stories, cfg["curation"]["min_sources"],
                      [h["topic"] for h in store.history()], cfg["curation"]["candidates_for_llm"])
    if not topics:
        log("No sufficiently corroborated stories right now.")
        return []
    plan = ["short"] * cfg["daily"]["shorts"] + ["long"] * cfg["daily"]["long"]
    made = []
    for i, fmt in enumerate(plan):
        topic = topics[i % len(topics)] if fmt == "short" else topics[0]
        log(f"=== {fmt}: {topic.title}")
        try:
            meta = produce(cfg, topic, fmt, client, tts, store, log=log)
        except Exception as exc:       # one failure must not kill the rest of the batch
            log(f"FAILED: {exc}")
            continue
        notify(cfg, meta)
        made.append(meta)
    return made


def publish_one(cfg: Config, store: Store, meta: dict[str, Any], publish_at: str | None = None) -> str:
    """Upload one approved video; records it in history (used for de-duplication and analytics)."""
    from .youtube import upload
    vid = upload(Path(meta["video"]), Path(meta["thumbnail"]), meta, cfg["youtube"],
                 meta["format"] == "short", publish_at)
    store.set_status(meta["id"], "published", video_id=vid)
    store.add_history(meta["title"], meta["topic"], vid, hook=meta.get("hook", ""), format=meta["format"],
                      duration=meta.get("duration"), run_id=meta["id"])
    return vid


def next_approved(store: Store) -> dict[str, Any] | None:
    for m in store.runs("approved"):
        if not m.get("video_id") and not any(i["level"] == "block" for i in m.get("issues", [])):
            return m
    return None
