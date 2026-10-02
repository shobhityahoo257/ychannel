"""Breaking-news alerts.

Every few minutes: fetch fresh stories, and ONLY if something new arrived ask the curator which
topics are reported by enough outlets and are important enough. For each new qualifying topic,
make a short breaking Short and ping you (Telegram) for approval. Nothing is ever published
without your approval, and a daily cap plus de-duplication prevent alert spam."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import curate as C
from . import sources
from .config import Config
from .models import Story, Topic
from .review import Store


@dataclass
class State:
    seen: dict[str, float] = field(default_factory=dict)          # story id -> first seen (unix)
    alerted: list[dict[str, Any]] = field(default_factory=list)    # {title, story_ids, ts, run_id}
    last_check: float = 0.0

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            return cls(**json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path, keep_hours: float = 48) -> None:
        cut = time.time() - keep_hours * 3600
        self.seen = {k: v for k, v in self.seen.items() if v >= cut}
        self.alerted = [a for a in self.alerted if a["ts"] >= cut]
        path.write_text(json.dumps(self.__dict__, ensure_ascii=False, indent=1), encoding="utf-8")

    def already_alerted(self, topic: Topic) -> bool:
        ids = {s.id for s in topic.stories}
        return any(ids & set(a["story_ids"]) for a in self.alerted)

    def alerts_today(self, now: float, tz: ZoneInfo) -> int:
        day = datetime.fromtimestamp(now, tz).date()
        return sum(1 for a in self.alerted if datetime.fromtimestamp(a["ts"], tz).date() == day)


def in_active_hours(spec: str, now: datetime) -> bool:
    start, end = spec.split("-")
    cur = now.strftime("%H:%M")
    return start <= cur <= end


class BreakingWatcher:
    def __init__(self, cfg: Config, client: Any, store: Store,
                 fetch: Callable[..., list[Story]] | None = None,
                 make: Callable[[Topic], dict[str, Any]] | None = None,
                 now: Callable[[], float] = time.time, log: Callable[[str], None] = print):
        self.cfg, self.client, self.store, self.log, self.now = cfg, client, store, log, now
        self.b = cfg.get("breaking", {})
        self.tz = ZoneInfo(cfg.get("schedule", {}).get("timezone", "Asia/Kolkata"))
        self.fetch = fetch or sources.fetch_stories
        self.make = make
        self.state_path = store.root / "breaking_state.json"
        self.state = State.load(self.state_path)

    def detect(self) -> list[Topic]:
        """Qualifying, not-yet-alerted topics that include at least one story we have not seen before."""
        b = self.b
        stories = self.fetch(self.cfg["feeds"], b.get("window_hours", 3))
        t = self.now()
        new_ids = {s.id for s in stories if s.id not in self.state.seen}
        for s in stories:
            self.state.seen.setdefault(s.id, t)
        self.state.last_check = t
        if not new_ids:                                   # nothing new -> no LLM call, no cost
            self.state.save(self.state_path)
            return []
        covered = [h["topic"] for h in self.store.history()] + [a["title"] for a in self.state.alerted]
        topics = C.curate(self.client, self.cfg.models()[0], stories, b.get("min_sources", 3), covered,
                          self.cfg["curation"]["candidates_for_llm"], top=10)
        hits = [x for x in topics if x.importance >= b.get("min_importance", 7)
                and not self.state.already_alerted(x) and any(s.id in new_ids for s in x.stories)]
        hits.sort(key=lambda x: -x.importance)
        return hits

    def run_once(self) -> list[dict[str, Any]]:
        """One check. Returns the metas of videos made for new breaking stories."""
        cap = self.b.get("max_alerts_per_day", 3)
        local = datetime.fromtimestamp(self.now(), self.tz)
        if not in_active_hours(self.b.get("active_hours", "00:00-23:59"), local):
            return []
        made: list[dict[str, Any]] = []
        for topic in self.detect():
            if self.state.alerts_today(self.now(), self.tz) >= cap:
                self.log(f"[breaking] daily alert cap ({cap}) reached; skipping '{topic.title}'")
                break
            self.log(f"[breaking] 🚨 {topic.title} ({len(topic.sources)} outlets, importance {topic.importance})")
            try:
                meta = self.make(topic) if self.make else self._produce(topic)
            except Exception as exc:                     # still mark it, so we don't retry-loop on a bad story
                self.log(f"[breaking] could not make a video: {exc}")
                meta = {"id": ""}
            self.state.alerted.append({"title": topic.title, "story_ids": [s.id for s in topic.stories],
                                       "ts": self.now(), "run_id": meta.get("id", "")})
            if meta.get("id"):
                made.append(meta)
        self.state.save(self.state_path)
        return made

    def _produce(self, topic: Topic) -> dict[str, Any]:
        from .daily import notify
        from .pipeline import produce
        from .tts import make_tts
        meta = produce(self.cfg, topic, "short", self.client, make_tts(self.cfg), self.store,
                       breaking=True, log=self.log)
        notify(self.cfg, meta)
        return meta


def watch(cfg: Config, log: Callable[[str], None] = print) -> None:
    from .llm import make_client
    store = Store(cfg.path(cfg["youtube"]["output_dir"]))
    w = BreakingWatcher(cfg, make_client(cfg), store, log=log)
    every = cfg.get("breaking", {}).get("poll_minutes", 5) * 60
    log(f"Watching for breaking news every {every // 60} min "
        f"(>= {w.b.get('min_sources', 3)} outlets, importance >= {w.b.get('min_importance', 7)}). Ctrl+C to stop.")
    while True:
        try:
            w.run_once()
        except Exception as exc:
            log(f"[breaking] check failed: {exc}")
        time.sleep(every)
