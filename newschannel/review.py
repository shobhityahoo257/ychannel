from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .config import Config


class Store:
    """Each video lives in output/<run-id>/ with a meta.json; history.json tracks what was published."""

    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        d = self.root / run_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def meta(self, run_id: str) -> dict[str, Any]:
        return json.loads((self.root / run_id / "meta.json").read_text(encoding="utf-8"))

    def save_meta(self, run_id: str, meta: dict[str, Any]) -> None:
        (self.root / run_id / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                                      encoding="utf-8")

    def runs(self, status: str | None = None) -> list[dict[str, Any]]:
        out = []
        for p in sorted(self.root.glob("*/meta.json")):
            m = json.loads(p.read_text(encoding="utf-8"))
            if status is None or m.get("status") == status:
                out.append(m)
        return out

    def set_status(self, run_id: str, status: str, **extra: Any) -> None:
        m = self.meta(run_id)
        m.update(status=status, **extra)
        self.save_meta(run_id, m)

    @property
    def history_path(self) -> Path:
        return self.root / "history.json"

    def history(self) -> list[dict[str, Any]]:
        try:
            return json.loads(self.history_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def add_history(self, title: str, topic: str, video_id: str, **extra: Any) -> None:
        h = self.history()
        now = datetime.now(timezone.utc)
        h.append({"title": title, "topic": topic, "video_id": video_id, "date": now.date().isoformat(),
                  "published_at": now.isoformat(), **extra})
        self.history_path.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------- Telegram approval
class Telegram:
    def __init__(self):
        self.token = Config.env("TELEGRAM_BOT_TOKEN")
        self.chat = Config.env("TELEGRAM_CHAT_ID")

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat)

    def _api(self, method: str, **kw):
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", timeout=90, **kw)
        r.raise_for_status()
        return r.json()["result"]

    def send_for_review(self, meta: dict[str, Any], video: Path) -> None:
        preview = video.parent / "preview.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(video), "-vf", "scale='min(540,iw)':-2",
                        "-c:v", "libx264", "-crf", "30", "-preset", "veryfast", "-c:a", "aac",
                        "-b:a", "64k", str(preview)], check=True)
        issues = "\n".join(f"- [{i['level']}] {i['msg']}" for i in meta.get("issues", [])) or "none"
        caption = f"{meta['title']}\n\nPolicy checks:\n{issues}"[:1000]
        kb = {"inline_keyboard": [[{"text": "✅ Approve", "callback_data": f"approve:{meta['id']}"},
                                   {"text": "❌ Reject", "callback_data": f"reject:{meta['id']}"}]]}
        with open(preview, "rb") as f:
            self._api("sendVideo", data={"chat_id": self.chat, "caption": caption,
                                         "reply_markup": json.dumps(kb), "supports_streaming": "true"},
                      files={"video": f})

    _offset: int | None = None

    def poll_once(self, store: Store, wait: int = 0) -> int:
        """Handle pending Approve/Reject button presses. Returns how many were applied."""
        params: dict[str, Any] = {"timeout": wait, "allowed_updates": json.dumps(["callback_query"])}
        if self._offset:
            params["offset"] = self._offset
        applied = 0
        for u in self._api("getUpdates", data=params):
            self._offset = u["update_id"] + 1
            cq = u.get("callback_query")
            if not cq or str(cq["message"]["chat"]["id"]) != str(self.chat):
                continue   # ignore anyone but the owner's chat
            action, run_id = cq["data"].split(":", 1)
            if action in ("approve", "reject"):
                store.set_status(run_id, "approved" if action == "approve" else "rejected")
                self._api("answerCallbackQuery", data={"callback_query_id": cq["id"], "text": action})
                applied += 1
        return applied

    def listen(self, store: Store, seconds: int = 600) -> None:
        """Long-poll for button presses and update run statuses."""
        end = time.time() + seconds
        while time.time() < end:
            self.poll_once(store, 30)
