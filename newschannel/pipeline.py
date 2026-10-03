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
from . import cards, deep, learn
from . import scout as scout_mod
from . import music as music_mod
from .ledger import Ledger
from .library import Library
from . import media, planner, render
from .config import Config
from .i18n import t as label
from .models import Asset, SceneAudio, Script, Story, Topic, save_json, script_from_dict
from .monetization import Issue, policy_check
from .review import Store
from .scriptwriter import build_description, fix_clip_scenes, write_script
from .packaging import Packaging, improve
from .thumbnail import make_thumbnail, make_variants
from .tts import probe_duration

INTRO_SECONDS, OUTRO_SECONDS = 2.0, 4.0     # outro becomes the end-screen card (see endscreen.seconds)


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
                  client: Any = None, model: str = "", vision_model: str = "",
                  card_size: tuple[int, int] = (1080, 1920), approved_only: bool = False) -> tuple[list[Asset], list[str]]:
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
    mode = "approve" if approved_only else img.get("stock_mode", "auto_strict")
    if mode == "auto_strict" and len(assets) < need:
        # Unattended runs: only take photos the scout rates as a clear fit (default 8/10 or better). In the web app you
        # approve photos yourself beforehand, so nothing is ever fetched automatically there.
        story = f"{topic.title}. " + " ".join(s.summary for s in topic.stories[:3]) + " " + \
            " ".join(sc.headline for sc in script.scenes)
        try:
            res = scout_mod.scout(cfg, client, story, [sc.visual_query for sc in script.scenes if sc.visual_query],
                                  folder=run / "scout", log=log)
            top = sorted((c for c in res["candidates"] if c["score"] >= img.get("stock_min_score", 8)),
                         key=lambda c: -c["score"])[:need - len(assets)]
            for e in scout_mod.approve(run / "scout", [c["id"] for c in top], lib, img["min_side_px"], log=log):
                assets.append(lib.to_asset(e, run / "images"))
                used.append(e.id)
            log(f"photos found online and rated as a clear fit: {len(top)}")
        except Exception as exc:
            log(f"[photos] online search skipped: {exc}")
    if not assets:   # last resort: clean headline cards (clearly graphics, never fake photos)
        brand = brand_from(cfg)
        fmt_w, fmt_h = card_size
        (run / "images").mkdir(parents=True, exist_ok=True)
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
            breaking: bool = False, library_ids: list[str] | None = None,
            language: str | None = None, script: Script | None = None, ledger: Ledger | None = None,
            target_seconds: int | None = None, music: str | None = None,
            deep_check: tuple[list[str], list[str]] | None = None, as_of: str = "",
            approved_only: bool = False) -> dict[str, Any]:
    """Make one video. For deep-analysis videos pass a ready `script`, its fact `ledger` and the checker's results."""
    if language:
        cfg.data.setdefault("content", {})["language"] = language
    lang = cfg.lang
    fmt = cfg.fmt(fmt_name)
    if breaking:                                 # breaking news: shorter, faster, labelled
        fmt.target_seconds = cfg.get("breaking", {}).get("target_seconds", 40)
    if target_seconds:
        fmt.target_seconds = target_seconds
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
    if ledger is not None:
        ledger.save(run / "ledger.json")
    if sp.exists():
        script = script_from_dict(json.loads(sp.read_text(encoding="utf-8")))
    elif script is not None:                      # deep analysis: already written and fact-checked
        log("polishing title and thumbnail text…")
        pk = improve(client, model, script, topic, insights, cfg.get("playlists", {}).get("categories", []), lang,
                     rewrite_hook=False)
        save_json(run / "packaging.json", pk)
        save_json(sp, script)
    else:
        log("writing script…")
        script = write_script(client, model, topic, fmt, cfg["channel"]["name"], clips, insights, urgent=breaking, lang=lang)
        fix_clip_scenes(script, clips, lang)
        if breaking:
            script.scenes[0].label = label(lang, "breaking")
        log("polishing hook, title and thumbnail text…")
        pk = improve(client, model, script, topic, insights, cfg.get("playlists", {}).get("categories", []), lang)
        save_json(run / "packaging.json", pk)
        save_json(sp, script)

    # 2. narration
    log("synthesizing voice…")
    audios = synthesize_scenes(tts, script, clips, run / "audio")
    durs = A.scene_durations(audios)
    long_form = not fmt.portrait
    intro = INTRO_SECONDS if long_form else 0.0
    es = cfg.get("endscreen", {})
    outro = (es.get("seconds", 12.0) if es.get("enabled", True) else OUTRO_SECONDS) if long_form else 0.0
    total = intro + sum(durs) + outro
    starts, t = [], intro
    for d in durs:
        starts.append(t)
        t += d

    # 3. pictures: user images first, stock fallback, AI picks + arranges
    clip_total = sum(a.duration for a, sc in zip(audios, script.scenes) if sc.kind == "clip")
    assets, lib_used = gather_assets(cfg, topic, script, run, total - clip_total, extra_images or [], log,
                                     library_ids, client, model, vision_model, (fmt.width, fmt.height), approved_only)
    by_id = {a.id: a for a in assets}
    log("choosing and arranging photos…")
    plan = planner.make_plan(client, vision_model, script.scenes, assets,
                             cfg["images"]["max_per_scene"])
    shots = planner.time_shots(plan, starts, durs)
    save_json(run / "plan.json", [asdict(s) for s in shots])
    card_shots: list[render.ShotT] = []
    if ledger is not None:                       # quote / number / timeline cards built from the ledger
        (run / "cards").mkdir(exist_ok=True)
        for i, sc in enumerate(script.scenes):
            if not sc.card or sc.kind == "clip":
                continue
            img = cards.build_card(ledger, sc.card, brand, lang, fmt.width, fmt.height, run / "cards" / f"card_{i}.jpg")
            dc = min(8.0 if sc.card["type"] in ("quote", "sources") else 6.0, 0.7 * durs[i])
            if not img or dc < 2.0:
                continue
            mine = [x for x in shots if x.scene == i]
            cut = starts[i] + dc
            while mine and mine[0].end <= cut + 0.8:       # photos fully covered by the card are dropped
                shots.remove(mine.pop(0))
            if mine:
                mine[0].start = cut
            else:
                dc = durs[i]
            card_shots.append(render.ShotT(str(img), starts[i], starts[i] + dc, "still", graphic=True))

    # 4. timeline
    tshots: list[render.ShotT] = []
    if intro:
        c = render.make_card(run / "intro.jpg", fmt.width, fmt.height, brand, brand.name,
                             label(lang, "intro_tag"))
        tshots.append(render.ShotT(str(c), 0.0, intro, "still", graphic=True))
    for s in shots:
        a = by_id[s.asset_id]
        tshots.append(render.ShotT(a.path, s.start, s.end, s.motion, s.focus_x, s.focus_y,
                                   graphic=a.kind == "graphic"))
    for i, sc in enumerate(script.scenes):
        if sc.kind == "clip":
            tshots.append(render.ShotT("", starts[i], starts[i] + durs[i], "still",
                                       video=clips[sc.clip_id].video))
    tshots += card_shots
    tshots.sort(key=lambda x: x.start)
    if outro:
        if es.get("enabled", True):     # leaves clean boxes where you place YouTube's end-screen elements in Studio
            c = render.make_end_card(run / "outro.jpg", fmt.width, fmt.height, brand, label(lang, "endcard_title"),
                                     label(lang, "endcard_sub"))
        else:
            c = render.make_card(run / "outro.jpg", fmt.width, fmt.height, brand, label(lang, "outro_title"),
                                 f"{label(lang, 'outro_sub')} · {brand.handle}")
        tshots.append(render.ShotT(str(c), total - outro, total, "still", graphic=True))
    if tshots:                                   # shots must tile the timeline with no gaps
        tshots[0].start = 0.0
        tshots[-1].end = total
    words, straps, subs, credits = [], [], [], []
    for i, (sc, au) in enumerate(zip(script.scenes, audios)):
        if sc.kind == "clip":
            c = clips[sc.clip_id]
            subs += [render.TextSpan(starts[i] + x.start, starts[i] + x.end, x.text) for x in c.subs]
            credits.append(render.TextSpan(starts[i], starts[i] + durs[i], f"{label(lang, 'source')}: {c.credit}"))
        if sc.source_tag and sc.kind != "clip":
            credits.append(render.TextSpan(starts[i], starts[i] + durs[i], f"{label(lang, 'source')}: {sc.source_tag}"))
        words += [render.CapWord(w.text, starts[i] + A.LEAD_IN + w.start, starts[i] + A.LEAD_IN + w.end)
                  for w in au.words]
        s0 = starts[i] + 0.2
        straps.append(render.Strap(s0, min(starts[i] + durs[i] - 0.15, s0 + 6.0), sc.label, sc.headline))
    tl = render.Timeline(
        fmt.width, fmt.height, fmt.fps, total, tshots, brand, straps,
        words if fmt.captions else [], "|".join(s.headline for s in script.scenes) if fmt.ticker else "",
        subs=subs, credits=credits, main_start=intro, main_end=total - outro, preset=preset or "fast",
        labels={"ticker": label(lang, "ticker")})

    # 5. render + sound
    log(f"rendering {total:.0f}s {fmt.width}x{fmt.height}…")
    silent = render.render_video(tl, run / "video_silent.mp4")
    nar = A.build_narration(audios, durs, intro, total, run / "narration.wav")
    music_file = A.pick_music(cfg.path(cfg["audio"]["music_dir"]))
    swell = None
    if ledger is not None:                        # analysis videos get a mood-matched bed that swells at section changes
        mood = music or cfg.get("analysis", {}).get("music", "auto")
        if mood == "off":
            music_file = None
        else:
            if music_file is None and cfg["audio"].get("generate_music", True):
                music_file = music_mod.synth(run / "bed.wav", total, music_mod.mood_for(script.stance)
                                             if mood == "auto" else mood)
            swell = music_mod.swell_expr([starts[i] for i in range(1, len(script.scenes))
                                          if script.scenes[i].section != script.scenes[i - 1].section])
    final = A.mux(silent, nar, run / "video.mp4", total, music_file, cfg["audio"]["music_volume"],
                  cfg["audio"]["target_lufs"], swell)
    silent.unlink(missing_ok=True)
    pkp = run / "packaging.json"
    pk = Packaging(**json.loads(pkp.read_text(encoding="utf-8"))) if pkp.exists() else Packaging()
    photo_paths = list(dict.fromkeys(by_id[s.asset_id].path for s in shots if by_id[s.asset_id].kind != "graphic")) \
        or [by_id[shots[0].asset_id].path]
    variants = make_variants(photo_paths, pk.thumb_texts, brand, run, script.scenes[0].headline, label(lang, "thumb_tag"))
    thumb = run / "thumbnail.jpg"
    shutil.copyfile(variants[0]["file"], thumb)

    # 6. description, policy gate, meta
    credits = sorted({a.credit for a in assets if a.credit})
    if any(("pexels" in c.lower() or "pixabay" in c.lower()) for c in credits):
        credits.append(label(lang, "d_stock_note"))
    if ledger is not None:
        desc = deep.build_description(script, ledger, starts, intro, credits, cfg["channel"], lang, as_of,
                                      cfg["youtube"].get("contains_synthetic_media", True))
    else:
        desc = build_description(script, topic, cfg["channel"], not long_form, credits,
                                 cfg["youtube"].get("contains_synthetic_media", True), clips, lang)
    issues = policy_check(script, topic, assets, store.history(), cfg.get("limits", {}).get("max_uploads_per_day", 4),
                          cfg["curation"]["min_sources"], clips, total, cfg.get("clips", {}).get("max_share", 0.4))
    if deep_check:
        issues += [Issue("block", f"Fact check: {m}") for m in deep_check[0]]
        issues += [Issue("warn", m) for m in deep_check[1]]
    if ledger is not None and script.stance in ("critical", "supportive"):
        issues.append(Issue("warn", f"Angle: {script.stance}. Every point is tied to ledger claims, but read the script once "
                                    "and check the Counterpoint scene before approving."))
    if breaking:
        issues.append(Issue("warn", "BREAKING: details may still be developing. Re-check every fact against the "
                                    "sources before approving."))
    meta = {"id": run_id, "status": "pending", "breaking": breaking, "language": lang, "format": fmt_name, "title": script.title,
            "description": desc, "tags": script.tags, "topic": topic.title, "video": str(final),
            "thumbnail": str(thumb), "thumbnails": variants, "category": pk.category,
            "title_options": [t["text"] for t in pk.titles], "hook": script.scenes[0].narration,
            "hook_options": [h["text"] for h in pk.hooks], "duration": round(total, 1), "video_id": None,
            "issues": [asdict(i) for i in issues],
            "clips": [{"credit": c.credit, "note": c.note, "seconds": round(c.duration, 1),
                       "language": c.language, "machine_translated": c.machine_translated,
                       "transcript": c.transcript} for c in clips],
            "created": datetime.now(timezone.utc).isoformat()}
    if ledger is not None:
        meta["analysis"] = {"stance": script.stance, "minutes": round(total / 60, 1), "counts": ledger.counts(),
                            "as_of": as_of, "ledger_file": "ledger.json",
                            "sources": [{"id": x.id, "outlet": x.outlet, "tier": x.tier, "url": x.url, "title": x.title}
                                        for x in ledger.sources]}
    store.save_meta(run_id, meta)
    Library(cfg.path(cfg["images"].get("library_dir", "library"))).mark_used(lib_used)
    if any(i.level == "block" for i in issues):
        log("⚠ blocked by policy checks: " + "; ".join(i.msg for i in issues if i.level == "block"))
    return meta
