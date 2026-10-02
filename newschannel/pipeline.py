from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import audio as A
from . import media, planner, render
from .config import Config
from .models import Asset, Script, Story, Topic, save_json, script_from_dict
from .monetization import policy_check
from .review import Store
from .scriptwriter import build_description, write_script
from .thumbnail import make_thumbnail
from .tts import synthesize_script

INTRO_SECONDS, OUTRO_SECONDS = 2.0, 4.0


def manual_topic(headline: str, text: str) -> Topic:
    sid = hashlib.sha1((headline + text).encode()).hexdigest()[:12]
    return Topic(title=headline, stories=[Story(sid, headline, text, "", "manual", time.time())])


def brand_from(cfg: Config) -> render.Brand:
    ch = cfg["channel"]
    fonts = cfg.path("assets/fonts")
    logo = str(cfg.path(ch["logo"])) if ch.get("logo") else ""
    return render.Brand(ch["name"], ch.get("handle", ""), logo, render.hex_rgb(ch["accent_color"]),
                        render.hex_rgb(ch["dark_color"]), str(fonts / "NotoSansDevanagari-Regular.ttf"),
                        str(fonts / "NotoSansDevanagari-Bold.ttf"))


def gather_assets(cfg: Config, topic: Topic, script: Script, run: Path, total_seconds: float,
                  extra: list[Path], log: Callable[[str], None]) -> list[Asset]:
    img = cfg["images"]
    inbox = cfg.path(img["inbox_dir"])
    folders = [*extra, inbox / topic.slug, inbox]
    assets = media.load_user_images(folders, run / "images", img["min_side_px"])
    log(f"user images: {len(assets)}")
    need = planner.needed_photos(total_seconds)
    if img.get("use_stock_fallback", True) and len(assets) < need:
        for sc in script.scenes:
            if len(assets) >= need:
                break
            if sc.visual_query:
                assets += media.fetch_stock(sc.visual_query, run / "images", img["min_side_px"], n=2)
        log(f"after stock fallback: {len(assets)}")
    if not assets:   # last resort: clean headline cards (clearly graphics, never fake photos)
        brand = brand_from(cfg)
        fmt_w, fmt_h = 1080, 1920
        for i, sc in enumerate(script.scenes):
            p = render.make_card(run / "images" / f"card_{i}.jpg", fmt_w, fmt_h, brand, sc.headline)
            assets.append(Asset(f"card{i}", str(p), "graphic", fmt_w, fmt_h, sc.headline))
    return assets


def produce(cfg: Config, topic: Topic, fmt_name: str, client: Any, tts: Any, store: Store,
            extra_images: list[Path] | None = None, preset: str | None = None,
            log: Callable[[str], None] = print) -> dict[str, Any]:
    fmt = cfg.fmt(fmt_name)
    run_id = f"{datetime.now().strftime('%Y%m%d')}-{topic.slug}-{fmt_name}"
    run = store.run_dir(run_id)
    brand = brand_from(cfg)
    model = cfg["llm"]["model"]

    # 1. script (cached so a failed render does not re-bill the LLM)
    sp = run / "script.json"
    if sp.exists():
        script = script_from_dict(json.loads(sp.read_text(encoding="utf-8")))
    else:
        log("writing script…")
        script = write_script(client, model, topic, fmt, cfg["channel"]["name"])
        save_json(sp, script)

    # 2. narration
    log("synthesizing voice…")
    audios = synthesize_script(tts, script.scenes, run / "audio")
    durs = A.scene_durations(audios)
    long_form = not fmt.portrait
    intro = INTRO_SECONDS if long_form else 0.0
    outro = OUTRO_SECONDS if long_form else 0.0
    total = intro + sum(durs) + outro
    starts, t = [], intro
    for d in durs:
        starts.append(t)
        t += d

    # 3. pictures: user images first, stock fallback, AI picks + arranges
    assets = gather_assets(cfg, topic, script, run, total, extra_images or [], log)
    by_id = {a.id: a for a in assets}
    log("choosing and arranging photos…")
    plan = planner.make_plan(client, cfg["llm"]["vision_model"], script.scenes, assets,
                             cfg["images"]["max_per_scene"])
    shots = planner.time_shots(plan, starts, durs)
    save_json(run / "plan.json", [asdict(s) for s in shots])

    # 4. timeline
    tshots: list[render.ShotT] = []
    if intro:
        c = render.make_card(run / "intro.jpg", fmt.width, fmt.height, brand, brand.name,
                             "भारतीय राजनीति, सीधी और साफ़ बात")
        tshots.append(render.ShotT(str(c), 0.0, intro, "still", graphic=True))
    for s in shots:
        a = by_id[s.asset_id]
        tshots.append(render.ShotT(a.path, s.start, s.end, s.motion, s.focus_x, s.focus_y,
                                   graphic=a.kind == "graphic"))
    if outro:
        c = render.make_card(run / "outro.jpg", fmt.width, fmt.height, brand, "चैनल को सब्सक्राइब करें",
                             f"रोज़ ताज़ा राजनीतिक खबरें · {brand.handle}")
        tshots.append(render.ShotT(str(c), total - outro, total, "still", graphic=True))
    if tshots:                                   # shots must tile the timeline with no gaps
        tshots[0].start = 0.0
        tshots[-1].end = total
    words, straps = [], []
    for i, (sc, au) in enumerate(zip(script.scenes, audios)):
        words += [render.CapWord(w.text, starts[i] + A.LEAD_IN + w.start, starts[i] + A.LEAD_IN + w.end)
                  for w in au.words]
        s0 = starts[i] + 0.2
        straps.append(render.Strap(s0, min(starts[i] + durs[i] - 0.15, s0 + 6.0), sc.label, sc.headline))
    tl = render.Timeline(
        fmt.width, fmt.height, fmt.fps, total, tshots, brand, straps,
        words if fmt.captions else [], "|".join(s.headline for s in script.scenes) if fmt.ticker else "",
        main_start=intro, main_end=total - outro, preset=preset or "fast")

    # 5. render + sound
    log(f"rendering {total:.0f}s {fmt.width}x{fmt.height}…")
    silent = render.render_video(tl, run / "video_silent.mp4")
    nar = A.build_narration(audios, durs, intro, total, run / "narration.wav")
    music = A.pick_music(cfg.path(cfg["audio"]["music_dir"]))
    final = A.mux(silent, nar, run / "video.mp4", total, music, cfg["audio"]["music_volume"],
                  cfg["audio"]["target_lufs"])
    silent.unlink(missing_ok=True)
    first = by_id[shots[0].asset_id].path
    thumb = make_thumbnail(first, script.scenes[0].headline, brand, run / "thumbnail.jpg")

    # 6. description, policy gate, meta
    credits = sorted({a.credit for a in assets if a.credit})
    desc = build_description(script, topic, cfg["channel"], not long_form, credits,
                             cfg["youtube"].get("contains_synthetic_media", True))
    issues = policy_check(script, topic, assets, store.history(), cfg.get("limits", {}).get("max_uploads_per_day", 4),
                          cfg["curation"]["min_sources"])
    meta = {"id": run_id, "status": "pending", "format": fmt_name, "title": script.title,
            "description": desc, "tags": script.tags, "topic": topic.title, "video": str(final),
            "thumbnail": str(thumb), "duration": round(total, 1), "video_id": None,
            "issues": [asdict(i) for i in issues],
            "created": datetime.now(timezone.utc).isoformat()}
    store.save_meta(run_id, meta)
    if any(i.level == "block" for i in issues):
        log("⚠ blocked by policy checks: " + "; ".join(i.msg for i in issues if i.level == "block"))
    return meta
