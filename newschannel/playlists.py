"""Playlists, 'watch next' links and end-screen helpers.

What the YouTube API can do: create playlists, add videos to them, edit descriptions.
What it cannot do: create end screens or cards (Studio only). So we (1) bake an end-card layout into long
videos, (2) put 'watch next' links in descriptions, (3) link older videos forward to newer ones, and
(4) give you a one-click checklist for the Studio step."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def watch_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def playlist_url(pid: str) -> str:
    return f"https://www.youtube.com/playlist?list={pid}"


def target_playlists(meta: dict[str, Any], pl_cfg: dict) -> list[str]:
    """Names of the playlists this video belongs to: its topic category + its format."""
    names = []
    cat = meta.get("category", "")
    if cat and cat in pl_cfg.get("categories", []):
        names.append(cat)
    fmt = pl_cfg.get("format_playlists", {}).get(meta.get("format", ""))
    if fmt:
        names.append(fmt)
    return names


def related(history: list[dict[str, Any]], category: str, exclude: str = "", n: int = 3) -> list[dict[str, Any]]:
    """Most recent published videos in the same category (falls back to latest of any category)."""
    pub = [h for h in history if h.get("video_id") and h["video_id"] != exclude]
    pub.sort(key=lambda h: h.get("published_at", h.get("date", "")), reverse=True)
    same = [h for h in pub if category and h.get("category") == category]
    return (same + [h for h in pub if h not in same])[:n]


def watch_next_block(rel: list[dict[str, Any]], playlist_urls: list[str]) -> str:
    lines = []
    if rel:
        lines.append("▶ Watch next / और देखिए:")
        lines += [f"• {h['title']}: {watch_url(h['video_id'])}" for h in rel]
    if playlist_urls:
        lines.append("")
        lines += [f"📂 Full playlist: {u}" for u in playlist_urls]
    return "\n".join(lines)


def pinned_comment(next_video: dict[str, Any] | None) -> str:
    text = "आपकी क्या राय है? नीचे कमेंट में बताइए 👇"
    if next_video:
        text += f"\n\n▶ अगला वीडियो: {watch_url(next_video['video_id'])}"
    return text + "\n\nऐसी खबरों के लिए चैनल को सब्सक्राइब करना न भूलें।"


class Playlists:
    """Thin wrapper over the YouTube Data API (pass any object with the same shape for tests)."""

    def __init__(self, yt: Any, cache_path: Path):
        self.yt, self.cache_path = yt, cache_path
        try:
            self.cache: dict[str, str] = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.cache = {}

    def _save(self) -> None:
        self.cache_path.write_text(json.dumps(self.cache, ensure_ascii=False, indent=1), encoding="utf-8")

    def _existing(self) -> dict[str, str]:
        found: dict[str, str] = {}
        token = None
        while True:
            res = self.yt.playlists().list(part="snippet", mine=True, maxResults=50, pageToken=token).execute()
            for it in res.get("items", []):
                found[it["snippet"]["title"]] = it["id"]
            token = res.get("nextPageToken")
            if not token:
                return found

    def ensure(self, title: str, description: str = "", privacy: str = "public") -> str:
        if title in self.cache:
            return self.cache[title]
        pid = self._existing().get(title)
        if not pid:
            pid = self.yt.playlists().insert(part="snippet,status", body={
                "snippet": {"title": title, "description": description, "defaultLanguage": "hi"},
                "status": {"privacyStatus": privacy}}).execute()["id"]
        self.cache[title] = pid
        self._save()
        return pid

    def add(self, playlist_id: str, video_id: str) -> None:
        self.yt.playlistItems().insert(part="snippet", body={"snippet": {
            "playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}).execute()

    def append_description(self, video_id: str, extra: str) -> bool:
        """Add a line to an existing video's description (skips if already present)."""
        res = self.yt.videos().list(part="snippet", id=video_id).execute()
        items = res.get("items", [])
        if not items:
            return False
        sn = items[0]["snippet"]
        if extra in sn.get("description", ""):
            return False
        body = {"id": video_id, "snippet": {"title": sn["title"], "categoryId": sn["categoryId"],
                                            "description": (sn.get("description", "") + "\n\n" + extra)[:4900]}}
        for k in ("tags", "defaultLanguage", "defaultAudioLanguage"):
            if sn.get(k):
                body["snippet"][k] = sn[k]
        self.yt.videos().update(part="snippet", body=body).execute()
        return True


def endscreen_helper(video: dict[str, Any], history: list[dict[str, Any]], cfg_pl: dict) -> dict[str, Any]:
    """What to click in YouTube Studio (the API cannot do it) for this published video."""
    vid = video["video_id"]
    rel = related(history, video.get("category", ""), exclude=vid, n=3)
    return {
        "studio_url": f"https://studio.youtube.com/video/{vid}/editor",
        "suggestions": [{"title": h["title"], "id": h["video_id"], "url": watch_url(h["video_id"])} for h in rel],
        "pinned_comment": pinned_comment(rel[0] if rel else None),
        "steps": [
            "Open the Studio link, then click Editor → End screen (the video must be 25+ seconds).",
            "Add a Video element: choose 'Most recent upload' or one of the suggestions below.",
            "Add a Playlist element for this video's playlist, and a Subscribe element.",
            "Drag the elements onto the empty boxes of the end card at the end of the video.",
            "Post the pinned-comment text below as a comment, then pin it (… menu → Pin).",
        ],
    }
