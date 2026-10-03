"""Step-by-step creation with your approval.

A video is made in three stages, each saved to disk so a draft survives page reloads and can be edited:

    1. SCRIPT   write (and for deep analysis fact-check) the script        -> you review / edit / ask the AI to revise
    2. PHOTOS   gather photos + media, let the AI arrange them per scene   -> you review / swap / add / re-arrange
    3. VIDEO    voice-over, render                                          -> you review the finished video (My videos)

For each of stage 1 and 2 you choose `manual` (the run stops and waits for you) or `auto` (it continues by itself).
Both `auto` = fully automatic. The final video still needs your approval before anything is uploaded.
"""
from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import analysis as analysis_mod
from . import clips as clipmod
from . import deep, learn, planner
from .config import Config
from .i18n import t as label
from .library import Library
from .media import import_image
from .models import Asset, Scene, Script, Shot, Story, Topic, save_json, script_from_dict
from .packaging import improve
from .pipeline import estimate_seconds, load_media, produce, save_media, stage_media
from .review import Store
from .scriptwriter import fix_clip_scenes, validate, write_script

MODES = ("manual", "auto")


class Blocked(RuntimeError):
    """The step cannot be approved yet (e.g. the fact check still has problems)."""


def topic_to_dict(t: Topic) -> dict[str, Any]:
    return {"title": t.title, "why": t.why, "stories": [asdict(s) for s in t.stories]}


def topic_from_dict(d: dict[str, Any]) -> Topic:
    return Topic(d["title"], [Story(**s) for s in d.get("stories", [])], d.get("why", ""))


class Workflow:
    def __init__(self, store: Store, run_id: str):
        self.store, self.id = store, run_id
        self.dir = store.root / run_id

    @property
    def state(self) -> dict[str, Any]:
        return json.loads((self.dir / "workflow.json").read_text(encoding="utf-8"))

    def save(self, **updates: Any) -> dict[str, Any]:
        st = self.state if (self.dir / "workflow.json").exists() else {}
        st.update(updates)
        (self.dir / "workflow.json").write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        return st

    def params(self) -> dict[str, Any]:
        return self.state["params"]

    @classmethod
    def exists(cls, store: Store, run_id: str) -> bool:
        return (store.root / run_id / "workflow.json").exists()

    @classmethod
    def create(cls, store: Store, kind: str, title: str, slug: str, fmt: str, params: dict[str, Any],
               mode: dict[str, str]) -> "Workflow":
        run_id = f"{datetime.now().strftime('%Y%m%d')}-{slug}-{fmt}-{uuid.uuid4().hex[:5]}"
        wf = cls(store, run_id)
        wf.dir.mkdir(parents=True, exist_ok=True)
        mode = {"script": mode.get("script", "manual"), "photos": mode.get("photos", "manual")}
        if any(v not in MODES for v in mode.values()):
            raise ValueError("mode must be manual or auto")
        wf.save(id=run_id, kind=kind, status="queued", title=title, format=fmt, mode=mode, params=params,
                error="", created=datetime.now().isoformat(timespec="seconds"))
        return wf

    # ---------------------------------------------------------------- data access
    def script(self) -> Script:
        return script_from_dict(json.loads((self.dir / "script.json").read_text(encoding="utf-8")))

    def clips(self) -> list:
        p = self.dir / "clips.json"
        return clipmod.load_saved(p) if p.exists() else []

    def ledger(self):
        from .ledger import Ledger
        return Ledger.load(self.dir / "ledger.json")

    def raw(self) -> dict[str, Any]:
        return json.loads((self.dir / "analysis_raw.json").read_text(encoding="utf-8"))

    def check(self) -> dict[str, list[str]]:
        p = self.dir / "deep_check.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"violations": [], "warnings": []}


def split_budget(bad: list[str], soft: list[str]) -> tuple[list[str], list[str]]:
    """While a person is reviewing, the target length is advice (a warning), not a rule: they decide how long it is.
    Every fact rule stays blocking."""
    length = [b for b in bad if b.startswith("Narration is")]
    return [b for b in bad if b not in length], soft + length


def apply_language(cfg: Config, wf: Workflow) -> None:
    lang = wf.params().get("language")
    if lang:
        cfg.data.setdefault("content", {})["language"] = lang


def _topic(wf: Workflow):
    p = wf.params()
    return topic_from_dict(p["topic"]) if wf.state["kind"] == "news" else analysis_mod.ledger_topic(wf.ledger())


def _fmt(cfg: Config, wf: Workflow):
    st, p = wf.state, wf.params()
    fmt = cfg.fmt("analysis" if st["kind"] == "analysis" else st["format"])
    if st["kind"] == "analysis":
        fmt.target_seconds = int(p["minutes"] * 60)
    elif p.get("breaking"):
        fmt.target_seconds = cfg.get("breaking", {}).get("target_seconds", 40)
    return fmt


# ------------------------------------------------------------------------------ stage 1: script
def stage_script(cfg: Config, client: Any, wf: Workflow, log: Callable[[str], None] = print,
                 instruction: str = "") -> None:
    apply_language(cfg, wf)
    lang, model = cfg.lang, cfg.models()[0]
    st, p = wf.state, wf.params()
    wf.save(status="script_running", error="")
    insights = learn.prompt_hint(wf.store)
    cats = cfg.get("playlists", {}).get("categories", [])
    previous: Script | None = wf.script() if instruction and (wf.dir / "script.json").exists() else None
    if st["kind"] == "news":
        topic, fmt = _topic(wf), _fmt(cfg, wf)
        clips = wf.clips() if (wf.dir / "clips.json").exists() else None
        if clips is None:
            clip_dirs = [wf.dir / "inputs" / "clips", cfg.path(cfg["images"]["inbox_dir"]) / topic.slug / "clips"]
            clips = clipmod.load(cfg, clip_dirs, wf.dir, client, fmt.fps)
            if clips:
                clipmod.save_clips(clips, wf.dir / "clips.json")
        log("writing script…")
        script = write_script(client, model, topic, fmt, cfg["channel"]["name"], clips, insights, urgent=bool(p.get("breaking")),
                              lang=lang, instruction=instruction, previous=previous)
        fix_clip_scenes(script, clips, lang)
        if p.get("breaking"):
            script.scenes[0].label = label(lang, "breaking")
        log("polishing hook, title and thumbnail text…")
        pk = improve(client, model, script, topic, insights, cats, lang)
    else:
        ledger, rec = analysis_mod.load_research(wf.store, p["research_id"])
        stance = rec["stance"] if p.get("stance") in ("auto", "", None) else p["stance"]
        minutes = rec["minutes"] if p.get("minutes") in ("auto", "", None) else float(p["minutes"])
        minutes = max(float(cfg.get("analysis", {}).get("min_minutes", 4.0)), min(20.0, minutes))
        wf.save(params={**p, "stance": stance, "minutes": minutes, "as_of": ledger.as_of})
        ledger.save(wf.dir / "ledger.json")
        log(f"writing the {stance} analysis script ({minutes:g} min)…")
        prev_raw = wf.raw() if instruction and (wf.dir / "analysis_raw.json").exists() else None
        w = deep.write_analysis(client, model, ledger, stance, minutes, lang, cfg["channel"]["name"], cfg.get("analysis", {}),
                                insights, ledger.as_of, log, instruction, prev_raw)
        script = w.script
        (wf.dir / "analysis_raw.json").write_text(json.dumps(w.raw, ensure_ascii=False, indent=1), encoding="utf-8")
        viol, warn = split_budget(w.violations, w.warnings)
        (wf.dir / "deep_check.json").write_text(json.dumps({"violations": viol, "warnings": warn}), encoding="utf-8")
        log("polishing title and thumbnail text…")
        pk = improve(client, model, script, analysis_mod.ledger_topic(ledger), insights, cats, lang, rewrite_hook=False)
    save_json(wf.dir / "packaging.json", pk)
    save_json(wf.dir / "script.json", script)
    wf.save(status="script_review", title=script.title)


def describe_script(wf: Workflow) -> dict[str, Any]:
    script = wf.script()
    st = wf.state
    out: dict[str, Any] = {"title": script.title, "title_options": [], "scenes": [], "stance": script.stance,
                           "words": script.word_count, "est_seconds": round(estimate_seconds(script))}
    pkp = wf.dir / "packaging.json"
    if pkp.exists():
        out["title_options"] = [x["text"] for x in json.loads(pkp.read_text(encoding="utf-8")).get("titles", [])]
    raw = wf.raw() if st["kind"] == "analysis" and (wf.dir / "analysis_raw.json").exists() else None
    for i, sc in enumerate(script.scenes):
        item = {"i": i, "kind": sc.kind, "section": sc.section, "label": sc.label, "headline": sc.headline,
                "narration": sc.narration, "clip_id": sc.clip_id, "claim_ids": sc.claim_ids, "card": sc.card}
        if raw and i < len(raw["scenes"]):
            item["beats"] = [{"type": b.get("type"), "text": b.get("text", ""), "claim_ids": b.get("claim_ids") or [],
                              "attributed_to": b.get("attributed_to", "")} for b in raw["scenes"][i].get("beats", [])]
        out["scenes"].append(item)
    return out


def save_script_edits(cfg: Config, wf: Workflow, edits: dict[str, Any]) -> dict[str, list[str]]:
    """Apply your edits (title, headlines, narration / beat text). Deep-analysis scripts are re-checked automatically."""
    apply_language(cfg, wf)
    st = wf.state
    rows = edits.get("scenes") or []
    if st["kind"] == "analysis":
        raw, ledger = wf.raw(), wf.ledger()
        if edits.get("title"):
            raw["title"] = edits["title"].strip()
        for i, row in enumerate(rows[:len(raw["scenes"])]):
            sc = raw["scenes"][i]
            if "headline" in row:
                sc["headline"] = str(row["headline"])[:60]
            for j, b in enumerate(row.get("beats") or []):
                if j < len(sc.get("beats", [])) and isinstance(b.get("text"), str):
                    sc["beats"][j]["text"] = b["text"].strip()
        p = wf.params()
        bad, soft = deep.lint(raw["scenes"], ledger, p["stance"], p["minutes"], cfg.get("analysis", {}).get("banned_patterns"),
                              cfg.get("analysis", {}).get("warn_patterns"))
        bad, soft = split_budget(bad, soft)
        script = deep.to_script(raw, ledger, p["stance"], cfg.lang)
        (wf.dir / "analysis_raw.json").write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
        (wf.dir / "deep_check.json").write_text(json.dumps({"violations": bad, "warnings": soft}), encoding="utf-8")
        save_json(wf.dir / "script.json", script)
        wf.save(title=script.title)
        return {"violations": bad, "warnings": soft}
    script = wf.script()
    if edits.get("title"):
        script.title = edits["title"].strip()[:100]
    for i, row in enumerate(rows[:len(script.scenes)]):
        sc = script.scenes[i]
        if "headline" in row:
            sc.headline = str(row["headline"])[:60]
        if sc.kind != "clip" and isinstance(row.get("narration"), str) and row["narration"].strip():
            sc.narration = row["narration"].strip()
    save_json(wf.dir / "script.json", script)
    wf.save(title=script.title)
    fmt = cfg.fmt(wf.state["format"])
    return {"violations": [], "warnings": validate(script, fmt, wf.clips())}


def approve_script_check(wf: Workflow, force: bool = False) -> None:
    if wf.state["kind"] == "analysis" and wf.check()["violations"] and not force:
        raise Blocked("The fact check still has problems: " + "; ".join(wf.check()["violations"][:3]))


# ------------------------------------------------------------------------------ stage 2: photos & media
def stage_media_draft(cfg: Config, client: Any, wf: Workflow, log: Callable[[str], None] = print) -> None:
    apply_language(cfg, wf)
    p = wf.params()
    wf.save(status="media_running", error="")
    inputs = wf.dir / "inputs" / "images"
    stage_media(cfg, _topic(wf), wf.script(), wf.dir, _fmt(cfg, wf), client, log,
                [inputs] if inputs.exists() else [], p.get("library_ids"), True, None, p.get("music"))
    wf.save(status="media_review")


def describe_media(wf: Workflow) -> dict[str, Any] | None:
    md = load_media(wf.dir)
    if not md:
        return None
    assets, used, plan, music = md
    return {"assets": [{"id": a.id, "kind": a.kind, "caption": a.caption, "credit": a.credit} for a in assets],
            "plan": [[{"asset_id": s.asset_id, "motion": s.motion} for s in row] for row in plan],
            "music": music or wf.params().get("music") or "auto"}


def save_plan(wf: Workflow, plan_edit: list[list[dict[str, Any]]], music: str | None) -> None:
    assets, used, plan, old_music = load_media(wf.dir)
    script = wf.script()
    ids = {a.id for a in assets}
    old = {(i, s.asset_id): s for i, row in enumerate(plan) for s in row}
    new_plan: list[list[Shot]] = []
    for i, sc in enumerate(script.scenes):
        row = plan_edit[i] if i < len(plan_edit) else []
        shots: list[Shot] = []
        for item in ([] if sc.kind == "clip" else row)[:6]:
            aid = item.get("asset_id")
            if aid not in ids:
                continue
            prev = old.get((i, aid))
            shots.append(Shot(aid, i, motion=item.get("motion") or (prev.motion if prev else planner.MOTIONS[i % 4]),
                              focus_x=prev.focus_x if prev else 0.5, focus_y=prev.focus_y if prev else 0.5))
        new_plan.append(shots)
    save_media(wf.dir, assets, used, new_plan, music if music is not None else old_music)


def add_assets(cfg: Config, wf: Workflow, library_ids: list[str] | None = None, files: list[Path] | None = None) -> int:
    """Put more photos in this video's pool (from your library or uploads). They are not placed until you place them."""
    assets, used, plan, music = load_media(wf.dir)
    lib = Library(cfg.path(cfg["images"].get("library_dir", "library")))
    have = {a.id for a in assets}
    added = 0
    for lid in library_ids or []:
        e = lib.get(lid)
        if e and e.id not in have:
            assets.append(lib.to_asset(e, wf.dir / "images"))
            used.append(e.id)
            have.add(e.id)
            added += 1
    for f in files or []:
        a = import_image(f, wf.dir / "images", "user", min_side=cfg["images"]["min_side_px"])
        if a is None:
            continue
        if cfg["images"].get("save_to_library", True):
            e, _ = lib.add_file(Path(a.path), "", "")
            if e:
                used.append(e.id)
        if a.id not in have:
            assets.append(a)
            have.add(a.id)
            added += 1
    save_media(wf.dir, assets, used, plan, music)
    return added


def replan(cfg: Config, client: Any, wf: Workflow, log: Callable[[str], None] = print) -> None:
    assets, used, _, music = load_media(wf.dir)
    log("choosing and arranging photos…")
    plan = planner.make_plan(client, cfg.models()[1], wf.script().scenes, assets, cfg["images"]["max_per_scene"])
    save_media(wf.dir, assets, used, plan, music)


def reopen_script(wf: Workflow) -> None:
    (wf.dir / "media.json").unlink(missing_ok=True)
    wf.save(status="script_review")


def thumb_path(wf: Workflow, asset_id: str) -> Path | None:
    from PIL import Image
    md = load_media(wf.dir)
    a = next((x for x in md[0] if x.id == asset_id), None) if md else None
    if not a or not Path(a.path).exists():
        return None
    out = wf.dir / "thumbs" / f"{asset_id}.jpg"
    if not out.exists():
        out.parent.mkdir(exist_ok=True)
        with Image.open(a.path) as im:
            im = im.convert("RGB")
            im.thumbnail((420, 420))
            im.save(out, quality=82)
    return out


# ------------------------------------------------------------------------------ stage 3: video
def stage_render(cfg: Config, client: Any, tts: Any, wf: Workflow, log: Callable[[str], None] = print,
                 preset: str | None = None) -> dict[str, Any]:
    apply_language(cfg, wf)
    st, p = wf.state, wf.params()
    wf.save(status="render_running", error="")
    if st["kind"] == "news":
        meta = produce(cfg, _topic(wf), st["format"], client, tts, wf.store, preset=preset, log=log, breaking=bool(p.get("breaking")),
                       language=p.get("language"), approved_only=True, run_id=wf.id, prepared_clips=wf.clips(), reuse_media=True)
    else:
        chk = wf.check()
        meta = produce(cfg, _topic(wf), "analysis", client, tts, wf.store, preset=preset, log=log, language=p.get("language"),
                       ledger=wf.ledger(), target_seconds=int(p["minutes"] * 60), music=p.get("music"),
                       deep_check=(chk["violations"], chk["warnings"]), as_of=p.get("as_of", ""), approved_only=True,
                       run_id=wf.id, prepared_clips=[], reuse_media=True)
    wf.save(status="done", video_id=meta["id"])
    return meta


# ------------------------------------------------------------------------------ driving the stages
def advance(cfg: Config, client: Any, tts: Any, wf: Workflow, log: Callable[[str], None] = print,
            preset: str | None = None) -> dict[str, Any]:
    """Run whatever comes next, stopping at the first step the user wants to review."""
    try:
        st = wf.state
        if st["status"] in ("queued", "script_running"):
            stage_script(cfg, client, wf, log)
            if wf.state["mode"]["script"] == "manual":
                return wf.state
            approve_script_check(wf)
        if wf.state["status"] in ("script_review", "media_running"):
            stage_media_draft(cfg, client, wf, log)
            if wf.state["mode"]["photos"] == "manual":
                return wf.state
        if wf.state["status"] in ("media_review", "render_running"):
            stage_render(cfg, client, tts, wf, log, preset)
        return wf.state
    except Exception as exc:
        wf.save(error=f"{type(exc).__name__}: {exc}", status=_fallback_status(wf))
        raise


def _fallback_status(wf: Workflow) -> str:
    """After a failure, go back to the last review screen so nothing is lost."""
    if (wf.dir / "media.json").exists():
        return "media_review"
    if (wf.dir / "script.json").exists():
        return "script_review"
    return "queued"


def approve_script(cfg: Config, client: Any, tts: Any, wf: Workflow, log: Callable[[str], None] = print,
                   auto_rest: bool = False, force: bool = False, preset: str | None = None) -> dict[str, Any]:
    if wf.state["status"] != "script_review":
        raise Blocked("The script is not waiting for approval.")
    approve_script_check(wf, force)
    if auto_rest:
        wf.save(mode={**wf.state["mode"], "photos": "auto"})
    return advance(cfg, client, tts, wf, log, preset)


def approve_media(cfg: Config, client: Any, tts: Any, wf: Workflow, log: Callable[[str], None] = print,
                  preset: str | None = None) -> dict[str, Any]:
    if wf.state["status"] != "media_review":
        raise Blocked("The photos are not waiting for approval.")
    return advance(cfg, client, tts, wf, log, preset)


def list_drafts(store: Store) -> list[dict[str, Any]]:
    out = []
    for p in sorted(store.root.glob("*/workflow.json"), reverse=True):
        st = json.loads(p.read_text(encoding="utf-8"))
        if st.get("status") != "done":
            out.append({k: st.get(k) for k in ("id", "kind", "status", "title", "format", "mode", "created", "error")})
    return out


def discard(wf: Workflow) -> None:
    shutil.rmtree(wf.dir, ignore_errors=True)
