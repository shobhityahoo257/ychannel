from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Config, ROOT
from .monetization import window

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
TOKEN = ROOT / "secrets" / "token.json"


def credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES) if TOKEN.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        secrets = Config.env("YOUTUBE_CLIENT_SECRETS", required=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(ROOT / secrets), SCOPES)
        creds = flow.run_local_server(port=0)   # run once on a machine with a browser, then copy token.json
    TOKEN.parent.mkdir(exist_ok=True)
    TOKEN.write_text(creds.to_json())
    return creds


def service(name: str = "youtube", version: str = "v3"):
    from googleapiclient.discovery import build
    return build(name, version, credentials=credentials(), cache_discovery=False)


def upload(video: Path, thumb: Path | None, meta: dict[str, Any], yt_cfg: dict, short: bool,
           publish_at: str | None = None) -> str:
    from googleapiclient.http import MediaFileUpload

    yt = service()
    title = meta["title"][:100]
    if short and "#Shorts" not in title and len(title) <= 92:
        title += " #Shorts"
    status: dict[str, Any] = {
        "privacyStatus": "private" if publish_at else yt_cfg["privacy"],
        "selfDeclaredMadeForKids": yt_cfg.get("made_for_kids", False),
        "containsSyntheticMedia": yt_cfg.get("contains_synthetic_media", True),
    }
    if publish_at:
        status["publishAt"] = publish_at          # RFC3339, e.g. 2026-10-03T07:30:00Z
    body = {
        "snippet": {
            "title": title, "description": meta["description"],
            "tags": (meta.get("tags", []) + yt_cfg.get("default_tags", []))[:30],
            "categoryId": yt_cfg["category_id"], "defaultLanguage": "hi", "defaultAudioLanguage": "hi",
        },
        "status": status,
    }
    req = yt.videos().insert(part="snippet,status", body=body,
                             media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True))
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    vid = resp["id"]
    if thumb and thumb.exists():
        try:
            yt.thumbnails().set(videoId=vid, media_body=MediaFileUpload(str(thumb))).execute()
        except Exception as exc:     # custom thumbnails need a verified channel
            print(f"[youtube] thumbnail not set: {exc}")
    return vid


def channel_stats() -> dict[str, Any]:
    yt = service()
    ch = yt.channels().list(part="statistics,snippet", mine=True).execute()["items"][0]
    stats = {"title": ch["snippet"]["title"], "subs": int(ch["statistics"].get("subscriberCount", 0)),
             "videos": int(ch["statistics"].get("videoCount", 0)), "watch_hours_365d": 0.0,
             "shorts_views_90d": 0}
    try:
        an = service("youtubeAnalytics", "v2")
        s, e = window(365)
        rows = an.reports().query(ids="channel==MINE", startDate=s, endDate=e,
                                  metrics="estimatedMinutesWatched").execute().get("rows") or [[0]]
        stats["watch_hours_365d"] = rows[0][0] / 60
        s, e = window(90)
        rows = an.reports().query(ids="channel==MINE", startDate=s, endDate=e, metrics="views",
                                  dimensions="creatorContentType").execute().get("rows") or []
        stats["shorts_views_90d"] = sum(r[1] for r in rows if str(r[0]).upper().startswith("SHORT"))
    except Exception as exc:
        print(f"[youtube] analytics unavailable: {exc}")
    return stats


def fetch_video_stats(ids: list[str], days: int = 365) -> dict[str, dict[str, Any]]:
    """Per-video views, retention and engagement from the YouTube Analytics API."""
    an = service("youtubeAnalytics", "v2")
    start, end = window(days)
    out: dict[str, dict[str, Any]] = {}
    for i in range(0, len(ids), 200):
        chunk = ids[i:i + 200]
        res = an.reports().query(
            ids="channel==MINE", startDate=start, endDate=end, dimensions="video",
            filters="video==" + ",".join(chunk), maxResults=200,
            metrics="views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
                    "subscribersGained,likes,comments,shares").execute()
        for r in res.get("rows") or []:
            out[r[0]] = {"views": r[1], "minutes": r[2], "avg_sec": r[3], "avg_pct": r[4],
                         "subs": r[5], "likes": r[6], "comments": r[7], "shares": r[8]}
    return out
