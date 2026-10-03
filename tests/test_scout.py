import io
import json
import re
from pathlib import Path

import pytest
from conftest import SCRIPT, FakeClient, make_photo

from newschannel import scout
from newschannel.library import Library
from newschannel.pipeline import manual_topic, produce
from newschannel.review import Store
from newschannel.scout import Candidate, license_ok
from newschannel.tts import MockTTS


def jpg(seed=1, w=900, h=600):
    p = Path("/tmp") / f"sc_{seed}_{w}.jpg"
    make_photo(p, w, h, seed)
    return p.read_bytes()


def cand(i, provider="wikimedia", title="Parliament House New Delhi", desc="", creator="A. Photographer", lic="CC BY 4.0", **kw):
    return Candidate(f"c{i}", provider, title, desc, creator, lic, "https://lic", f"https://page/{i}", f"https://t/{i}.jpg",
                     f"https://f/{i}.jpg", 1920, 1080, **kw)


# ------------------------------------------------------------------ licences
@pytest.mark.parametrize("lic,sa,ok", [
    ("CC0 1.0", False, True), ("Public domain", False, True), ("PD-India", False, True), ("CC BY 4.0", False, True),
    ("CC BY 2.5 in", False, True), ("Attribution", False, True), ("GODL-India", False, True),
    ("CC BY-SA 4.0", False, False), ("CC BY-SA 4.0", True, True),
    ("CC BY-NC 4.0", True, False), ("CC BY-ND 2.0", True, False), ("CC BY-NC-SA 3.0", True, False),
    ("Fair use", True, False), ("", True, False), ("All rights reserved", True, False)])
def test_license_rules(lic, sa, ok):
    assert license_ok(lic, sa)[0] is ok


# ------------------------------------------------------------------ providers (recorded response shapes)
def test_wikimedia_parser_keeps_only_free_large_photos(monkeypatch):
    def fake_get(url, params=None, headers=None, timeout=0):
        def page(title, lic, width, artist="<a>Jane Doe</a>"):
            return {"title": f"File:{title}", "imageinfo": [{
                "thumburl": f"https://upload.wikimedia.org/x/1920px-{title}", "url": f"https://upload.wikimedia.org/{title}",
                "width": width, "height": 1000, "thumbwidth": 1920, "thumbheight": 1080,
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{title}",
                "extmetadata": {"LicenseShortName": {"value": lic}, "Artist": {"value": artist},
                                "ImageDescription": {"value": "<b>Parliament House</b> in Delhi"},
                                "Categories": {"value": "Parliament of India|Buildings in Delhi"},
                                "LicenseUrl": {"value": "https://creativecommons.org/licenses/by/4.0"}}}]}
        return {"query": {"pages": {"1": page("A.jpg", "CC BY 4.0", 4000), "2": page("B.jpg", "CC BY-NC 4.0", 4000),
                                    "3": page("C.jpg", "CC0", 800), "4": page("D.jpg", "Public domain", 3000)}}}
    monkeypatch.setattr(scout, "_get", fake_get)
    got = scout.search_wikimedia("parliament", 6)
    assert [c.title for c in got] == ["A.jpg", "D.jpg"]                      # NC and too-small dropped
    a = got[0]
    assert a.creator == "Jane Doe" and a.description == "Parliament House in Delhi" and "Parliament of India" in a.categories
    assert a.thumb_url.endswith("/320px-A.jpg") and a.page_url.endswith("File:A.jpg") and a.license == "CC BY 4.0"
    assert scout.search_wikimedia("x", 6, allow_sa=True)[0].provider == "wikimedia"


def test_openverse_parser(monkeypatch):
    res = {"results": [
        {"url": "https://x/1.jpg", "thumbnail": "https://api/1/thumb", "title": "Rashtrapati Bhavan", "creator": "Sam",
         "license": "by", "license_version": "2.0", "license_url": "https://l", "foreign_landing_url": "https://flickr/1",
         "width": 3000, "height": 2000, "tags": [{"name": "delhi"}, {"name": "president"}], "source": "flickr"},
        {"url": "https://x/2.jpg", "title": "t", "license": "by-nc", "license_version": "2.0", "width": 3000},
        {"url": "https://x/3.jpg", "title": "t", "license": "cc0", "width": 500},
        {"url": "https://x/4.jpg", "title": "Open", "license": "cc0", "license_version": "1.0", "width": None, "creator": None}]}
    monkeypatch.setattr(scout, "_get", lambda *a, **k: res)
    got = scout.search_openverse("rashtrapati", 6)
    assert [c.title for c in got] == ["Rashtrapati Bhavan", "Open"]
    assert got[0].license == "CC BY 2.0" and got[0].description == "delhi president" and got[1].license == "CC0 / Public domain"
    assert got[1].creator == "Unknown"


def test_pexels_and_pixabay_need_keys_and_parse(monkeypatch):
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    monkeypatch.delenv("PIXABAY_API_KEY", raising=False)
    with pytest.raises(KeyError):
        scout.search_pexels("x")
    with pytest.raises(KeyError):
        scout.search_pixabay("x")
    monkeypatch.setenv("PEXELS_API_KEY", "k")
    monkeypatch.setenv("PIXABAY_API_KEY", "k")
    monkeypatch.setattr(scout, "_get", lambda url, *a, **k: {"photos": [
        {"id": 1, "width": 4000, "height": 3000, "url": "https://pexels/1", "photographer": "Pam", "alt": "wheat field",
         "src": {"large2x": "https://p/large.jpg", "medium": "https://p/med.jpg"}}]} if "pexels" in url else {"hits": [
        {"imageWidth": 1920, "imageHeight": 1280, "user": "Pix", "tags": "wheat, farm", "pageURL": "https://pixabay/1",
         "webformatURL": "https://w.jpg", "largeImageURL": "https://l.jpg"}]})
    px, pb = scout.search_pexels("wheat")[0], scout.search_pixabay("wheat")[0]
    assert px.generic and pb.generic and "Pexels" in scout.credit_for(px) and "Pixabay" in scout.credit_for(pb)
    wm = scout.credit_for(cand(1, creator="Jane"))
    assert wm == "Jane / Wikimedia Commons (CC BY 4.0) - https://page/1"


# ------------------------------------------------------------------ planning
def test_plan_needs_uses_ai_then_falls_back():
    client = FakeClient({"plan_photos": {"needs": [
        {"label": "Parliament House exterior", "kind": "institution", "queries": ["Parliament House New Delhi", "Sansad Bhavan"]},
        {"label": "Finance minister", "kind": "person", "named_entity": "Nirmala Sitharaman", "queries": ["Nirmala Sitharaman"]},
        {"label": "empty", "kind": "place", "queries": []}]}})
    needs = scout.plan_needs(client, "m", "story", ["hint"])
    assert [n["id"] for n in needs] == ["n1", "n2"] and needs[1]["named_entity"] == "Nirmala Sitharaman"
    assert "NEVER" not in client.calls[0][1]["system"] and "Never guess who someone is" in client.calls[0][1]["system"]
    fb = scout.plan_needs(None, "m", "Heated debate in Parliament over the budget", ["parliament hall"])
    assert fb[0]["queries"][0].startswith("Heated debate") and len(fb) == 1


# ------------------------------------------------------------------ the scout
def make_cfg(cfg, tmp_path):
    cfg.data["images"]["scout"] = {"providers": ["wikimedia", "openverse", "pexels"], "allow_cc_by_sa": False,
                                   "min_width": 1280, "per_need": 3, "recommend_from": 7}
    return cfg


def rate_answer(scores):
    def f(kw):
        ids = [re.search(r"id=(\S+)", b["text"]).group(1) for b in kw["messages"][0]["content"]
               if b["type"] == "text" and "provider=" in b["text"]]
        return {"ratings": [{"id": i, "score": scores.get(i, 5), "reason": f"fit {i}", "flags": scores.get(("flags", i), [])} for i in ids]}
    return f


def test_scout_ranks_filters_and_reports_provider_status(cfg, tmp_path):
    make_cfg(cfg, tmp_path)
    cfg.data["images"]["scout"]["per_need"] = 5
    needs = [{"id": "n1", "label": "Parliament House", "kind": "institution", "named_entity": "",
              "queries": ["parliament house"], "avoid": ""}]
    wk = [cand(1), cand(2, title="Parliament crowd"), cand(3, title="Watermarked parliament")]
    ov = [cand(4, provider="openverse", title="Parliament night"), cand(1)]           # c1 duplicates the wikimedia one
    gen = [cand(6, provider="pexels", title="Parliament stock", creator="Pam")]
    scores = {"c1": 9, "c2": 7, "c3": 9, ("flags", "c3"): ["watermark"], "c4": 8, ("flags", "c4"): ["text_overlay"], "c6": 9}
    client = FakeClient({"rate_photos": rate_answer(scores)})

    def boom(*a, **k):
        raise ConnectionError("down")
    provs = {"wikimedia": lambda q, n, sa, w: wk, "openverse": lambda q, n, sa, w: ov, "pexels": lambda q, n, sa, w: gen}
    res = scout.scout(cfg, client, "story", needs=needs, folder=tmp_path / "s", providers=provs,
                      download=lambda url: jpg(int(re.search(r"/(\d+)", url).group(1))), log=lambda *_: None)
    by = {c["id"]: c for c in res["candidates"]}
    assert "c3" not in by                                          # watermark: removed
    assert by["c4"]["score"] == 4 and not by["c4"]["recommended"]  # text overlay: capped
    assert by["c6"]["score"] == 6                                  # generic stock never beats a real photo of the subject
    assert by["c1"]["recommended"] and by["c2"]["recommended"] and by["c1"]["reason"] == "fit c1"
    assert [c["id"] for c in res["candidates"]] == ["c1", "c2", "c6", "c4"]                        # best first
    assert sum(1 for c in res["candidates"] if c["id"] == "c1") == 1                                  # de-duplicated
    assert all((tmp_path / "s" / "thumbs" / f"{c['id']}.jpg").exists() for c in res["candidates"])
    assert (tmp_path / "s" / "candidates.json").exists()
    provs["openverse"] = boom
    res2 = scout.scout(cfg, client, "story", needs=needs, folder=tmp_path / "s2", providers=provs,
                       download=lambda url: jpg(1), log=lambda *_: None)
    assert res2["providers"]["openverse"].startswith("failed") and res2["providers"]["wikimedia"].startswith("ok")


def test_pexels_without_a_key_is_reported_not_fatal(cfg, tmp_path, monkeypatch):
    make_cfg(cfg, tmp_path)
    monkeypatch.delenv("PEXELS_API_KEY", raising=False)
    needs = [{"id": "n1", "label": "x", "kind": "concept", "named_entity": "", "queries": ["x"], "avoid": ""}]
    provs = {"wikimedia": lambda q, n, sa, w: [cand(1)], "openverse": lambda q, n, sa, w: [], "pexels": scout.search_pexels}
    res = scout.scout(cfg, None, "s", needs=needs, folder=tmp_path / "s", providers=provs, download=lambda u: jpg(1), log=lambda *_: None)
    assert res["providers"]["pexels"].startswith("skipped") and res["candidates"][0]["reason"].startswith("keyword match only")


def test_a_named_person_needs_the_caption_to_name_them(cfg, tmp_path):
    make_cfg(cfg, tmp_path)
    needs = [{"id": "n1", "label": "Finance minister", "kind": "person", "named_entity": "Nirmala Sitharaman",
              "queries": ["finance minister"], "avoid": ""}]
    wk = [cand(1, title="Nirmala Sitharaman 2019.jpg"), cand(2, title="Man at a podium", desc="A politician speaking"),
          cand(3, title="Sitharaman", desc="Budget session")]
    provs = {"wikimedia": lambda q, n, sa, w: wk, "openverse": lambda q, n, sa, w: [], "pexels": lambda q, n, sa, w: []}
    res = scout.scout(cfg, FakeClient({"rate_photos": rate_answer({})}), "s", needs=needs, folder=tmp_path / "s", providers=provs,
                      download=lambda u: jpg(1), log=lambda *_: None)
    assert [c["id"] for c in res["candidates"]] == ["c1"]          # c2 does not say who it is; c3 lacks the first name
    assert scout.name_in("Photo of Nirmala Sitharaman", "Nirmala Sitharaman") and not scout.name_in("Sitharaman", "Nirmala Sitharaman")


def test_search_more_adds_new_candidates_without_duplicates(cfg, tmp_path):
    make_cfg(cfg, tmp_path)
    needs = [{"id": "n1", "label": "Parliament", "kind": "institution", "named_entity": "", "queries": ["parliament"], "avoid": ""}]
    provs = {"wikimedia": lambda q, n, sa, w: [cand(1)] if q == "parliament" else [cand(1), cand(7, title="Lok Sabha hall")],
             "openverse": lambda q, n, sa, w: [], "pexels": lambda q, n, sa, w: []}
    res = scout.scout(cfg, None, "s", needs=needs, folder=tmp_path / "s", providers=provs, download=lambda u: jpg(1), log=lambda *_: None)
    assert len(res["candidates"]) == 1
    again = scout.search_more(cfg, None, tmp_path / "s", "lok sabha", "n1", providers=provs, download=lambda u: jpg(2), log=lambda *_: None)
    assert [c["id"] for c in again["candidates"]] == ["c1", "c7"] and again["candidates"][1]["need"] == "n1"
    custom = scout.search_more(cfg, None, tmp_path / "s", "brand new topic", None, providers=provs, download=lambda u: jpg(3), log=lambda *_: None)
    assert custom["needs"][-1]["label"] == "brand new topic"


def test_approve_imports_into_library_with_credit_and_license(cfg, tmp_path):
    make_cfg(cfg, tmp_path)
    needs = [{"id": "n1", "label": "Parliament House", "kind": "institution", "named_entity": "", "queries": ["p"], "avoid": ""}]
    provs = {"wikimedia": lambda q, n, sa, w: [cand(1, desc="Seat of the Indian parliament"), cand(2, title="Other")],
             "openverse": lambda q, n, sa, w: [], "pexels": lambda q, n, sa, w: []}
    res = scout.scout(cfg, None, "s", needs=needs, folder=tmp_path / "s", providers=provs, download=lambda u: jpg(1), log=lambda *_: None)
    lib = Library(tmp_path / "lib")
    bigs = {"https://f/1.jpg": jpg(11, 1600, 900), "https://f/2.jpg": b"not an image"}

    def dl(url):
        return bigs[url]
    entries = scout.approve(tmp_path / "s", ["c1", "c2", "nope"], lib, 700, dl, log=lambda *_: None)
    assert len(entries) == 1                                           # junk and unknown ids are skipped
    e = lib.get(entries[0].id)
    assert e.source == "stock" and e.license == "CC BY 4.0" and e.page_url == "https://page/1"
    assert e.credit == "A. Photographer / Wikimedia Commons (CC BY 4.0) - https://page/1"
    assert "Parliament House New Delhi" in e.caption and "parliament" in e.tags


# ------------------------------------------------------------------ in the video pipeline
def run(cfg, client, **kw):
    store = Store(Path(cfg["youtube"]["output_dir"]))
    return produce(cfg, manual_topic("संसद में बहस", "संसद में आज बहस हुई।"), "short", client, MockTTS(), store,
                   preset="ultrafast", log=lambda *_: None, **kw)


def arrange(kw):
    ids = [ln.split("asset_id=")[1].split()[0] for b in kw["messages"][0]["content"] if b["type"] == "text"
           for ln in b["text"].split("\n") if ln.startswith("asset_id=")]
    return {"scenes": [{"scene": i, "photos": [{"asset_id": ids[i % len(ids)]}]} for i in range(3)]}


def clean_inbox(cfg):
    for f in Path(cfg["images"]["inbox_dir"]).glob("*"):
        f.unlink()


def test_web_app_style_runs_never_fetch_photos_on_their_own(cfg, monkeypatch):
    clean_inbox(cfg)
    cfg.data["images"]["stock_mode"] = "auto_strict"
    monkeypatch.setattr(scout, "scout", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not search online")))
    meta = run(cfg, FakeClient({"submit_script": SCRIPT, "arrange_photos": arrange}), approved_only=True)
    assert Path(meta["video"]).exists()                                # falls back to headline cards, never to unchecked photos


def test_unattended_runs_take_only_clear_fits(cfg, monkeypatch):
    clean_inbox(cfg)
    cfg.data["images"]["stock_mode"] = "auto_strict"
    cfg.data["images"]["stock_min_score"] = 8
    cfg.data["images"]["scout"] = {"providers": ["wikimedia"], "per_need": 4, "recommend_from": 7, "min_width": 1280}
    good, meh = cand(1, title="Parliament House New Delhi"), cand(2, title="Some crowd")
    monkeypatch.setitem(scout.PROVIDERS, "wikimedia", lambda q, n, sa, w: [good, meh])
    monkeypatch.setattr(scout, "fetch_bytes", lambda url: jpg(int(re.search(r"/(\d+)", url).group(1)), 1600, 900))
    client = FakeClient({"submit_script": SCRIPT, "arrange_photos": arrange,
                         "plan_photos": {"needs": [{"label": "Parliament", "kind": "institution", "queries": ["parliament"]}]},
                         "rate_photos": rate_answer({"c1": 9, "c2": 6})})
    meta = run(cfg, client)
    lib = Library(Path(cfg["images"]["library_dir"]))
    stock = [e for e in lib.all() if e.source == "stock"]
    assert len(stock) == 1 and stock[0].license == "CC BY 4.0"       # the 6/10 photo was NOT used
    assert "Wikimedia Commons (CC BY 4.0)" in meta["description"]    # credit is published with the video


def test_generic_stock_adds_a_representative_image_note(cfg):
    clean_inbox(cfg)
    lib = Library(Path(cfg["images"]["library_dir"]))
    pic = Path(cfg["images"]["library_dir"]).parent / "p.jpg"
    make_photo(pic, 1600, 900, 5)
    e, _ = lib.add_file(pic, caption="wheat field", credit="Photo by Pam on Pexels (Pexels License)", source="stock")
    cfg.data["content"]["language"] = "hinglish"
    meta = run(cfg, FakeClient({"submit_script": SCRIPT, "arrange_photos": arrange}), library_ids=[e.id], approved_only=True)
    assert "Photo by Pam on Pexels" in meta["description"] and "generic representative images" in meta["description"]
