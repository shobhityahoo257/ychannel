from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import audio as A
from . import clips as clipmod
from . import learn
from .library import Library
from . import media, planner, render
from .config import Config
from .models import Asset, SceneAudio, Script, Story, Topic, save_json, script_from_dict
from .monetization import Issue, policy_check
from .review import Store
from .scriptwriter import build_description, fix_clip_scenes, write_script
from .packaging import Packaging, improve
from .thumbnail import make_thumbnail, make_variants
from .tts import probe_duration

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
                  extra: list[Path], log: Callable[[str], None], library_ids: list[str] | None = None,
                  client: Any = None, model: str = "", vision_model: str = "") -> tuple[list[Asset], list[str]]:
    """Order of preference: photos you upload now -> photos you picked from the library ->
    library photos the AI finds relevant -> stock. Returns (assets, library ids used)."""
    img = cfg["images"]
    inbox = cfg.path(img["inbox_dir"])
    lib = Library(cfg.path(img.get("library_dir", "library")))
    folders = [*extra, inbox / topic.slug, inbox]
    assets = media.load_user_images(folders, run / "images", img["min_side_px"])
    used: list[str] = []
    if img.get("save_to_library", True):           # every photo you add becomes part of your inventory
        for a in assets:
            e, _ = lib.add_file(Path(a.path), a.caption if a.explicit_caption else "", a.credit)
            if e:
                used.append(e.id)
    log(f"user images: {len(assets)}")
    if used and client is not None and img.get("auto_tag", True):
        log("describing new photos for your library…")
        lib.autotag(client, vision_model or model, limit=12)         # makes them findable later; best effort
    for lid in library_ids or []:
        e = lib.get(lid)
        if e and lid not in used:
            assets.append(lib.to_asset(e, run / "images"))
            used.append(lid)
    if library_ids:
        log(f"picked from library: {len(library_ids)}")
    need = planner.needed_photos(total_seconds)
    if img.get("use_library", True) and len(assets) < need:
        story = f"{topic.title}. " + " ".join(s.summary for s in topic.stories[:3]) + " " + \
            " ".join(sc.headline for sc in script.scenes)
        for e in lib.suggest(client, model, story, min(need - len(assets), 6), set(used)):
            assets.append(lib.to_asset(e, run / "images"))
            used.append(e.id)
        log(f"after library suggestions: {len(assets)}")
    if img.get("use_stock_fallback", True) and len(assets) < need:
        for sc in script.scenes:
            if len(assets) >= need:
                break
            if sc.visual_query:
                got = media.fetch_stock(sc.visual_query, run / "images", img["min_side_px"], n=2)
                assets += got
                if img.get("save_to_library", True):
                    for a in got:
                        e, _ = lib.add_file(Path(a.path), a.caption, a.credit, source="stock")
                        if e:
                            used.append(e.id)
        log(f"after stock fallback: {len(assets)}")
    if not assets:   # last resort: clean headline cards (clearly graphics, never fake photos)
        brand = brand_from(cfg)
        fmt_w, fmt_h = 1080, 1920
        for i, sc in enumerate(script.scenes):
            p = render.make_card(run / "images" / f"card_{i}.jpg", fmt_w, fmt_h, brand, sc.headline)
            assets.append(Asset(f"card{i}", str(p), "graphic", fmt_w, fmt_h, sc.headline))
    return assets, used


def synthesize_scenes(tts, script: Script, clips: list, folder: Path) -> list[SceneAudio]:
    """Voice for spoken scenes; original (loudness-normalised) audio for clip scenes."""
    folder.mkdir(parents=True, exist_ok=True)
    out: list[SceneAudio] = []
    sc = script.scenes
    for i, s in enumerate(sc):
        if s.kind == "clip":
            c = clips[s.clip_id]
            out.append(SceneAudio(c.wav, probe_duration(c.wav), [], lead=0.0, tail=0.0))
            continue
        prev = sc[i - 1].narration[-200:] if i else ""
        nxt = sc[i + 1].narration[:200] if i + 1 < len(sc) else ""
        out.append(tts.synthesize(s.narration, folder / f"scene_{i:02d}.mp3", prev, nxt))
    return out


def produce(cfg: Config, topic: Topic, fmt_name: str, client: Any, tts: Any, store: Store,
            extra_images: list[Path] | None = None, preset: str | None = None,
            log: Callable[[str], None] = print, clip_folders: list[Path] | None = None,
            breaking: bool = False, library_ids: list[str] | None = None) -> dict[str, Any]:
    fmt = cfg.fmt(fmt_name)
    if breaking:                                 # breaking news: shorter, faster, labelled
        fmt.target_seconds = cfg.get("breaking", {}).get("target_seconds", 40)
    run_id = f"{datetime.now().strftime('%Y%m%d')}-{topic.slug}-{fmt_name}"
    run = store.run_dir(run_id)
    brand = brand_from(cfg)
    model, vision_model = cfg.models()

    # 0. original clips (speeches etc.) supplied by you: trim, normalise, transcribe, subtitle
    clips = clipmod.load(cfg, [*(clip_folders or []), cfg.path(cfg["images"]["inbox_dir"]) / topic.slug / "clips"],
                         run, client, fmt.fps)
    if clips:
        log(f"clips: {len(clips)} ({sum(c.duration for c in clips):.0f}s of original footage)")

    insights = learn.prompt_hint(store)

    # 1. script (cached so a failed render does not re-bill the LLM)
    sp = run / "script.json"
    if sp.exists():
        script = script_from_dict(json.loads(sp.read_text(encoding="utf-8")))
    else:
        log("writing script…")
        script = write_script(client, model, topic, fmt, cfg["channel"]["name"], clips, insights, urgent=breaking)
        fix_clip_scenes(script, clips)
        if breaking:
            script.scenes[0].label = "ब्रेकिंग न्यूज़"
        log("polishing hook, title and thumbnail text…")
        pk = improve(client, model, script, topic, insights)
        save_json(run / "packaging.json", pk)
        save_json(sp, script)

    # 2. narration
    log("synthesizing voice…")
    audios = synthesize_scenes(tts, script, clips, run / "audio")
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
    clip_total = sum(a.duration for a, sc in zip(audios, script.scenes) if sc.kind == "clip")
    assets, lib_used = gather_assets(cfg, topic, script, run, total - clip_total, extra_images or [], log,
                                     library_ids, client, model, vision_model)
    by_id = {a.id: a for a in assets}
    log("choosing and arranging photos…")
    plan = planner.make_plan(client, vision_model, script.scenes, assets,
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
    for i, sc in enumerate(script.scenes):
        if sc.kind == "clip":
            tshots.append(render.ShotT("", starts[i], starts[i] + durs[i], "still",
                                       video=clips[sc.clip_id].video))
    tshots.sort(key=lambda x: x.start)
    if outro:
        c = render.make_card(run / "outro.jpg", fmt.width, fmt.height, brand, "चैनल को सब्सक्राइब करें",
                             f"रोज़ ताज़ा राजनीतिक खबरें · {brand.handle}")
        tshots.append(render.ShotT(str(c), total - outro, total, "still", graphic=True))
    if tshots:                                   # shots must tile the timeline with no gaps
        tshots[0].start = 0.0
        tshots[-1].end = total
    words, straps, subs, credits = [], [], [], []
    for i, (sc, au) in enumerate(zip(script.scenes, audios)):
        if sc.kind == "clip":
            c = clips[sc.clip_id]
            subs += [render.TextSpan(starts[i] + x.start, starts[i] + x.end, x.text) for x in c.subs]
            credits.append(render.TextSpan(starts[i], starts[i] + durs[i], f"स्रोत: {c.credit}"))
        words += [render.CapWord(w.text, starts[i] + A.LEAD_IN + w.start, starts[i] + A.LEAD_IN + w.end)
                  for w in au.words]
        s0 = starts[i] + 0.2
        straps.append(render.Strap(s0, min(starts[i] + durs[i] - 0.15, s0 + 6.0), sc.label, sc.headline))
    tl = render.Timeline(
        fmt.width, fmt.height, fmt.fps, total, tshots, brand, straps,
        words if fmt.captions else [], "|".join(s.headline for s in script.scenes) if fmt.ticker else "",
        subs=subs, credits=credits, main_start=intro, main_end=total - outro, preset=preset or "fast")

    # 5. render + sound
    log(f"rendering {total:.0f}s {fmt.width}x{fmt.height}…")
    silent = render.render_video(tl, run / "video_silent.mp4")
    nar = A.build_narration(audios, durs, intro, total, run / "narration.wav")
    music = A.pick_music(cfg.path(cfg["audio"]["music_dir"]))
    final = A.mux(silent, nar, run / "video.mp4", total, music, cfg["audio"]["music_volume"],
                  cfg["audio"]["target_lufs"])
    silent.unlink(missing_ok=True)
    pkp = run / "packaging.json"
    pk = Packaging(**json.loads(pkp.read_text(encoding="utf-8"))) if pkp.exists() else Packaging()
    photo_paths = list(dict.fromkeys(by_id[s.asset_id].path for s in shots if by_id[s.asset_id].kind != "graphic")) \
        or [by_id[shots[0].asset_id].path]
    variants = make_variants(photo_paths, pk.thumb_texts, brand, run, script.scenes[0].headline)
    thumb = run / "thumbnail.jpg"
    shutil.copyfile(variants[0]["file"], thumb)

    # 6. description, policy gate, meta
    credits = sorted({a.credit for a in assets if a.credit})
    desc = build_description(script, topic, cfg["channel"], not long_form, credits,
                             cfg["youtube"].get("contains_synthetic_media", True), clips)
    issues = policy_check(script, topic, assets, store.history(), cfg.get("limits", {}).get("max_uploads_per_day", 4),
                          cfg["curation"]["min_sources"], clips, total, cfg.get("clips", {}).get("max_share", 0.4))
    if breaking:
        issues.append(Issue("warn", "BREAKING: details may still be developing. Re-check every fact against the "
                                    "sources before approving."))
    meta = {"id": run_id, "status": "pending", "breaking": breaking, "format": fmt_name, "title": script.title,
            "description": desc, "tags": script.tags, "topic": topic.title, "video": str(final),
            "thumbnail": str(thumb), "thumbnails": variants,
            "title_options": [t["text"] for t in pk.titles], "hook": script.scenes[0].narration,
            "hook_options": [h["text"] for h in pk.hooks], "duration": round(total, 1), "video_id": None,
            "issues": [asdict(i) for i in issues],
            "clips": [{"credit": c.credit, "note": c.note, "seconds": round(c.duration, 1),
                       "language": c.language, "machine_translated": c.machine_translated,
                       "transcript": c.transcript} for c in clips],
            "created": datetime.now(timezone.utc).isoformat()}
    store.save_meta(run_id, meta)
    Library(cfg.path(cfg["images"].get("library_dir", "library"))).mark_used(lib_used)
    if any(i.level == "block" for i in issues):
        log("⚠ blocked by policy checks: " + "; ".join(i.msg for i in issues if i.level == "block"))
    return meta
