"""Find sources for an evergreen topic without a search-engine key.

Wikipedia is NOT used as a source of facts (anyone can edit it). It is used as a map: the articles on a topic list the
primary sources they cite - central banks, statistics offices, filings, major outlets. We follow those links and keep only
the ones the ledger rates official or major-outlet; the claims then come from the real source and are verified against it."""
from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .ledger import classify_tier, original_url, outlet_of

API = "https://en.wikipedia.org/w/api.php"
UA = {"User-Agent": "Mozilla/5.0 (compatible; ExplainerBot/0.1; +research)"}
SKIP_HOSTS = ("wikipedia.org", "wikimedia.org", "wikidata.org", "doi.org", "jstor.org", "books.google.com", "worldcat.org")


def _api(params: dict[str, Any], http: Callable | None = None) -> Any:
    r = (http or requests.get)(API, params={**params, "format": "json", "formatversion": 2}, headers=UA, timeout=25)
    r.raise_for_status()
    return r.json()


def search_titles(query: str, n: int = 3, http: Callable | None = None) -> list[str]:
    res = _api({"action": "query", "list": "search", "srsearch": query, "srlimit": n}, http)
    return [x["title"] for x in res.get("query", {}).get("search", [])]


def reference_links(title: str, http: Callable | None = None) -> list[str]:
    res = _api({"action": "parse", "page": title, "prop": "externallinks", "redirects": 1}, http)
    return [u for u in res.get("parse", {}).get("externallinks", []) if u.startswith("http")]


def rank_links(links: list[str], tier1: list[str] | None = None, tier2: list[str] | None = None,
               limit: int = 8) -> list[tuple[str, int]]:
    """Official sources first, then major outlets. One link per page, at most 2 per site, no PDFs."""
    seen: set[str] = set()
    per_host: dict[str, int] = {}
    scored: list[tuple[int, int, str]] = []
    for i, u in enumerate(links):
        real = original_url(u)
        host = outlet_of(real)
        if not host or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
            continue
        tier = classify_tier(u, tier1, tier2)
        path = urlparse(real).path.lower()
        if tier > 2 or path.endswith((".pdf", ".xls", ".xlsx", ".csv", ".zip")) or path in ("", "/"):
            continue
        key = real.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        scored.append((tier, i, u))
    out: list[tuple[str, int]] = []
    for tier, _, u in sorted(scored):
        host = outlet_of(original_url(u))
        if per_host.get(host, 0) >= 2:
            continue
        per_host[host] = per_host.get(host, 0) + 1
        out.append((u, tier))
        if len(out) >= limit:
            break
    return out


def discover(topic: str, tier1: list[str] | None = None, tier2: list[str] | None = None, limit: int = 8,
             log: Callable[[str], None] = print, http: Callable | None = None) -> list[str]:
    """URLs of official / major-outlet pages cited by the Wikipedia articles on `topic`."""
    links: list[str] = []
    try:
        titles = search_titles(topic, 3, http)
    except Exception as exc:
        log(f"[discover] could not search: {exc}")
        return []
    for title in titles:
        try:
            got = reference_links(title, http)
        except Exception as exc:
            log(f"[discover] could not read references of '{title}': {exc}")
            continue
        log(f"  {title}: {len(got)} cited links")
        links += got
    ranked = rank_links(links, tier1, tier2, limit)
    log(f"  keeping {len(ranked)} official / major-outlet sources")
    return [u for u, _ in ranked]
