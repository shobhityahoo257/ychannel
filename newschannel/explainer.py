"""Economics & business explainers: branding profile, topic ideas, series templates.

An explainer is a deep-analysis video (same fact ledger, same code-checked script) with a different job: teach how something
works, for a global English-speaking audience, and stay evergreen. Everything specific to it is in `config.yaml -> explainer`."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Callable

from . import data as data_mod
from .config import Config
from .llm import call_tool

SERIES: list[dict[str, str]] = [
    {"id": "how_it_works", "name": "How it works", "formula": "How [thing] actually works",
     "hint": "A mechanism explained step by step: central banks, exchange rates, supply chains, tariffs, credit."},
    {"id": "why_it_happened", "name": "Why it happened", "formula": "Why [event] happened",
     "hint": "A crisis, boom or policy change: the causes in order, with the data."},
    {"id": "rise_and_fall", "name": "Rise and fall", "formula": "The rise and fall of [company or industry]",
     "hint": "A business story told from filings, reports and dated turning points."},
    {"id": "country_in_numbers", "name": "Country in numbers", "formula": "[Country]'s economy in charts",
     "hint": "Data-led: growth, inflation, trade, debt, compared with peers. Uses World Bank charts."},
    {"id": "myth_vs_data", "name": "Myth vs data", "formula": "Is it true that [popular claim]? What the data shows",
     "hint": "Tests a popular belief against official data."},
    {"id": "who_pays", "name": "Who pays", "formula": "Who really pays for [policy or price]",
     "hint": "Follows the money: winners, losers and trade-offs."},
]

DEFAULT = {
    "language": "english",
    "niche": "economics and business explained through history and data",
    "channel": {"name": "Money Explained", "handle": "@MoneyExplained", "accent_color": "#F5B700", "dark_color": "#0A1128"},
    "youtube": {"category_id": "27", "language": "en",
                "default_tags": ["economics", "business", "explained", "finance", "economy", "history of money"]},
    "playlists": {"categories": ["Economy Explained", "Business Stories", "Money and Markets", "Trade and Geopolitics",
                                 "Economic History"], "format_playlists": {"analysis": "Explainers"}},
    "discover": True,
    "data_years": 15,
}


def channel_of(style: str | None) -> str:
    """Which channel a video belongs to: "explainer" or "" (the news / political-analysis channel)."""
    return "explainer" if style == "explainer" else ""


def settings(cfg: Config) -> dict[str, Any]:
    out = copy.deepcopy(DEFAULT)
    for k, v in (cfg.get("explainer") or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def apply_profile(cfg: Config, style: str, language: str | None = None) -> None:
    """Explainers have their own channel look, YouTube category, playlists and default language. Changes `cfg` in place."""
    if style != "explainer":
        return
    ex = settings(cfg)
    cfg.data["channel"] = {**cfg["channel"], **ex["channel"]}
    cfg.data["youtube"] = {**cfg["youtube"], "category_id": ex["youtube"]["category_id"],
                           "default_tags": ex["youtube"]["default_tags"]}
    cfg.data["playlists"] = {**(cfg.get("playlists") or {}), **ex["playlists"]}
    cfg.data.setdefault("content", {})["language"] = language or ex["language"]


def profile_copy(cfg: Config, style: str, language: str | None = None) -> Config:
    """A private copy with the profile applied, for code that must not change the shared config (publishing)."""
    c = Config(copy.deepcopy(cfg.data), cfg.root)
    if language:
        c.data.setdefault("content", {})["language"] = language
    apply_profile(c, style, language)
    return c


# ----------------------------------------------------------------------------- topic ideas
IDEAS_SYSTEM = """You are the research editor of a YouTube channel that explains {niche} to a global audience of curious non-experts.
Propose video topics that people search for and that stay interesting for years (evergreen), where the answer can be built from
official statistics, central-bank / institution publications, company filings and reputable reporting.

For each idea give:
- title: a working title in plain English, max 70 characters, NO numbers and NO factual claims (you have not researched anything; facts come later).
- question: the one question the viewer wants answered, in plain words.
- series: one of {series_ids}.
- why: one sentence on why people would click and keep watching.
- data_hooks: 0-2 items {{country, indicators}} naming World Bank series that would make a good chart. Indicators must be from: {indicators}.
- source_hints: 2-4 kinds of primary source to read (e.g. "IMF World Economic Outlook", "central bank annual report", "SEC filings").
- difficulty: easy | medium | hard (how hard it is to source with confidence).
Prefer topics with a clear mechanism or turning point, a visual story (charts, maps, archival photos), and genuine disagreement or nuance.
Avoid: stock tips, predictions of prices, crypto promotion, personal-finance advice, topics about named private individuals, and anything needing leaked or unverifiable information.
Do not repeat or lightly reword the topics already made. Return exactly {n} ideas."""

IDEAS_SCHEMA = {"type": "object", "properties": {"ideas": {"type": "array", "items": {"type": "object", "properties": {
    "title": {"type": "string"}, "question": {"type": "string"}, "series": {"type": "string"}, "why": {"type": "string"},
    "data_hooks": {"type": "array", "items": {"type": "object", "properties": {
        "country": {"type": "string"}, "indicators": {"type": "array", "items": {"type": "string"}}}}},
    "source_hints": {"type": "array", "items": {"type": "string"}},
    "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]}},
    "required": ["title", "question", "series"]}}}, "required": ["ideas"]}


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", s.lower())}


def _similar(a: str, b: str) -> bool:
    x, y = _words(a), _words(b)
    return bool(x and y) and len(x & y) / len(x | y) > 0.6


def clean_ideas(raw: list[dict[str, Any]], taken: list[str], n: int) -> list[dict[str, Any]]:
    """Code-side rules: no digits in titles (nothing is researched yet), valid series / indicators, no repeats."""
    ids = {s["id"] for s in SERIES}
    out: list[dict[str, Any]] = []
    for it in raw:
        title = (it.get("title") or "").strip()
        if not title or len(title) > 100 or re.search(r"\d", title):
            continue
        if any(_similar(title, t) for t in [*taken, *(o["title"] for o in out)]):
            continue
        hooks = []
        for h in it.get("data_hooks") or []:
            inds = [i for i in h.get("indicators", []) if i in data_mod.INDICATORS]
            if h.get("country") and inds:
                hooks.append({"country": str(h["country"]).strip(), "indicators": inds[:3]})
        out.append({"title": title, "question": (it.get("question") or "").strip(),
                    "series": it.get("series") if it.get("series") in ids else "how_it_works",
                    "why": (it.get("why") or "").strip(), "data_hooks": hooks[:2],
                    "source_hints": [str(x) for x in (it.get("source_hints") or [])][:4],
                    "difficulty": it.get("difficulty") if it.get("difficulty") in ("easy", "medium", "hard") else "medium"})
        if len(out) >= n:
            break
    return out


class Ideas:
    """Remembers which topics were already suggested or made, so the bank never repeats."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def save(self, items: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    def taken(self, history: list[dict[str, Any]] | None = None) -> list[str]:
        return [i["title"] for i in self.load()] + [h.get("title", "") for h in (history or []) if h.get("title")]

    def add(self, ideas: list[dict[str, Any]]) -> None:
        items = self.load()
        items += [{**i, "status": "suggested"} for i in ideas]
        self.save(items[-300:])

    def mark_made(self, title: str) -> None:
        items = self.load()
        for i in items:
            if _similar(i["title"], title):
                i["status"] = "made"
        self.save(items)


def suggest(cfg: Config, client: Any, ideas: Ideas, n: int = 8, focus: str = "", series: str = "",
            history: list[dict[str, Any]] | None = None, log: Callable[[str], None] = print) -> list[dict[str, Any]]:
    ex = settings(cfg)
    taken = ideas.taken(history)
    system = IDEAS_SYSTEM.format(niche=ex["niche"], series_ids=", ".join(s["id"] for s in SERIES), n=n + 4,
                                 indicators=", ".join(data_mod.INDICATORS))
    prompt = (f"Series formats:\n" + "\n".join(f"- {s['id']}: {s['formula']} - {s['hint']}" for s in SERIES)
              + (f"\n\nEditor's focus for this batch: {focus}" if focus.strip() else "")
              + (f"\nUse only the series '{series}'." if series else "")
              + ("\n\nAlready made or suggested (do not repeat):\n" + "\n".join(f"- {t}" for t in taken[-80:]) if taken else ""))
    log("thinking of topics…")
    res = call_tool(client, cfg.models()[0], system, prompt, "submit_ideas", IDEAS_SCHEMA, 4000)
    out = clean_ideas(res.get("ideas", []), taken, n)
    if out:
        ideas.add(out)
    return out
