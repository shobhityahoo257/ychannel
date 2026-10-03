import json
import re
from pathlib import Path

import pytest
from conftest import FakeClient, make_photo

from newschannel import cards, pagephotos as PP, scout as S
from newschannel.library import Library


def jpg(seed=1, w=1600, h=900):
    p = Path("/tmp") / f"pp_{seed}_{w}.jpg"
    make_photo(p, w, h, seed)
    return p.read_bytes()


ARTICLE = """<html><head><title>Cabinet clears MSP hike | The Daily</title>
<meta property="og:title" content="Cabinet clears MSP hike"><meta property="og:image" content="/img/lead-photo.jpg">
<meta name="author" content="Staff Reporter">
<script type="application/ld+json">{"@type":"NewsArticle","image":[{"@type":"ImageObject","url":"https://cdn.example.com/cc.jpg",
 "license":"https://creativecommons.org/licenses/by/4.0/","creditText":"Priya Rao"}]}</script></head><body>
<img src="/static/site-logo.png" width="900" height="300"><img src="/img/tiny.jpg" width="100" height="80">
<figure><img src="/img/farmers.jpg" srcset="/img/f-480.jpg 480w, /img/f-1600.jpg 1600w" alt="Farmers at a mandi"><figcaption>Farmers wait at a mandi in Punjab.</figcaption></figure>
<img data-src="/img/lazy.webp" alt="Lazy loaded photo"><img src="data:image/gif;base64,AAAA" alt="pixel">
<img src="https://cdn.example.com/cc.jpg" alt="Licensed one"></body></html>"""


def by_url(cands):
    return {c.full_url.rsplit("/", 1)[-1]: c for c in cands}


def test_license_urls_and_address_safety():
    assert PP.license_from_url("https://creativecommons.org/licenses/by/4.0/") == "CC BY 4.0"
    assert PP.license_from_url("https://creativecommons.org/licenses/by-nc-nd/3.0/") == "CC BY-NC-ND 3.0"
    assert PP.license_from_url("http://creativecommons.org/publicdomain/zero/1.0/") == "CC0 / Public domain"
    assert PP.license_from_url("https://example.com/terms") == ""
    pub = lambda h, p: [(0, 0, 0, "", ("93.184.216.34", 0))]   # noqa: E731
    assert PP.public_url("https://example.com/a", pub)
    for bad in ("http://127.0.0.1/x", "http://localhost/x", "http://10.1.2.3/x", "http://192.168.0.5/", "ftp://example.com/x", "file:///etc/passwd", "http://169.254.169.254/"):
        assert not PP.public_url(bad), bad
    assert not PP.public_url("https://example.com", lambda h, p: [(0, 0, 0, "", ("10.0.0.7", 0))])   # DNS pointing inside the network


def test_rights_are_labelled_per_photo():
    got = by_url(PP.find_photos(ARTICLE, "https://www.thedaily.example/news/1"))
    assert "site-logo.png" not in got and "tiny.jpg" not in got                 # logos / tiny images skipped
    assert not any(k.startswith("data:") for k in got)
    assert got["cc.jpg"].rights == "licensed" and got["cc.jpg"].license == "CC BY 4.0" and got["cc.jpg"].creator == "Priya Rao"
    assert got["lead-photo.jpg"].rights == "unknown" and "Ask for permission" in got["lead-photo.jpg"].note
    assert got["f-1600.jpg"].rights == "unknown" and got["f-1600.jpg"].description == "Farmers wait at a mandi in Punjab."   # biggest srcset entry + caption
    assert "lazy.webp" in got and got["lead-photo.jpg"].full_url == "https://www.thedaily.example/img/lead-photo.jpg"
    assert not any(c.recommended for c in got.values())


def test_official_sites_and_blocked_licences():
    gov = by_url(PP.find_photos(ARTICLE.replace("https://cdn.example.com/cc.jpg", "https://cdn.example.com/x.jpg"), "https://pib.gov.in/PressRelease?id=1"))
    assert gov["lead-photo.jpg"].rights == "official" and "Copyright Policy" in gov["lead-photo.jpg"].note
    assert gov["lead-photo.jpg"].creator == "Staff Reporter"
    nc = '<html><head><link rel="license" href="https://creativecommons.org/licenses/by-nc/4.0/"></head><body><img src="/p.jpg" width="1200"></body></html>'
    c = PP.find_photos(nc, "https://blog.example.org/post")[0]
    assert c.rights == "blocked" and "does not allow" in c.note and c.license == "CC BY-NC 4.0"
    sa = nc.replace("by-nc", "by-sa")
    assert PP.find_photos(sa, "https://blog.example.org/post")[0].rights == "blocked"
    assert PP.find_photos(sa, "https://blog.example.org/post", allow_sa=True)[0].rights == "licensed"
    ok = '<html><head><link rel="license" href="https://creativecommons.org/publicdomain/zero/1.0/"></head><body><img src="/p.jpg" width="1200"></body></html>'
    assert PP.find_photos(ok, "https://blog.example.org/post")[0].rights == "licensed"


def make_session(cfg, tmp_path):
    cfg.data["images"]["scout"] = {"providers": ["wikimedia"], "per_need": 3, "recommend_from": 7, "min_width": 1280}
    needs = [{"id": "n1", "label": "MSP hike cabinet", "kind": "event", "named_entity": "", "queries": ["msp"], "avoid": ""}]
    return S.scout(cfg, None, "s", needs=needs, folder=tmp_path / "s", providers={"wikimedia": lambda q, n, sa, w: []},
                   download=lambda u: jpg(1), log=lambda *_: None)


def test_add_page_rates_labels_and_never_recommends_unlicensed(cfg, tmp_path):
    make_session(cfg, tmp_path)
    scores = {}
    client = FakeClient({"rate_photos": lambda kw: {"ratings": [
        {"id": re.search(r"id=(\S+)", b["text"]).group(1), "score": 9, "reason": "good"} for b in kw["messages"][0]["content"]
        if b["type"] == "text" and "provider=" in b["text"]]}})
    sess = PP.add_page(cfg, client, tmp_path / "s", "https://www.thedaily.example/news/1", "n1", html_fetch=lambda u: ARTICLE,
                       download=lambda u: jpg(abs(hash(u)) % 90 + 2), resolve=lambda h, p: [(0, 0, 0, "", ("93.184.216.34", 0))],
                       log=lambda *_: None)
    page = [c for c in sess["candidates"] if c["provider"] == "page"]
    assert page and all(c["need"] == "n1" for c in page)
    assert {c["rights"] for c in page} == {"licensed", "unknown"}
    assert all(c["score"] == 9 for c in page)
    rec = [c for c in page if c["recommended"]]
    assert rec and all(c["rights"] == "licensed" for c in rec)                  # a 9/10 photo with unknown rights is still not pre-selected
    again = PP.add_page(cfg, client, tmp_path / "s", "https://www.thedaily.example/news/1", "n1", html_fetch=lambda u: ARTICLE,
                        download=lambda u: jpg(3), resolve=lambda h, p: [(0, 0, 0, "", ("93.184.216.34", 0))], log=lambda *_: None)
    assert len(again["candidates"]) == len(sess["candidates"])                 # nothing is added twice
    with pytest.raises(ValueError, match="public web address"):
        PP.add_page(cfg, client, tmp_path / "s", "http://127.0.0.1:8000/x", html_fetch=lambda u: ARTICLE)
    with pytest.raises(ValueError, match="No usable photos"):
        PP.add_page(cfg, client, tmp_path / "s", "https://x.example.com/", html_fetch=lambda u: "<html></html>",
                    resolve=lambda h, p: [(0, 0, 0, "", ("93.184.216.34", 0))])
    new = PP.add_page(cfg, None, tmp_path / "s", "https://news.example.org/a", None, html_fetch=lambda u: ARTICLE,
                      download=lambda u: jpg(4), resolve=lambda h, p: [(0, 0, 0, "", ("93.184.216.34", 0))], log=lambda *_: None)
    assert new["needs"][-1]["label"].startswith("Photos from")


def test_unlicensed_photos_cannot_be_imported_even_if_the_screen_asks(cfg, tmp_path):
    make_session(cfg, tmp_path)
    sess = PP.add_page(cfg, None, tmp_path / "s", "https://www.thedaily.example/news/1", "n1", html_fetch=lambda u: ARTICLE,
                       download=lambda u: jpg(abs(hash(u)) % 90 + 2), resolve=lambda h, p: [(0, 0, 0, "", ("93.184.216.34", 0))],
                       log=lambda *_: None)
    unknown = [c["id"] for c in sess["candidates"] if c["rights"] == "unknown"]
    licensed = [c["id"] for c in sess["candidates"] if c["rights"] == "licensed"]
    lib = Library(tmp_path / "lib")
    skipped: list = []
    got = S.approve(tmp_path / "s", unknown + licensed, lib, 700, lambda u: jpg(7), lambda *_: None, True, skipped)
    assert len(got) == len(licensed) and {x["id"] for x in skipped} == set(unknown)
    e = lib.get(got[0].id)
    assert e.credit == "Priya Rao (CC BY 4.0) - https://www.thedaily.example/news/1" and e.license == "CC BY 4.0"
    # official-site photos need the user's explicit confirmation
    gov = PP.add_page(cfg, None, tmp_path / "s", "https://pib.gov.in/PressRelease?id=9", "n1",
                      html_fetch=lambda u: "<html><head><meta property='og:image' content='/lead.jpg'></head></html>",
                      download=lambda u: jpg(8), resolve=lambda h, p: [(0, 0, 0, "", ("93.184.216.34", 0))], log=lambda *_: None)
    off = [c["id"] for c in gov["candidates"] if c["rights"] == "official"]
    assert off
    sk2: list = []
    assert S.approve(tmp_path / "s", off, lib, 700, lambda u: jpg(9), lambda *_: None, False, sk2) == [] and "confirm" in sk2[0]["reason"]
    ok = S.approve(tmp_path / "s", off, lib, 700, lambda u: jpg(9), lambda *_: None, True)
    assert lib.get(ok[0].id).credit == "Photo: PIB (official source) - https://pib.gov.in/PressRelease?id=9"


# ------------------------------------------------------------------ the copyright-safe "sources" card
def test_sources_card_and_checker(tmp_path):
    from newschannel.pipeline import brand_from
    from newschannel.config import Config
    from newschannel.ledger import Claim, Ledger, Source
    led = Ledger("t", [Source("S1", "https://pib.gov.in/a", "Cabinet approves MSP hike", "pib.gov.in", 1, "2026-10-01", "x"),
                       Source("S2", "https://www.thehindu.com/b", "Cabinet clears MSP hike | The Hindu", "thehindu.com", 2, "", "x"),
                       Source("S3", "", "User notes", "user notes", 3, "", "x")],
                 [Claim("C1", "text", source_ids=["S1", "S2", "S3"], status="confirmed", numbers=[])])
    out = cards.build_card(led, {"type": "sources", "claim_ids": ["C1"]}, brand_from(Config.load()), "hinglish", 640, 360, tmp_path / "s.jpg")
    assert out and out.exists()
    assert cards.build_card(led, {"type": "sources", "claim_ids": ["C9"]}, brand_from(Config.load()), "hinglish", 640, 360, tmp_path / "x.jpg") is None
    from newschannel import deep
    scenes = [{"section": "hook", "beats": [{"type": "question", "text": "Kya sach hai?"}]},
              {"section": "recap", "card": {"type": "sources", "claim_ids": ["C1"]}, "beats": [{"type": "fact", "text": "Reports hain.", "claim_ids": ["C1"]}]}]
    bad, _ = deep.lint(scenes, led, "neutral", 0.1)
    assert not any("sources card" in b or "card cites" in b for b in bad)
    led.claims[0].source_ids = ["S3"]
    bad2, _ = deep.lint(scenes, led, "neutral", 0.1)
    assert any("sources card needs" in b for b in bad2)
