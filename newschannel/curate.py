from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .llm import call_tool
from .models import Story, Topic

SYSTEM = """You are the news editor of a Hindi YouTube channel about Indian politics.
From the numbered headlines (Hindi and English, from different outlets) do three things:
1. Keep ONLY Indian political / government / election / policy news. Drop sports, crime, entertainment, foreign news.
2. Group headlines that report the SAME event, even across languages.
3. Rank groups by public importance and by how many different outlets report them.
Never merge different events. Do not invent facts. Skip topics listed as already covered."""

SCHEMA = {
    "type": "object",
    "properties": {"topics": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Neutral one-line English title of the event"},
            "ids": {"type": "array", "items": {"type": "integer"}},
            "why": {"type": "string", "description": "Why it matters, one sentence"},
        },
        "required": ["title", "ids"]}}},
    "required": ["topics"],
}

_STOP = {"the", "a", "of", "in", "to", "and", "for", "on", "is", "at", "by", "with", "की", "के", "का", "में", "से", "ने", "को", "पर"}


def _tokens(t: str) -> set[str]:
    return {w for w in re.findall(r"\w+", t.lower()) if w not in _STOP and len(w) > 2}


def heuristic_group(stories: list[Story], threshold: float = 0.35) -> list[Topic]:
    """Offline fallback: group same-language headlines by token overlap."""
    topics: list[tuple[set[str], Topic]] = []
    for s in stories:
        tk = _tokens(s.title)
        for ttk, topic in topics:
            if tk and ttk and len(tk & ttk) / len(tk | ttk) >= threshold:
                topic.stories.append(s)
                ttk |= tk
                break
        else:
            topics.append((set(tk), Topic(title=s.title, stories=[s])))
    return [t for _, t in topics]


def load_history(path: Path) -> list[str]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def curate(client: Any, model: str, stories: list[Story], min_sources: int = 2,
           covered: list[str] | None = None, limit: int = 40, top: int = 8) -> list[Topic]:
    stories = stories[:limit]
    if client is None:
        topics = heuristic_group(stories)
    else:
        listing = "\n".join(f"{i} | {s.source} | {s.title} | {s.summary[:160]}"
                            for i, s in enumerate(stories))
        done = "\n".join(covered or []) or "(none)"
        res = call_tool(client, model, SYSTEM,
                        f"Already covered:\n{done}\n\nHeadlines:\n{listing}", "pick_topics", SCHEMA)
        topics = []
        for t in res["topics"]:
            members = [stories[i] for i in t["ids"] if 0 <= i < len(stories)]
            if members:
                topics.append(Topic(title=t["title"], stories=members, why=t.get("why", "")))
    # corroboration: never publish a political claim that only one outlet reports
    ok = [t for t in topics if len(t.sources) >= min_sources]
    return ok[:top]
