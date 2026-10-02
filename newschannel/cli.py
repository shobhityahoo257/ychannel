from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import curate as C
from . import sources
from .config import Config
from .llm import make_client
from .monetization import ypp_progress
from .pipeline import manual_topic, produce
from .review import Store, Telegram
from .tts import make_tts


def _client(cfg: Config, required: bool = True):
    key = Config.env("ANTHROPIC_API_KEY", required=required)
    return make_client(key) if key else None


def cmd_doctor(cfg: Config, a) -> int:
    import shutil
    ok = True
    for name, fine in [("ffmpeg", bool(shutil.which("ffmpeg"))), ("ffprobe", bool(shutil.which("ffprobe"))),
                       ("ANTHROPIC_API_KEY", bool(Config.env("ANTHROPIC_API_KEY"))),
                       ("ELEVENLABS_API_KEY", bool(Config.env("ELEVENLABS_API_KEY"))),
                       ("ELEVENLABS_VOICE_ID", bool(Config.env("ELEVENLABS_VOICE_ID"))),
                       ("PEXELS_API_KEY (optional)", True), ("Hindi font", cfg.path("assets/fonts/NotoSansDevanagari-Bold.ttf").exists()),
                       ("YouTube client secret", bool(Config.env("YOUTUBE_CLIENT_SECRETS") and
                                                      cfg.path(Config.env("YOUTUBE_CLIENT_SECRETS")).exists()))]:
        print(f"{'OK ' if fine else 'MISSING'}  {name}")
        ok &= fine
    from PIL import features
    print(f"{'OK ' if features.check('raqm') else 'MISSING'}  Pillow Raqm (Hindi text shaping)")
    return 0 if ok else 1


def cmd_demo(cfg: Config, a) -> int:
    from . import demo
    print("Rendering a keyless demo (placeholder pictures, silent audio)…")
    print("Video:", demo.run(cfg, a.format, Path(a.out) if a.out else None))
    return 0


def cmd_fetch(cfg: Config, a) -> int:
    stories = sources.fetch_stories(cfg["feeds"], cfg["curation"]["max_age_hours"])
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    topics = C.curate(_client(cfg, False) if not a.offline else None, cfg["llm"]["model"], stories,
                      cfg["curation"]["min_sources"], [h["topic"] for h in store.history()],
                      cfg["curation"]["candidates_for_llm"])
    for i, t in enumerate(topics):
        print(f"{i}. {t.title}  ({', '.join(t.sources)})")
    return 0


def _notify(store: Store, meta: dict, cfg: Config) -> None:
    tg = Telegram()
    if cfg["review"].get("telegram") and tg.enabled:
        tg.send_for_review(meta, Path(meta["video"]))


def cmd_daily(cfg: Config, a) -> int:
    client, tts = _client(cfg), make_tts(cfg)
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    stories = sources.fetch_stories(cfg["feeds"], cfg["curation"]["max_age_hours"])
    topics = C.curate(client, cfg["llm"]["model"], stories, cfg["curation"]["min_sources"],
                      [h["topic"] for h in store.history()], cfg["curation"]["candidates_for_llm"])
    plan = ["short"] * cfg["daily"]["shorts"] + ["long"] * cfg["daily"]["long"]
    if not topics:
        print("No sufficiently corroborated stories right now.")
        return 0
    for i, fmt in enumerate(plan):
        topic = topics[i % len(topics)] if fmt == "short" else topics[0]
        print(f"\n=== {fmt}: {topic.title}")
        try:
            meta = produce(cfg, topic, fmt, client, tts, store)
        except Exception as exc:   # one failure must not kill the rest of the day's batch
            print(f"FAILED: {exc}")
            continue
        _notify(store, meta, cfg)
        print(f"-> {meta['video']}  [{meta['status']}]")
    return 0


def cmd_make(cfg: Config, a) -> int:
    text = Path(a.text_file).read_text(encoding="utf-8") if a.text_file else (a.text or "")
    topic = manual_topic(a.headline, text)
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    meta = produce(cfg, topic, a.format, _client(cfg), make_tts(cfg), store,
                   [Path(p) for p in (a.images or [])], clip_folders=[Path(p) for p in (a.clips or [])])
    _notify(store, meta, cfg)
    print(meta["video"], meta["status"])
    return 0


def cmd_review(cfg: Config, a) -> int:
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    if a.action == "list":
        for m in store.runs():
            blocks = sum(1 for i in m.get("issues", []) if i["level"] == "block")
            print(f"{m['id']:<40} {m['status']:<10} {m['format']:<6} {m['duration']:>6}s  blocks={blocks}  {m['title']}")
    elif a.action in ("approve", "reject"):
        m = store.meta(a.id)
        if a.action == "approve" and any(i["level"] == "block" for i in m.get("issues", [])) and not a.force:
            print("Blocked by policy checks (see `review show`). Fix the content or use --force.")
            return 1
        store.set_status(a.id, "approved" if a.action == "approve" else "rejected")
    elif a.action == "show":
        import json
        print(json.dumps(store.meta(a.id), ensure_ascii=False, indent=2))
    elif a.action == "listen":
        Telegram().listen(store, a.seconds)
    return 0


def cmd_publish(cfg: Config, a) -> int:
    from .youtube import upload
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    for m in store.runs("approved"):
        if m.get("video_id"):
            continue
        vid = upload(Path(m["video"]), Path(m["thumbnail"]), m, cfg["youtube"], m["format"] == "short", a.publish_at)
        store.set_status(m["id"], "published", video_id=vid)
        store.add_history(m["title"], m["topic"], vid)
        print(f"published https://youtu.be/{vid}")
    return 0


def cmd_stats(cfg: Config, a) -> int:
    from .youtube import channel_stats
    s = channel_stats()
    print(f"Channel: {s['title']}  videos: {s['videos']}")
    print("\n".join(ypp_progress(s)))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="newschannel")
    ap.add_argument("--config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    dm = sub.add_parser("demo", help="render a sample video without any API keys")
    dm.add_argument("--format", choices=["short", "long"], default="short"); dm.add_argument("--out")
    f = sub.add_parser("fetch"); f.add_argument("--offline", action="store_true")
    sub.add_parser("daily")
    m = sub.add_parser("make", help="make one video from your own headline/text/photos")
    m.add_argument("--headline", required=True); m.add_argument("--text"); m.add_argument("--text-file")
    m.add_argument("--format", choices=["short", "long"], default="short")
    m.add_argument("--images", nargs="*", help="folders with your photos (AI picks and arranges them)")
    m.add_argument("--clips", nargs="*", help="folders with speech/video clips + clips.txt (trim, credit, subtitles)")
    r = sub.add_parser("review")
    r.add_argument("action", choices=["list", "show", "approve", "reject", "listen"])
    r.add_argument("id", nargs="?"); r.add_argument("--force", action="store_true")
    r.add_argument("--seconds", type=int, default=600)
    p = sub.add_parser("publish"); p.add_argument("--publish-at", help="RFC3339, e.g. 2026-10-03T07:30:00Z")
    sub.add_parser("stats")
    a = ap.parse_args(argv)
    cfg = Config.load(a.config)
    return {"doctor": cmd_doctor, "demo": cmd_demo, "fetch": cmd_fetch, "daily": cmd_daily, "make": cmd_make,
            "review": cmd_review, "publish": cmd_publish, "stats": cmd_stats}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
