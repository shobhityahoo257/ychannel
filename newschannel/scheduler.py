"""Unattended mode: `python -m newschannel schedule`.

Every day: produce videos at `produce_at`, collect your Approve/Reject taps from Telegram, and publish
approved videos one at a time at each `publish_slots` time. Nothing is published without approval."""
from __future__ import annotations

import json
import time
from datetime import datetime, time as dtime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .config import Config
from .daily import next_approved, publish_one, run_daily
from .review import Store, Telegram

CATCH_UP_MINUTES = 30      # a slot missed by more than this (e.g. laptop was asleep) is skipped, not run late


def hhmm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class Scheduler:
    def __init__(self, cfg: Config, now: Callable[[], datetime] | None = None,
                 produce: Callable[[], Any] | None = None,
                 publish: Callable[[dict[str, Any]], str] | None = None,
                 poll: Callable[[], Any] | None = None, log: Callable[[str], None] = print,
                 breaking: Callable[[], Any] | None = None):
        sc = cfg.get("schedule", {})
        self.tz = ZoneInfo(sc.get("timezone", "Asia/Kolkata"))
        self.produce_at = hhmm(sc.get("produce_at", "05:30"))
        self.slots = [hhmm(x) for x in sc.get("publish_slots", ["07:30", "12:30", "18:30"])]
        self.cfg, self.log = cfg, log
        self.store = Store(cfg.path(cfg["youtube"]["output_dir"]))
        self.max_per_day = cfg.get("limits", {}).get("max_uploads_per_day", 4)
        self.now = now or (lambda: datetime.now(self.tz))
        self.produce = produce or (lambda: run_daily(cfg, log))
        self.publish = publish or (lambda meta: publish_one(cfg, self.store, meta))
        tg = Telegram()
        self.poll = poll or ((lambda: tg.poll_once(self.store)) if tg.enabled else (lambda: 0))
        self.state_file = self.store.root / "scheduler_state.json"
        self.breaking = breaking
        self.breaking_every = cfg.get("breaking", {}).get("poll_minutes", 5) * 60
        self._last_breaking = 0.0

    # -- persistent "already ran" marks so a restart never repeats a job
    def _done(self) -> set[str]:
        try:
            return set(json.loads(self.state_file.read_text()))
        except (OSError, ValueError):
            return set()

    def _mark(self, key: str) -> None:
        done = {k for k in self._done() if k.split("|")[0] >= self.now().date().isoformat()}
        done.add(key)
        self.state_file.write_text(json.dumps(sorted(done)))

    def _due(self, at: dtime, name: str) -> bool:
        n = self.now()
        key = f"{n.date().isoformat()}|{name}"
        if key in self._done():
            return False
        minutes = (n.hour * 60 + n.minute) - (at.hour * 60 + at.minute)
        if 0 <= minutes <= CATCH_UP_MINUTES:
            self._mark(key)
            return True
        return False

    def uploads_today(self) -> int:
        today = self.now().astimezone(ZoneInfo("UTC")).date().isoformat()
        return sum(1 for h in self.store.history() if h.get("date") == today)

    def tick(self) -> list[str]:
        """One scheduler step; returns what it did (handy for tests and logs)."""
        did: list[str] = []
        try:
            self.poll()
        except Exception as exc:
            self.log(f"[scheduler] telegram poll failed: {exc}")
        if self.breaking and time.time() - self._last_breaking >= self.breaking_every:
            self._last_breaking = time.time()
            try:
                if self.breaking():
                    did.append("breaking")
            except Exception as exc:
                self.log(f"[scheduler] breaking check failed: {exc}")
        if self._due(self.produce_at, "produce"):
            did.append("produce")
            self.log("[scheduler] producing today's videos…")
            try:
                self.produce()
            except Exception as exc:
                self.log(f"[scheduler] production failed: {exc}")
        for slot in self.slots:
            if self._due(slot, f"publish-{slot:%H:%M}"):
                meta = next_approved(self.store)
                if meta is None:
                    self.log(f"[scheduler] {slot:%H:%M}: nothing approved to publish")
                elif self.uploads_today() >= self.max_per_day:
                    self.log(f"[scheduler] {slot:%H:%M}: daily upload limit reached")
                else:
                    try:
                        vid = self.publish(meta)
                        did.append(f"publish:{vid}")
                        self.log(f"[scheduler] published https://youtu.be/{vid}")
                    except Exception as exc:
                        self.log(f"[scheduler] publish failed: {exc}")
        return did


def run_forever(cfg: Config, interval: int = 30) -> None:
    breaking = None
    if cfg.get("schedule", {}).get("breaking", True):
        from .breaking import BreakingWatcher
        from .llm import make_client
        w = BreakingWatcher(cfg, make_client(cfg), Store(cfg.path(cfg["youtube"]["output_dir"])))
        breaking = w.run_once
    s = Scheduler(cfg, breaking=breaking)
    print(f"Scheduler running ({s.tz.key}): produce {s.produce_at:%H:%M}, publish {[f'{x:%H:%M}' for x in s.slots]}. "
          "Ctrl+C to stop. Keep this computer awake.")
    while True:
        s.tick()
        time.sleep(interval)
