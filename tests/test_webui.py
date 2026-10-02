import io
import json
import subprocess
import time
from pathlib import Path

import pytest
import yaml
from conftest import SCRIPT, FakeClient, make_photo

from newschannel import webui
from newschannel.config import ROOT
from newschannel.tts import MockTTS


@pytest.fixture
def client(tmp_path, monkeypatch):
    data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    data["tts"]["provider"] = "mock"
    data["images"].update(inbox_dir=str(tmp_path / "inbox"), use_stock_fallback=False,
                          library_dir=str(tmp_path / "library"))
    data["youtube"]["output_dir"] = str(tmp_path / "out")
    data["audio"]["music_dir"] = str(tmp_path / "nomusic")
    data["formats"]["short"].update(width=360, height=640, fps=12, target_seconds=12, max_scenes=5)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    (tmp_path / "assets").symlink_to(ROOT / "assets")

    def arrange(kw):
        ids = [ln.split("asset_id=")[1].split()[0] for b in kw["messages"][0]["content"] if b["type"] == "text"
               for ln in b["text"].split("\n") if ln.startswith("asset_id=")]
        return {"scenes": [{"scene": i, "photos": [{"asset_id": ids[i % len(ids)]}]} for i in range(3)]}

    monkeypatch.setattr(webui, "make_client", lambda cfg, required=True: FakeClient(
        {"submit_script": SCRIPT, "arrange_photos": arrange}))
    monkeypatch.setattr(webui, "make_tts", lambda cfg: MockTTS())
    app = webui.create_app(str(tmp_path / "config.yaml"), tmp_path / ".env")
    app.testing = True
    return app.test_client(), tmp_path


def jpeg(seed=1):
    p = Path("/tmp") / f"ui_{seed}.jpg"
    make_photo(p, 1200, 800, seed)
    return io.BytesIO(p.read_bytes())


def wait(c, job, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = c.get(f"/api/jobs/{job}").get_json()
        if j["status"] in ("done", "error"):
            return j
        time.sleep(0.5)
    raise AssertionError("job timed out")


def test_page_and_status(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200 and "Create video" in r.get_data(as_text=True)
    s = c.get("/api/status").get_json()
    assert {x["name"] for x in s["checks"]} >= {"ffmpeg"} and "OPENAI_API_KEY" in s["keys"]


def test_other_hosts_and_origins_are_refused(client):
    c, _ = client
    assert c.get("/api/status", headers={"Host": "evil.example.com"}).status_code == 403
    assert c.post("/api/keys", json={}, headers={"Origin": "https://evil.example.com"}).status_code == 403


def test_keys_saved_to_env_never_returned(client, monkeypatch):
    c, tmp = client
    (tmp / ".env").write_text("# my keys\nOPENAI_API_KEY=old\nPEXELS_API_KEY=keepme\n", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = c.post("/api/keys", json={"OPENAI_API_KEY": " sk-new ", "OPENAI_VOICE": "", "EVIL": "x"})
    assert r.get_json()["saved"] == ["OPENAI_API_KEY"]
    env = (tmp / ".env").read_text()
    assert "OPENAI_API_KEY=sk-new" in env and "PEXELS_API_KEY=keepme" in env and "# my keys" in env and "EVIL" not in env
    assert "sk-new" not in json.dumps(c.get("/api/status").get_json())
    assert c.get("/api/status").get_json()["keys"]["OPENAI_API_KEY"] is True


def test_create_video_with_uploaded_photos_and_review(client):
    c, tmp = client
    data = {"headline": "संसद में बहस", "text": "संसद में आज बहस हुई।", "format": "short",
            "images": [(jpeg(1), "a.jpg"), (jpeg(2), "b.png"), (io.BytesIO(b"x"), "evil.exe")]}
    r = c.post("/api/create", data=data, content_type="multipart/form-data")
    j = wait(c, r.get_json()["job"])
    assert j["status"] == "done", j["error"]
    vid = c.get("/api/videos").get_json()["videos"][0]
    assert vid["status"] == "pending" and vid["title"] == SCRIPT["title"]
    assert c.get(vid["video_url"]).status_code == 200
    assert c.get(vid["thumb_url"]).status_code == 200
    assert c.post(f"/api/videos/{vid['id']}/status", json={"action": "approve"}).status_code == 200
    assert c.get("/api/videos").get_json()["videos"][0]["status"] == "approved"
    # publishing without YouTube set up gives a friendly message, not a crash
    p = c.post(f"/api/videos/{vid['id']}/publish", json={})
    assert p.status_code == 400 and "YouTube" in p.get_json()["error"]


def test_files_route_is_restricted(client):
    c, _ = client
    assert c.get("/files/..%2F..%2Fetc/passwd").status_code == 404
    assert c.get("/files/x/meta.json").status_code == 404


def test_missing_headline_rejected(client):
    c, _ = client
    r = c.post("/api/create", data={"format": "short", "headline": " "}, content_type="multipart/form-data")
    assert r.status_code == 400


def test_clip_without_credit_blocks_approval_until_forced(client, tmp_path, monkeypatch):
    c, _ = client
    clip = tmp_path / "s.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=5",
                    "-f", "lavfi", "-i", "sine=duration=5", "-shortest", "-pix_fmt", "yuv420p", str(clip)], check=True)
    sc = json.loads(json.dumps(SCRIPT))
    sc["scenes"].insert(2, {"narration": "", "headline": "भाषण", "kind": "clip", "clip_id": 0})
    monkeypatch.setattr(webui, "make_client", lambda cfg, required=True: FakeClient(
        {"submit_script": sc, "arrange_photos": lambda kw: {"scenes": []}}))
    data = {"headline": "बहस", "text": "तथ्य", "format": "short", "images": [(jpeg(3), "a.jpg")],
            "clips": [(io.BytesIO(clip.read_bytes()), "s.mp4")], "clip_meta": json.dumps([{"credit": "", "note": "n"}])}
    j = wait(c, c.post("/api/create", data=data, content_type="multipart/form-data").get_json()["job"])
    assert j["status"] == "done", j["error"]
    rid = j["result"]["id"]
    r = c.post(f"/api/videos/{rid}/status", json={"action": "approve"})
    assert r.status_code == 409 and "no source credit" in r.get_json()["issues"][0]["msg"]
    assert c.post(f"/api/videos/{rid}/status", json={"action": "approve", "force": True}).status_code == 200


def test_choose_title_and_thumbnail_variant(client, monkeypatch):
    c, tmp = client
    sc = json.loads(json.dumps(SCRIPT))
    monkeypatch.setattr(webui, "make_client", lambda cfg, required=True: FakeClient({
        "submit_script": sc, "arrange_photos": lambda kw: {"scenes": []},
        "submit_packaging": {"hooks": [], "titles": [{"text": "विकल्प एक शीर्षक", "score": 8}, {"text": "विकल्प दो शीर्षक", "score": 7}],
                             "thumb_texts": ["एक", "दो", "तीन"]}}))
    data = {"headline": "बहस", "text": "तथ्य", "format": "short", "images": [(jpeg(1), "a.jpg"), (jpeg(2), "b.jpg")]}
    j = wait(c, c.post("/api/create", data=data, content_type="multipart/form-data").get_json()["job"])
    assert j["status"] == "done", j["error"]
    v = c.get("/api/videos").get_json()["videos"][0]
    assert v["title"] == "विकल्प एक शीर्षक" and len(v["thumb_variants"]) == 3
    assert c.get(v["thumb_variants"][2]["url"]).status_code == 200
    r = c.post(f"/api/videos/{v['id']}/package", json={"title": "विकल्प दो शीर्षक", "thumbnail": 2})
    assert r.status_code == 200
    v2 = c.get("/api/videos").get_json()["videos"][0]
    assert v2["title"] == "विकल्प दो शीर्षक" and v2["thumb_choice"] == 2
    assert c.post(f"/api/videos/{v['id']}/package", json={"title": "x" * 101}).status_code == 400
    assert c.post(f"/api/videos/{v['id']}/package", json={"thumbnail": 9}).status_code == 400


def test_insights_endpoint_and_sync_requires_youtube(client):
    c, _ = client
    assert c.get("/api/insights").get_json()["ready"] is False
    r = c.post("/api/insights/sync")
    assert r.status_code == 400 and "YouTube" in r.get_json()["error"]


def test_breaking_watcher_endpoints(client, monkeypatch):
    c, _ = client
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    s = c.get("/api/breaking").get_json()
    assert s["running"] is False and s["settings"]["min_sources"] == 3
    assert c.post("/api/breaking", json={"action": "start"}).status_code == 400      # needs a key first
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    monkeypatch.setattr("newschannel.breaking.BreakingWatcher.run_once", lambda self: [])
    assert c.post("/api/breaking", json={"action": "start"}).status_code == 200
    import time
    time.sleep(0.5)
    assert c.get("/api/breaking").get_json()["running"] is True
    assert c.post("/api/breaking", json={"action": "stop"}).status_code == 200
    assert c.post("/api/breaking", json={"action": "bogus"}).status_code == 400


def test_library_api_upload_search_edit_delete_and_reuse(client):
    c, tmp = client
    r = c.post("/api/library", data={"files": [(jpeg(11), "a.jpg"), (jpeg(12), "b.jpg"), (io.BytesIO(b"x"), "bad.exe")],
                                     "caption": "", "credit": "PIB"}, content_type="multipart/form-data")
    assert r.get_json() == {"added": 2, "duplicates": 0}
    again = c.post("/api/library", data={"files": [(jpeg(11), "a_copy.jpg")]}, content_type="multipart/form-data").get_json()
    assert again == {"added": 0, "duplicates": 1}
    photos = c.get("/api/library").get_json()
    assert photos["total"] == 2 and photos["untagged"] == 2
    pid = photos["photos"][0]["id"]
    assert c.get(photos["photos"][0]["thumb"]).status_code == 200 and c.get(photos["photos"][0]["full"]).status_code == 200
    e = c.post(f"/api/library/{pid}", json={"caption": "Parliament at night", "tags": "building, night"}).get_json()
    assert e["tags"] == ["building", "night"] and e["credit"] == "PIB"
    assert [p["id"] for p in c.get("/api/library?q=parliament").get_json()["photos"]] == [pid]
    # a video can use a library photo by id
    data = {"headline": "बहस", "text": "तथ्य", "format": "short", "library_ids": json.dumps([pid])}
    j = wait(c, c.post("/api/create", data=data, content_type="multipart/form-data").get_json()["job"])
    assert j["status"] == "done", j["error"]
    assert c.get("/api/videos").get_json()["videos"][0]["description"].count("PIB") >= 1
    assert c.get("/library/thumbs/../../etc.jpg").status_code == 404
    assert c.delete(f"/api/library/{pid}").status_code == 200 and c.delete(f"/api/library/{pid}").status_code == 404
