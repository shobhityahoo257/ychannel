"""Analytics feedback loop: what did viewers actually watch? Feed the lessons back into scripting."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Callable

from .review import Store

MIN_VIEWS = 30      # ignore videos with too little data to judge
MIN_VIDEOS = 4      # need at least this many rated videos before we give advice


def perf_path(store: Store):
    return store.root / "performance.json"


def load_perf(store: Store) -> dict[str, dict[str, Any]]:
    try:
        return json.loads(perf_path(store).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def sync(store: Store, fetch: Callable[[list[str]], dict[str, dict[str, Any]]] | None = None) -> int:
    """Download stats for every published video. `fetch` defaults to the YouTube Analytics API."""
    ids = [h["video_id"] for h in store.history() if h.get("video_id")]
    if not ids:
        return 0
    if fetch is None:
        from .youtube import fetch_video_stats
        fetch = fetch_video_stats
    perf = load_perf(store)
    now = datetime.now(timezone.utc).isoformat()
    for vid, stats in fetch(ids).items():
        perf[vid] = {**stats, "synced": now}
    perf_path(store).write_text(json.dumps(perf, indent=2), encoding="utf-8")
    return len(perf)


def rows(store: Store) -> list[dict[str, Any]]:
    perf = load_perf(store)
    out = []
    for h in store.history():
        p = perf.get(h.get("video_id", ""))
        if p:
            out.append({**h, **p})
    return out


def insights(store: Store) -> dict[str, Any]:
    rated = [r for r in rows(store) if r.get("views", 0) >= MIN_VIEWS and r.get("avg_pct") is not None]
    by_fmt: dict[str, Any] = {}
    for fmt in ("short", "long"):
        sub = [r for r in rated if r.get("format") == fmt]
        if sub:
            by_fmt[fmt] = {"videos": len(sub), "avg_retention_pct": round(mean(r["avg_pct"] for r in sub), 1),
                           "avg_views": round(mean(r["views"] for r in sub))}
    by_ret = sorted(rated, key=lambda r: -r["avg_pct"])
    by_views = sorted(rated, key=lambda r: -r["views"])
    return {"rated": len(rated), "total": len(rows(store)), "by_format": by_fmt,
            "best_hooks": [r for r in by_ret[:3] if r.get("hook")],
            "worst_hooks": [r for r in by_ret[::-1][:2] if r.get("hook")] if len(by_ret) >= 6 else [],
            "best_titles": by_views[:3], "ready": len(rated) >= MIN_VIDEOS}


def prompt_hint(store: Store) -> str:
    """Short text for the script writer; empty until there is enough data."""
    ins = insights(store)
    if not ins["ready"]:
        return ""
    lines = []
    if ins["best_hooks"]:
        lines.append("Openings that kept viewers watching longest:")
        lines += [f"- ({r['avg_pct']:.0f}% watched) {r['hook'][:140]}" for r in ins["best_hooks"]]
    if ins["worst_hooks"]:
        lines.append("Openings viewers dropped quickly (avoid this style):")
        lines += [f"- ({r['avg_pct']:.0f}% watched) {r['hook'][:140]}" for r in ins["worst_hooks"]]
    if ins["best_titles"]:
        lines.append("Titles with the most views: " + " | ".join(r["title"] for r in ins["best_titles"]))
    return "\n".join(lines)
