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
    data["content"]["language"] = "hindi"
    data["images"].update(inbox_dir=str(tmp_path / "inbox"), stock_mode="off",
                          library_dir=str(tmp_path / "library"))
    data["youtube"]["output_dir"] = str(tmp_path / "out")
    data["audio"]["music_dir"] = str(tmp_path / "nomusic")
    data["formats"]["short"].update(width=360, height=640, fps=12, target_seconds=12, max_scenes=5)
    data["formats"]["analysis"].update(width=640, height=360, fps=12, max_scenes=40)
    data["endscreen"] = {"enabled": True, "seconds": 4}
    data["analysis"]["min_minutes"] = 0.2
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


def test_endscreen_helper_endpoints(client):
    c, tmp = client
    store = webui.Store(tmp / "out")
    store.add_history("Earlier", "t", "OLD1", category="चुनाव", format="long")
    store.add_history("Latest", "t", "NEW1", category="चुनाव", format="long")
    d = store.run_dir("rid")
    store.save_meta("rid", {"id": "rid", "status": "published", "format": "long", "title": "Latest", "topic": "t",
                            "video_id": "NEW1", "video": "", "thumbnail": "", "issues": [], "description": ""})
    r = c.get("/api/videos/rid/endscreen").get_json()
    assert r["studio_url"].endswith("/video/NEW1/editor") and r["suggestions"][0]["id"] == "OLD1" and r["long"] and not r["done"]
    assert c.post("/api/videos/rid/endscreen/done", json={"done": True}).status_code == 200
    assert c.get("/api/videos/rid/endscreen").get_json()["done"] is True
    store.save_meta("pending1", {"id": "pending1", "status": "pending", "format": "short", "title": "x", "topic": "t",
                                 "video_id": None, "video": "", "thumbnail": "", "issues": [], "description": ""}) if store.run_dir("pending1") else None
    assert c.get("/api/videos/pending1/endscreen").status_code == 400


def test_deep_analysis_flow_through_the_api(client, monkeypatch):
    import test_analysis as TA
    from newschannel.ledger import Ledger
    c, tmp = client
    fc = TA.client_for_research("critical", 0.2)
    monkeypatch.setattr(webui, "make_client", lambda cfg, required=True: fc)
    monkeypatch.setattr("newschannel.research.fetch_article", TA.fake_fetch)
    # input validation
    assert c.post("/api/analysis/research", json={"headline": " "}).status_code == 400
    assert c.post("/api/analysis/research", json={"headline": "x"}).status_code == 400          # no sources at all
    assert c.post("/api/analysis/create", data={"research_id": "nope"}, content_type="multipart/form-data").status_code == 400
    r = c.post("/api/analysis/research", json={"headline": "MSP hike", "urls": "\n".join(TA.TEXTS)})
    j = wait(c, r.get_json()["job"])
    assert j["status"] == "done", j["error"]
    res = j["result"]
    assert res["enough"] and res["recommend"]["stance"] == "critical" and res["ledger"]["claims"]
    again = c.get(f"/api/analysis/research/{res['research_id']}").get_json()
    assert again["usable"] == res["usable"] and again["recommend"]["minutes"] == 1 or again["usable"] == res["usable"]
    led = Ledger.load(tmp / "out" / "_research" / res["research_id"] / "ledger.json")
    scenes = TA.good_scenes(led)
    fc.answers["submit_analysis"] = {"title": "MSP hike: kya sach hai?", "description": "d", "tags": ["msp"], "scenes": scenes}
    fc.answers["arrange_photos"] = lambda kw: {"scenes": []}
    base = {"research_id": res["research_id"], "minutes": str(TA.minutes_for(scenes)), "language": "hinglish", "music": "calm"}
    assert c.post("/api/analysis/create", data={**base, "stance": "angry"}, content_type="multipart/form-data").status_code == 400
    assert c.post("/api/analysis/create", data={**base, "minutes": "lots"}, content_type="multipart/form-data").status_code == 400
    j2 = wait(c, c.post("/api/analysis/create", data={**base, "stance": "auto"}, content_type="multipart/form-data").get_json()["job"])
    assert j2["status"] == "done", j2["error"]
    v = c.get("/api/videos").get_json()["videos"][0]
    assert v["analysis"]["stance"] == "critical" and v["language"] == "hinglish" and v["format"] == "analysis"
    assert not [i for i in v["issues"] if i["level"] == "block"]
    led_json = c.get(f"/api/videos/{v['id']}/ledger").get_json()
    assert led_json["analysis"]["counts"]["confirmed"] >= 2 and led_json["ledger"]["claims"]
    assert c.get("/api/videos/20200101-nothing-short/ledger").status_code == 404


def test_photo_finder_shows_candidates_then_uses_only_approved_ones(client, monkeypatch):
    import re
    import test_scout as TS
    from newschannel import scout as scout_mod
    c, tmp = client
    monkeypatch.setitem(scout_mod.PROVIDERS, "wikimedia", lambda q, n, sa, w: [
        TS.cand(1, title="Parliament House New Delhi"), TS.cand(2, title="Parliament night view")]
        if "lok" not in q else [TS.cand(1), TS.cand(9, title="Lok Sabha hall")])
    monkeypatch.setitem(scout_mod.PROVIDERS, "openverse", lambda q, n, sa, w: [])
    monkeypatch.setitem(scout_mod.PROVIDERS, "pexels", lambda q, n, sa, w: [])
    monkeypatch.setitem(scout_mod.PROVIDERS, "pixabay", lambda q, n, sa, w: [])
    monkeypatch.setattr(scout_mod, "fetch_bytes", lambda url: TS.jpg(int(re.search(r"/(\d+)", url).group(1)), 1600, 900))
    fc = FakeClient({"submit_script": SCRIPT, "arrange_photos": lambda kw: {"scenes": []},
                     "plan_photos": {"needs": [{"label": "Parliament House", "kind": "institution", "queries": ["parliament house"]}]},
                     "rate_photos": TS.rate_answer({"c1": 9, "c2": 5, "c9": 8})})
    monkeypatch.setattr(webui, "make_client", lambda cfg, required=True: fc)
    assert c.post("/api/scout", json={}).status_code == 400
    r = c.post("/api/scout", json={"headline": "Budget debate in Parliament", "text": "The debate was heated."}).get_json()
    j = wait(c, r["job"])
    assert j["status"] == "done", j["error"]
    res = j["result"]
    assert res["sid"] == r["sid"] and [x["id"] for x in res["candidates"]] == ["c1", "c2"]
    assert res["candidates"][0]["recommended"] and not res["candidates"][1]["recommended"]
    assert c.get(f"/api/scout/{r['sid']}/thumb/c1.jpg").status_code == 200
    assert c.get(f"/api/scout/{r['sid']}/thumb/..%2Fcandidates.json").status_code == 404
    assert c.get("/api/scout/nope/thumb/c1.jpg").status_code == 404
    more = c.post(f"/api/scout/{r['sid']}/search", json={"query": "lok sabha", "need_id": "n1"}).get_json()
    assert [x["id"] for x in more["candidates"]] == ["c1", "c2", "c9"]
    assert c.post(f"/api/scout/{r['sid']}/search", json={"query": " "}).status_code == 400
    assert c.post(f"/api/scout/{r['sid']}/approve", json={"ids": []}).status_code == 400
    ok = c.post(f"/api/scout/{r['sid']}/approve", json={"ids": ["c1", "c9"]}).get_json()
    assert len(ok["photos"]) == 2 and ok["photos"][0]["license"] == "CC BY 4.0" and "Wikimedia Commons" in ok["photos"][0]["credit"]
    assert len(c.get("/api/library?source=stock").get_json()["photos"]) == 2           # approved photos are in the library
    # the video uses exactly the approved photos and never goes online by itself
    monkeypatch.setattr(scout_mod, "scout", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no automatic search")))
    data = {"headline": "बहस", "text": "तथ्य", "format": "short", "library_ids": json.dumps([p["id"] for p in ok["photos"]])}
    j2 = wait(c, c.post("/api/create", data=data, content_type="multipart/form-data").get_json()["job"])
    assert j2["status"] == "done", j2["error"]
    desc = c.get("/api/videos").get_json()["videos"][0]["description"]
    assert "A. Photographer / Wikimedia Commons (CC BY 4.0)" in desc


def test_page_photos_endpoint_enforces_rights(client, monkeypatch):
    import re
    import test_pagephotos as TP
    import test_scout as TS
    from newschannel import pagephotos, scout as scout_mod
    c, tmp = client
    for name in ("wikimedia", "openverse", "pexels", "pixabay"):
        monkeypatch.setitem(scout_mod.PROVIDERS, name, lambda q, n, sa, w: [])
    monkeypatch.setattr(scout_mod, "fetch_bytes", lambda url: TS.jpg(abs(hash(url)) % 90 + 2, 1600, 900))
    fc = FakeClient({"plan_photos": {"needs": [{"label": "MSP hike", "kind": "event", "queries": ["msp"]}]}, "rate_photos": TS.rate_answer({})})
    monkeypatch.setattr(webui, "make_client", lambda cfg, required=True: fc)
    sid = c.post("/api/scout", json={"headline": "MSP hike"}).get_json()
    wait(c, sid["job"])
    sid = sid["sid"]
    assert c.post(f"/api/scout/{sid}/page", json={"url": "http://127.0.0.1:8000/secret"}).status_code == 400    # never fetches local addresses
    assert c.post(f"/api/scout/{sid}/page", json={"url": "not a url"}).status_code == 400
    monkeypatch.setattr(pagephotos, "public_url", lambda u, *a: True)
    monkeypatch.setattr(pagephotos, "fetch_html", lambda u: TP.ARTICLE)
    res = c.post(f"/api/scout/{sid}/page", json={"url": "https://www.thedaily.example/news/1"})
    assert res.status_code == 200
    cands = res.get_json()["candidates"]
    unknown = [x["id"] for x in cands if x["rights"] == "unknown"]
    licensed = [x["id"] for x in cands if x["rights"] == "licensed"]
    assert unknown and licensed
    refused = c.post(f"/api/scout/{sid}/approve", json={"ids": unknown, "confirm_official": True}).get_json()
    assert refused["photos"] == [] and len(refused["skipped"]) == len(unknown)                                  # server refuses, even when told to import
    ok = c.post(f"/api/scout/{sid}/approve", json={"ids": licensed}).get_json()
    assert len(ok["photos"]) == len(licensed) and ok["photos"][0]["license"] == "CC BY 4.0"
    assert "Priya Rao" in ok["photos"][0]["credit"]
