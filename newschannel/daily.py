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
    """Upload one approved video, file it into playlists, add 'watch next' links, and record it in
    history (used for de-duplication, analytics and end-screen suggestions). Playlist problems never
    undo the upload; they are recorded in meta['playlist_error']."""
    from . import playlists as P
    from .youtube import service, upload

    pl = cfg.get("playlists", {})
    history = store.history()
    send = dict(meta)
    pls, pids, names = None, [], []
    if pl.get("enabled", True):
        try:
            pls = P.Playlists(service(), store.root / "playlists.json")
            names = P.target_playlists(meta, pl)
            pids = [pls.ensure(n, privacy=pl.get("privacy", "public")) for n in names]
            rel = P.related(history, meta.get("category", ""), n=pl.get("watch_next_links", 3))
            extra = P.watch_next_block(rel, [P.playlist_url(p) for p in pids[:1]])
            if extra:
                send["description"] = (meta["description"] + "\n\n" + extra)[:4900]
        except Exception as exc:
            print(f"[playlists] skipped before upload: {exc}")
            pls, pids = None, []
    vid = upload(Path(meta["video"]), Path(meta["thumbnail"]), send, cfg["youtube"],
                 meta["format"] == "short", publish_at)
    problems: list[str] = []
    if pls:
        for n, pid in zip(names, pids):
            try:
                pls.add(pid, vid)
            except Exception as exc:
                problems.append(f"could not add to playlist '{n}': {exc}")
        if pl.get("link_previous", True) and meta.get("category"):
            prev = [h for h in P.related(history, meta["category"], n=5) if h.get("category") == meta["category"]][:1]
            for h in prev:
                try:
                    pls.append_description(h["video_id"], f"▶ Next / अगला: {meta['title']} {P.watch_url(vid)}")
                except Exception as exc:
                    problems.append(f"could not link previous video: {exc}")
    store.set_status(meta["id"], "published", video_id=vid, playlists=names, playlist_error="; ".join(problems))
    store.add_history(meta["title"], meta["topic"], vid, hook=meta.get("hook", ""), format=meta["format"],
                      duration=meta.get("duration"), run_id=meta["id"], category=meta.get("category", ""),
                      playlists=names)
    return vid


def next_approved(store: Store) -> dict[str, Any] | None:
    for m in store.runs("approved"):
        if not m.get("video_id") and not any(i["level"] == "block" for i in m.get("issues", [])):
            return m
    return None
