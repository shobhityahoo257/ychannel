from __future__ import annotations

import hashlib
import html
import re
import time
from typing import Iterable

import feedparser
import requests

from .models import Story

UA = "Mozilla/5.0 (compatible; NewsChannelBot/0.1)"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def clean(text: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()


def parse_feed(content: bytes | str, source: str) -> list[Story]:
    parsed = feedparser.parse(content)
    out: list[Story] = []
    for e in parsed.entries:
        link = e.get("link", "")
        title = clean(e.get("title", ""))
        if not title or not link:
            continue
        ts = e.get("published_parsed") or e.get("updated_parsed")
        out.append(Story(
            id=hashlib.sha1(link.encode()).hexdigest()[:12],
            title=title,
            summary=clean(e.get("summary", ""))[:600],
            link=link,
            source=source,
            published=time.mktime(ts) if ts else 0.0,
        ))
    return out


def fetch_stories(feeds: Iterable[dict], max_age_hours: float = 24, timeout: int = 15) -> list[Story]:
    cutoff = time.time() - max_age_hours * 3600
    stories: list[Story] = []
    for f in feeds:
        try:
            r = requests.get(f["url"], headers={"User-Agent": UA}, timeout=timeout)
            r.raise_for_status()
            items = parse_feed(r.content, f["name"])
        except Exception as exc:  # one broken feed must not stop the run
            print(f"[feeds] {f['name']}: {exc}")
            continue
        stories += [s for s in items if s.published == 0.0 or s.published >= cutoff]
    seen: set[str] = set()
    unique = []
    for s in sorted(stories, key=lambda s: -s.published):
        if s.id not in seen:
            seen.add(s.id)
            unique.append(s)
    return unique
