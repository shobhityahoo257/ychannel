"""Packaging: the parts of a video that decide whether people click and keep watching.

One LLM call proposes several opening hooks, titles and thumbnail texts and scores them. The best
hook replaces the first scene and the best title becomes the title - but only if they pass
fact-safety checks (no new numbers, sane length). All options are kept so you can switch in the UI."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .i18n import LANG_RULES, norm
from .llm import call_tool
from .models import Script, Topic

SYSTEM = """You are a YouTube growth editor for {channel_kind}. Using ONLY the facts
in the story text given (never invent facts, numbers, names or quotes), produce:
1. hooks: 5 alternative spoken openings for the first scene (in the OUTPUT LANGUAGE below). Each is 8-22 words, makes the
   viewer need the next sentence within 3 seconds (stakes, tension, a surprising fact FROM THE SOURCES, or a question),
   in plain spoken language, no shouting. It must still state what the story is about.
2. titles: 5 YouTube titles (OUTPUT LANGUAGE), max 70 characters, specific and curiosity-driven, each a different angle
   (stakes / question / number-from-sources / consequence / contrast). No lies, no clickbait promises the video
   does not keep, no ALL-CAPS, no abusive or communal language.
3. category: the ONE best-fitting category from the allowed list (if a list is given), else empty.
4. thumb_texts: 3 thumbnail texts (OUTPUT LANGUAGE), 2-5 words each, punchy, readable at small size.
Score every hook and title 1-10 on: grabs attention, honest to the sources, clear. Be strict: most should score 4-7."""

SCHEMA = {
    "type": "object",
    "properties": {
        "hooks": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "score": {"type": "number"}}, "required": ["text", "score"]}},
        "titles": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "angle": {"type": "string"}, "score": {"type": "number"}},
            "required": ["text", "score"]}},
        "thumb_texts": {"type": "array", "items": {"type": "string"}},
        "category": {"type": "string"},
    },
    "required": ["hooks", "titles", "thumb_texts"],
}

_DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


@dataclass
class Packaging:
    hooks: list[dict[str, Any]] = field(default_factory=list)
    titles: list[dict[str, Any]] = field(default_factory=list)
    thumb_texts: list[str] = field(default_factory=list)
    chosen_hook: str = ""
    chosen_title: str = ""
    category: str = ""


def numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?", text.translate(_DEV_DIGITS)))


def _safe(candidate: str, allowed_numbers: set[str]) -> bool:
    return numbers(candidate) <= allowed_numbers


CHANNEL_KIND = {"explainer": "a global economics-and-business explainer channel (honest, curiosity-driven, never promising returns)",
                "": "an Indian political-news channel"}


def improve(client: Any, model: str, script: Script, topic: Topic, insights: str = "",
            categories: list[str] | None = None, lang: str = "hinglish", rewrite_hook: bool = True,
            style: str = "") -> Packaging:
    """Rewrite script.scenes[0].narration and script.title in place with the best safe options."""
    facts = " ".join(f"{s.title}. {s.summary}" for s in topic.stories[:6])
    first = script.scenes[0]
    prompt = (f"Story title: {topic.title}\nSource facts:\n{facts}\n\n"
              f"Current first scene (spoken): {first.narration}\nCurrent title: {script.title}\n"
              f"Whole script, for context:\n" + "\n".join(s.narration for s in script.scenes if s.narration))
    if categories:
        prompt += "\n\nAllowed categories (pick exactly one): " + " | ".join(categories)
    if insights:
        prompt += f"\n\nWhat has worked on this channel (use for style only, never for facts):\n{insights}"
    try:
        system = SYSTEM.format(channel_kind=CHANNEL_KIND.get(style, CHANNEL_KIND[""]))
        res = call_tool(client, model, system + "\n\n" + LANG_RULES[norm(lang)], prompt, "submit_packaging", SCHEMA, 3000)
    except Exception as exc:                       # packaging is an upgrade, never a blocker
        print(f"[packaging] skipped: {exc}")
        return Packaging()
    pk = Packaging(hooks=sorted(res.get("hooks", []), key=lambda h: -h.get("score", 0)),
                   titles=sorted(res.get("titles", []), key=lambda t: -t.get("score", 0)),
                   thumb_texts=[t.strip() for t in res.get("thumb_texts", []) if t.strip()][:3],
                   category=res.get("category", "") if res.get("category", "") in (categories or []) else "")
    allowed = numbers(facts + " " + script.title + " " + " ".join(s.narration for s in script.scenes))
    old_words = max(1, len(first.narration.split()))
    for h in pk.hooks:
        text = h["text"].strip()
        n = len(text.split())
        if 6 <= n <= 28 and n <= old_words * 1.8 + 6 and _safe(text, allowed):
            pk.chosen_hook = text
            break
    if rewrite_hook and pk.chosen_hook and first.kind != "clip":
        first.narration = pk.chosen_hook
    for t in pk.titles:
        text = t["text"].strip()
        if 8 <= len(text) <= 100 and _safe(text, allowed) and not re.search(r"!{2,}", text):
            pk.chosen_title = text
            script.title = text
            break
    return pk
