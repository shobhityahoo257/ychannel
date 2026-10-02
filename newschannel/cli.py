from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import curate as C
from . import sources
from .config import Config
from .llm import make_client
from .monetization import ypp_progress
from .daily import notify
from .pipeline import manual_topic, produce
from .review import Store, Telegram
from .tts import make_tts


def _client(cfg: Config, required: bool = True):
    return make_client(cfg, required)


def cmd_doctor(cfg: Config, a) -> int:
    import shutil
    from PIL import features
    has = lambda k: bool(Config.env(k))  # noqa: E731
    llm_ok = has("ANTHROPIC_API_KEY") or has("OPENAI_API_KEY")
    voice_ok = (has("ELEVENLABS_API_KEY") and has("ELEVENLABS_VOICE_ID")) or has("OPENAI_API_KEY")
    secret = Config.env("YOUTUBE_CLIENT_SECRETS")
    checks = [
        ("ffmpeg", bool(shutil.which("ffmpeg"))), ("ffprobe", bool(shutil.which("ffprobe"))),
        ("Hindi font", cfg.path("assets/fonts/NotoSansDevanagari-Bold.ttf").exists()),
        ("Pillow Raqm (Hindi text shaping)", features.check("raqm")),
        (f"Script/photo AI key (ANTHROPIC_API_KEY or OPENAI_API_KEY) -> using {cfg.llm_provider()}", llm_ok),
        (f"Voice key (ELEVENLABS_API_KEY+VOICE_ID or OPENAI_API_KEY) -> using {cfg.tts_provider()}", voice_ok),
        ("YouTube client secret (needed only to upload)", bool(secret and cfg.path(secret).exists())),
    ]
    for name, fine in checks:
        print(f"{'OK     ' if fine else 'MISSING'}  {name}")
    print(f"        optional: PEXELS_API_KEY {'set' if has('PEXELS_API_KEY') else 'not set'}, "
          f"Telegram {'set' if has('TELEGRAM_BOT_TOKEN') else 'not set'}")
    return 0 if all(f for n, f in checks if "YouTube" not in n) else 1


def cmd_demo(cfg: Config, a) -> int:
    from . import demo
    print("Rendering a keyless demo (placeholder pictures, silent audio)…")
    print("Video:", demo.run(cfg, a.format, Path(a.out) if a.out else None))
    return 0


def cmd_ui(cfg: Config, a) -> int:
    from .webui import serve
    serve(a.port, not a.no_browser, a.config)
    return 0


def cmd_fetch(cfg: Config, a) -> int:
    stories = sources.fetch_stories(cfg["feeds"], cfg["curation"]["max_age_hours"])
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    topics = C.curate(_client(cfg, False) if not a.offline else None, cfg.models()[0], stories,
                      cfg["curation"]["min_sources"], [h["topic"] for h in store.history()],
                      cfg["curation"]["candidates_for_llm"])
    for i, t in enumerate(topics):
        print(f"{i}. {t.title}  ({', '.join(t.sources)})")
    return 0


def cmd_daily(cfg: Config, a) -> int:
    from .daily import run_daily
    for m in run_daily(cfg):
        print(f"-> {m['video']}  [{m['status']}]")
    return 0


def cmd_make(cfg: Config, a) -> int:
    text = Path(a.text_file).read_text(encoding="utf-8") if a.text_file else (a.text or "")
    topic = manual_topic(a.headline, text)
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    meta = produce(cfg, topic, a.format, _client(cfg), make_tts(cfg), store,
                   [Path(p) for p in (a.images or [])], clip_folders=[Path(p) for p in (a.clips or [])])
    notify(cfg, meta)
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
    from .daily import publish_one
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    for m in store.runs("approved"):
        if m.get("video_id"):
            continue
        print(f"published https://youtu.be/{publish_one(cfg, store, m, a.publish_at)}")
    return 0


def cmd_insights(cfg: Config, a) -> int:
    from . import learn
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    if a.sync:
        print(f"synced stats for {learn.sync(store)} videos")
    ins = learn.insights(store)
    print(f"Videos with enough data: {ins['rated']} of {ins['total']} (need {learn.MIN_VIDEOS}+ for advice)")
    for fmt, d in ins["by_format"].items():
        print(f"  {fmt}: avg retention {d['avg_retention_pct']}%, avg views {d['avg_views']} ({d['videos']} videos)")
    print(learn.prompt_hint(store) or "No advice yet - publish more videos and run again with --sync.")
    return 0


def cmd_breaking(cfg: Config, a) -> int:
    from . import breaking
    if not a.once:
        breaking.watch(cfg)
        return 0
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    w = breaking.BreakingWatcher(cfg, _client(cfg, required=not a.dry_run), store,
                                 make=(lambda t: {"id": ""}) if a.dry_run else None)
    if a.dry_run:
        for t in w.detect():
            print(f"WOULD ALERT: {t.title}  ({', '.join(t.sources)}; importance {t.importance})")
        return 0
    for m in w.run_once():
        print("->", m["video"])
    return 0


def cmd_schedule(cfg: Config, a) -> int:
    from .scheduler import run_forever
    run_forever(cfg)
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
    ui = sub.add_parser("ui", help="open the point-and-click web app in your browser")
    ui.add_argument("--port", type=int, default=8765); ui.add_argument("--no-browser", action="store_true")
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
    ins = sub.add_parser("insights", help="what worked on your channel (add --sync to refresh from YouTube)")
    ins.add_argument("--sync", action="store_true")
    br = sub.add_parser("breaking", help="watch for breaking political news and prepare a Short for approval")
    br.add_argument("--once", action="store_true", help="check once and exit (for cron)")
    br.add_argument("--dry-run", action="store_true", help="with --once: only show what would trigger")
    sub.add_parser("schedule", help="run unattended: produce, collect approvals and publish at set times")
    a = ap.parse_args(argv)
    cfg = Config.load(a.config)
    return {"ui": cmd_ui, "doctor": cmd_doctor, "demo": cmd_demo, "fetch": cmd_fetch, "daily": cmd_daily, "make": cmd_make,
            "review": cmd_review, "publish": cmd_publish, "stats": cmd_stats, "insights": cmd_insights, "schedule": cmd_schedule, "breaking": cmd_breaking}[a.cmd](cfg, a)


if __name__ == "__main__":
    sys.exit(main())
