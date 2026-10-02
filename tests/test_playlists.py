import json
from pathlib import Path

import pytest
from conftest import SCRIPT, FakeClient

from newschannel import daily, packaging
from newschannel import playlists as P
from newschannel.models import Scene, Script, Story, Topic
from newschannel.pipeline import manual_topic, produce
from newschannel.review import Store
from newschannel.tts import MockTTS


class Exec:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


class FakeYT:
    """Just enough of googleapiclient's chained API for the playlist code."""

    def __init__(self):
        self.playlists_db = {"Existing list": "PL_EXIST"}
        self.items, self.videos_db, self.updates, self.created = [], {}, [], []

    def playlists(self):
        yt = self
        class R:
            def list(self, part, mine, maxResults, pageToken=None):
                return Exec(lambda: {"items": [{"id": i, "snippet": {"title": t}} for t, i in yt.playlists_db.items()]})
            def insert(self, part, body):
                def go():
                    pid = f"PL{len(yt.playlists_db)}"
                    yt.playlists_db[body["snippet"]["title"]] = pid
                    yt.created.append(body["snippet"]["title"])
                    return {"id": pid}
                return Exec(go)
        return R()

    def playlistItems(self):
        yt = self
        class R:
            def insert(self, part, body):
                return Exec(lambda: yt.items.append((body["snippet"]["playlistId"], body["snippet"]["resourceId"]["videoId"])))
        return R()

    def videos(self):
        yt = self
        class R:
            def list(self, part, id):
                v = yt.videos_db.get(id)
                return Exec(lambda: {"items": [{"snippet": v}] if v else []})
            def update(self, part, body):
                def go():
                    yt.videos_db[body["id"]] = body["snippet"]
                    yt.updates.append(body["id"])
                return Exec(go)
        return R()


def test_ensure_reuses_existing_creates_missing_and_caches(tmp_path):
    yt = FakeYT()
    pls = P.Playlists(yt, tmp_path / "pl.json")
    assert pls.ensure("Existing list") == "PL_EXIST"
    new = pls.ensure("Brand new")
    assert yt.created == ["Brand new"] and pls.ensure("Brand new") == new and yt.created == ["Brand new"]
    assert P.Playlists(yt, tmp_path / "pl.json").ensure("Brand new") == new          # cache survives restart
    pls.add(new, "VID1")
    assert yt.items == [(new, "VID1")]


def test_append_description_keeps_fields_and_is_idempotent(tmp_path):
    yt = FakeYT()
    yt.videos_db["V0"] = {"title": "Old", "categoryId": "25", "description": "orig", "tags": ["a"]}
    pls = P.Playlists(yt, tmp_path / "pl.json")
    assert pls.append_description("V0", "Next: new") is True
    assert yt.videos_db["V0"]["description"].endswith("Next: new") and yt.videos_db["V0"]["tags"] == ["a"]
    assert pls.append_description("V0", "Next: new") is False and yt.updates == ["V0"]
    assert pls.append_description("MISSING", "x") is False


def test_target_playlists_and_related_selection():
    cfg = {"categories": ["Elections", "Budget"], "format_playlists": {"short": "Shorts", "long": "Reports"}}
    assert P.target_playlists({"category": "Elections", "format": "short"}, cfg) == ["Elections", "Shorts"]
    assert P.target_playlists({"category": "Made up", "format": "long"}, cfg) == ["Reports"]
    hist = [{"video_id": "a", "title": "A", "category": "Elections", "published_at": "2026-01-01"},
            {"video_id": "b", "title": "B", "category": "Budget", "published_at": "2026-01-03"},
            {"video_id": "c", "title": "C", "category": "Elections", "published_at": "2026-01-02"}]
    assert [h["video_id"] for h in P.related(hist, "Elections", n=3)] == ["c", "a", "b"]   # same category first, newest first
    assert [h["video_id"] for h in P.related(hist, "Elections", exclude="c", n=1)] == ["a"]
    block = P.watch_next_block(P.related(hist, "Elections", n=1), ["https://yt/p"])
    assert "youtu.be/c" in block and "https://yt/p" in block


def test_publish_one_files_video_and_links_previous(cfg, tmp_path, monkeypatch):
    yt = FakeYT()
    yt.videos_db["PREV"] = {"title": "Earlier", "categoryId": "25", "description": "d"}
    uploaded = {}
    monkeypatch.setattr("newschannel.youtube.service", lambda *a, **k: yt)
    monkeypatch.setattr("newschannel.youtube.upload", lambda video, thumb, meta, ytcfg, short, at=None:
                        uploaded.update(meta=meta, short=short) or "NEWVID")
    store = Store(Path(cfg["youtube"]["output_dir"]))
    store.add_history("Earlier", "t", "PREV", category="चुनाव", format="short")
    d = store.run_dir("r1")
    meta = {"id": "r1", "status": "approved", "format": "short", "title": "New one", "topic": "x", "category": "चुनाव",
            "video": str(d / "v.mp4"), "thumbnail": str(d / "t.jpg"), "description": "base", "issues": [], "video_id": None}
    store.save_meta("r1", meta)
    assert daily.publish_one(cfg, store, meta) == "NEWVID"
    assert "Watch next" in uploaded["meta"]["description"] and "youtu.be/PREV" in uploaded["meta"]["description"]
    assert "playlist?list=" in uploaded["meta"]["description"]
    assert {t for t, _ in yt.items} == set(yt.playlists_db[n] for n in ("चुनाव", cfg["playlists"]["format_playlists"]["short"]))
    assert "youtu.be/NEWVID" in yt.videos_db["PREV"]["description"]            # older video now points forward
    saved = store.meta("r1")
    assert saved["status"] == "published" and saved["playlists"][0] == "चुनाव" and saved["playlist_error"] == ""
    assert store.history()[-1]["category"] == "चुनाव"


def test_playlist_failures_never_undo_the_upload(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr("newschannel.youtube.service", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no auth")))
    monkeypatch.setattr("newschannel.youtube.upload", lambda *a, **k: "OKVID")
    store = Store(Path(cfg["youtube"]["output_dir"]))
    d = store.run_dir("r2")
    meta = {"id": "r2", "status": "approved", "format": "long", "title": "T", "topic": "x", "video": str(d / "v.mp4"),
            "thumbnail": "", "description": "d", "issues": [], "video_id": None}
    store.save_meta("r2", meta)
    assert daily.publish_one(cfg, store, meta) == "OKVID" and store.meta("r2")["status"] == "published"


def test_packaging_accepts_only_allowed_category():
    topic = Topic("t", [Story("1", "संसद में बहस", "सरकार", "u", "A")])
    sc = Script("t", "d", [], [Scene("संसद में आज बहस हुई और सरकार ने बात रखी", "h")])
    ok = FakeClient({"submit_packaging": {"hooks": [], "titles": [], "thumb_texts": [], "category": "चुनाव"}})
    bad = FakeClient({"submit_packaging": {"hooks": [], "titles": [], "thumb_texts": [], "category": "बकवास"}})
    assert packaging.improve(ok, "m", sc, topic, categories=["चुनाव", "विदेश नीति"]).category == "चुनाव"
    assert packaging.improve(bad, "m", sc, topic, categories=["चुनाव"]).category == ""
    assert "चुनाव | विदेश नीति" in ok.calls[0][1]["messages"][0]["content"]


def test_long_video_ends_with_an_end_card_of_the_configured_length(cfg):
    cfg.data["endscreen"] = {"enabled": True, "seconds": 8}
    client = FakeClient({"submit_script": SCRIPT, "arrange_photos": lambda kw: {"scenes": []}})
    store = Store(Path(cfg["youtube"]["output_dir"]))
    meta = produce(cfg, manual_topic("बहस", "तथ्य"), "long", client, MockTTS(), store, preset="ultrafast", log=lambda *_: None)
    run = Path(meta["video"]).parent
    assert (run / "outro.jpg").exists() and (run / "intro.jpg").exists()
    cfg.data["endscreen"]["seconds"] = 4
    meta2 = produce(cfg, manual_topic("बहस दो", "तथ्य"), "long", client, MockTTS(), store, preset="ultrafast", log=lambda *_: None)
    assert meta["duration"] - meta2["duration"] == pytest.approx(4.0, abs=0.2)


def test_endscreen_helper_has_studio_link_suggestions_and_comment():
    hist = [{"video_id": "old", "title": "Old one", "category": "चुनाव", "published_at": "2026-01-01"},
            {"video_id": "me", "title": "Me", "category": "चुनाव", "published_at": "2026-01-02"}]
    out = P.endscreen_helper(hist[1], hist, {})
    assert out["studio_url"].endswith("/video/me/editor")
    assert out["suggestions"][0]["id"] == "old" and "youtu.be/old" in out["pinned_comment"]
    assert len(out["steps"]) == 5
