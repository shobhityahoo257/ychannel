"""Monetization guard-rails.

YouTube pays only channels in the Partner Programme (YPP) and demonetizes "inauthentic /
mass-produced / repetitious" content. These checks make every upload carry original editorial
value, corroborated sources and credited media, and track progress toward the YPP thresholds.
Nothing here guarantees approval or income - that is YouTube's decision.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .models import Asset, Script, Topic


@dataclass
class Issue:
    level: str   # "block" | "warn"
    msg: str


def _ngrams(text: str, n: int) -> set[tuple[str, ...]]:
    w = re.findall(r"\w+", text.lower())
    return {tuple(w[i:i + n]) for i in range(len(w) - n + 1)}


def _jaccard(a: str, b: str) -> float:
    x, y = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
    return len(x & y) / len(x | y) if x | y else 0.0


def policy_check(script: Script, topic: Topic, assets: list[Asset], history: list[dict],
                 max_per_day: int = 4, min_sources: int = 2) -> list[Issue]:
    issues: list[Issue] = []
    if len(topic.sources) < min_sources and topic.stories and topic.stories[0].source != "manual":  # manual = your own reporting
        issues.append(Issue("block", f"Only {len(topic.sources)} outlet(s) report this; need {min_sources} "
                                     "(uncorroborated political claims are a defamation/misinformation risk)."))
    if not any(s.kind == "analysis" for s in script.scenes):
        issues.append(Issue("block", "No analysis scene: pure headline-reading is 'mass-produced' content."))
    narration = " ".join(s.narration for s in script.scenes)
    manual = bool(topic.stories) and topic.stories[0].source == "manual"
    src_text = "" if manual else " ".join(f"{s.title}. {s.summary}" for s in topic.stories)
    if src_text and _ngrams(narration, 8) & _ngrams(src_text, 8):
        issues.append(Issue("block", "Narration copies 8+ consecutive words from a source; rewrite in your own words."))
    for h in history[-200:]:
        if _jaccard(script.title, h.get("title", "")) > 0.8:
            issues.append(Issue("block", f"Title nearly identical to an earlier upload: {h.get('title')}"))
            break
    today = datetime.now(timezone.utc).date().isoformat()
    n_today = sum(1 for h in history if h.get("date") == today)
    if n_today >= max_per_day:
        issues.append(Issue("block", f"Already {n_today} uploads today (limit {max_per_day}); "
                                     "high-volume posting triggers repetitious-content review."))
    if len(script.title) > 100:
        issues.append(Issue("block", "Title longer than 100 characters."))
    if re.search(r"!{2,}|[A-Z]{12,}", script.title):
        issues.append(Issue("warn", "Title looks clickbait-y; this hurts advertiser suitability."))
    if not any(a.kind == "user" for a in assets):
        issues.append(Issue("warn", "No photos supplied by you: add your own images for stronger originality."))
    return issues


YPP_FULL = {"subs": 1000, "watch_hours": 4000, "shorts_views_90d": 10_000_000}
YPP_EARLY = {"subs": 500, "watch_hours": 3000, "shorts_views_90d": 3_000_000, "uploads_90d": 3}


def ypp_progress(stats: dict[str, Any]) -> list[str]:
    subs = stats.get("subs", 0)
    hrs = stats.get("watch_hours_365d", 0.0)
    shorts = stats.get("shorts_views_90d", 0)

    def pct(v, t):
        return f"{min(100, 100 * v / t):.0f}%"

    return [
        f"Subscribers: {subs:,} / 1,000 ({pct(subs, 1000)})  [early tier: 500]",
        f"Public watch hours (12 mo): {hrs:,.0f} / 4,000 ({pct(hrs, 4000)})  [early tier: 3,000]",
        f"Shorts views (90 d): {shorts:,} / 10,000,000 ({pct(shorts, 10_000_000)})  [early tier: 3M]",
        "Eligible when subs >= 1,000 AND (watch hours >= 4,000 OR Shorts views >= 10M). "
        "Then apply in YouTube Studio -> Earn and link AdSense.",
    ]


def revenue_estimate(long_views: int, shorts_views: int, rpm_long=(0.3, 1.5), rpm_short=(0.01, 0.06)) -> str:
    """Illustrative only. Real RPM varies by audience country, season and ad suitability."""
    lo = long_views / 1000 * rpm_long[0] + shorts_views / 1000 * rpm_short[0]
    hi = long_views / 1000 * rpm_long[1] + shorts_views / 1000 * rpm_short[1]
    return f"Illustrative creator revenue for these views: ${lo:,.0f} - ${hi:,.0f} (RPM assumptions, not a promise)"


def window(days: int) -> tuple[str, str]:
    end = datetime.now(timezone.utc).date()
    return (end - timedelta(days=days)).isoformat(), end.isoformat()
