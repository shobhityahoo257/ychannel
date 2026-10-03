"""Deep-analysis workflow: (1) research + fact-check -> ledger and recommendations, (2) write + render the video."""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Callable

from . import data as data_mod
from . import deep, discover as discover_mod, explainer, research
from .config import Config
from .ledger import Ledger
from .models import Story, Topic
from .pipeline import produce
from .review import Store

MIN_USABLE_CLAIMS = 4


class NotEnoughMaterial(RuntimeError):
    pass


def ledger_topic(ledger: Ledger) -> Topic:
    """A Topic view of the ledger so the shared pipeline (policy checks, packaging) can work with it."""
    stories = [Story(s.id, s.title, s.text[:400], s.url, s.outlet, 0.0) for s in ledger.sources if s.outlet != "user notes"]
    return Topic(ledger.topic, stories)


def research_dir(store: Store, rid: str) -> Path:
    d = store.root / "_research" / rid
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_research(cfg: Config, client: Any, store: Store, headline: str, urls: list[str] | None = None, notes: str = "",
                 topic: Topic | None = None, log: Callable[[str], None] = print,
                 fetch: Callable | None = None, style: str = "analysis", data: list[dict[str, Any]] | None = None,
                 discover: bool = False, http: Callable | None = None) -> dict[str, Any]:
    """Fetch sources, extract and verify claims, cross-check, and recommend an angle and a length.
    Explainers can also pull official data series (`data`) and follow the citations of Wikipedia articles (`discover`)."""
    model = cfg.models()[0]
    rid = uuid.uuid4().hex[:10]
    ac = cfg.get("analysis", {})
    topic = topic or Topic(headline, [])
    sc = ac.get("sources", {})
    urls = list(urls or [])
    if discover:
        log("looking for official sources…")
        urls += [u for u in discover_mod.discover(headline, sc.get("tier1"), sc.get("tier2"), log=log, http=http) if u not in urls]
    log("fetching sources…")
    kw = {"fetch": fetch} if fetch else {}
    series = data_mod.gather_data(data or [], years=explainer.settings(cfg)["data_years"], log=log, http=http) if data else []
    sources = research.gather_sources(cfg, topic, urls, notes, ac.get("max_sources", 10), log, data_sources=series, **kw)
    if not any(s.outlet != "user notes" for s in sources) and not notes.strip():
        raise NotEnoughMaterial("No readable sources. Add article URLs (official sites, major outlets) or paste your notes.")
    d = research_dir(store, rid)
    research.save_sources(sources, d / "sources")
    tz = cfg.get("schedule", {}).get("timezone", "Asia/Kolkata")
    ledger = deep.build_ledger(client, model, headline, sources, deep.as_of_text(tz, date_only=style == "explainer"), log, style)
    ledger.save(d / "ledger.json")
    log("deciding length…" if style == "explainer" else "deciding angle and length…")
    rec = deep.recommend(client, model, ledger, style)
    rec["style"] = style
    (d / "recommend.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    usable = len(ledger.usable())
    return {"research_id": rid, "headline": headline, "recommend": rec, "usable": usable, "style": style,
            "enough": usable >= MIN_USABLE_CLAIMS, "counts": ledger.counts(), "ledger": ledger.to_json(),
            "sources_found": len([s for s in sources if s.outlet != "user notes"])}


def load_research(store: Store, rid: str) -> tuple[Ledger, dict[str, Any]]:
    d = store.root / "_research" / rid
    return Ledger.load(d / "ledger.json"), json.loads((d / "recommend.json").read_text(encoding="utf-8"))


def make_video(cfg: Config, client: Any, tts: Any, store: Store, rid: str, stance: str = "auto",
               minutes: float | str = "auto", language: str | None = None, music: str | None = None,
               extra_images: list[Path] | None = None, library_ids: list[str] | None = None,
               preset: str | None = None, log: Callable[[str], None] = print,
               approved_only: bool = False, style: str = "analysis") -> dict[str, Any]:
    ledger, rec = load_research(store, rid)
    if len(ledger.usable()) < MIN_USABLE_CLAIMS:
        raise NotEnoughMaterial(f"Only {len(ledger.usable())} verified claims; need {MIN_USABLE_CLAIMS}+. Add more sources.")
    if language:
        cfg.data.setdefault("content", {})["language"] = language
    explainer.apply_profile(cfg, style, language)
    lang = cfg.lang
    stance = "neutral" if style == "explainer" else rec["stance"] if stance in ("auto", "", None) else stance
    if stance not in deep.STANCES:
        raise ValueError(f"stance must be one of {deep.STANCES}")
    mins = rec["minutes"] if minutes in ("auto", "", None) else float(minutes)
    mins = max(float(cfg.get("analysis", {}).get("min_minutes", 4.0)), min(20.0, mins))
    model = cfg.models()[0]
    log(f"writing the {stance} analysis script ({mins:g} min, {lang})…")
    w = deep.write_analysis(client, model, ledger, stance, mins, lang, cfg["channel"]["name"], cfg.get("analysis", {}),
                            as_of=ledger.as_of, log=log, style=style)
    if w.violations:
        log(f"⚠ the script still has {len(w.violations)} fact-check problem(s); it will be blocked until fixed")
    return produce(cfg, ledger_topic(ledger), "analysis", client, tts, store, extra_images, preset, log,
                   library_ids=library_ids, language=lang, script=w.script, ledger=ledger,
                   target_seconds=int(mins * 60), music=music, deep_check=(w.violations, w.warnings),
                   as_of=ledger.as_of, approved_only=approved_only)
