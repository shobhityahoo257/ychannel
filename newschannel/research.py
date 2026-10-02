"""Collect source texts for a topic: the articles the feeds found, plus URLs and notes you give."""
from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

import requests

from .ledger import Source, classify_tier, outlet_of
from .models import Topic

UA = "Mozilla/5.0 (compatible; NewsChannelBot/0.1; +research)"
MAX_CHARS = 14000
_SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "figure"}


class _Text(HTMLParser):
    """Pulls the title, publish date and paragraph text out of an article page (stdlib only)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title, self.published = "", ""
        self.paras: list[str] = []
        self._skip = 0
        self._in_title = False
        self._in_p = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in _SKIP:
            self._skip += 1
        elif tag == "title":
            self._in_title = True
        elif tag in ("p", "h2", "h3", "li", "blockquote") and not self._skip:
            self._in_p, self._buf = True, []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key in ("article:published_time", "datepublished", "pubdate", "date") and a.get("content"):
                self.published = self.published or a["content"][:25]
            if key == "og:title" and a.get("content"):
                self.title = a["content"]

    def handle_endtag(self, tag):
        if tag in _SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in ("p", "h2", "h3", "li", "blockquote") and self._in_p:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(text) >= 40:
                self.paras.append(text)
            self._in_p = False

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()
        if self._in_p and not self._skip:
            self._buf.append(data)


def extract_article(html: str) -> tuple[str, str, str]:
    p = _Text()
    p.feed(html)
    seen, out = set(), []
    for para in p.paras:
        if para not in seen:
            seen.add(para)
            out.append(para)
    return p.title.strip(), p.published, "\n".join(out)[:MAX_CHARS]


def fetch_article(url: str, timeout: int = 20) -> tuple[str, str, str]:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    if "pdf" in r.headers.get("content-type", ""):
        raise ValueError("PDF sources are not supported yet; paste the text as a note instead")
    r.encoding = r.encoding or r.apparent_encoding
    return extract_article(r.text)


def gather_sources(cfg: Any, topic: Topic, urls: list[str] | None = None, notes: str = "",
                   max_sources: int = 10, log: Callable[[str], None] = print,
                   fetch: Callable[[str], tuple[str, str, str]] | None = None) -> list[Source]:
    fetch = fetch or fetch_article
    sc = cfg.get("analysis", {}).get("sources", {})
    t1, t2 = sc.get("tier1"), sc.get("tier2")
    links: list[str] = []
    for u in [*(urls or []), *[s.link for s in topic.stories if s.link]]:
        u = u.strip()
        if u.startswith("http") and u not in links:
            links.append(u)
    out: list[Source] = []
    for u in links[:max_sources * 2]:
        if len(out) >= max_sources:
            break
        try:
            title, published, text = fetch(u)
        except Exception as exc:
            log(f"[research] could not read {u}: {exc}")
            continue
        if len(text) < 300:
            log(f"[research] too little text at {u}; skipped")
            continue
        out.append(Source(f"S{len(out) + 1}", u, title or u, outlet_of(u), classify_tier(u, t1, t2), published, text))
    if notes.strip():       # your own notes are never enough on their own: weak tier unless other sources back them
        out.append(Source(f"S{len(out) + 1}", "", "User notes", "user notes", 3, "", notes.strip()[:MAX_CHARS]))
    return out


def save_sources(sources: list[Source], folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for s in sources:
        (folder / f"{s.id}.txt").write_text(f"{s.url}\n{s.title}\n\n{s.text}", encoding="utf-8")
