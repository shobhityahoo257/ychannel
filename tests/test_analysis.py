import json
import re
from pathlib import Path

import pytest
from conftest import FakeClient

from newschannel import analysis, cards, deep, ledger as L, research
from newschannel.deep import lint
from newschannel.ledger import Claim, Ledger, Source
from newschannel.review import Store
from newschannel.tts import MockTTS

PIB = ("The Union Cabinet on Tuesday approved a 15 percent increase in the minimum support price for wheat for the coming season. "
       "The Agriculture Minister Anil Rao said, \"This decision will benefit more than 2 crore farmers across the country.\" "
       "The government said the decision will cost the exchequer Rs 12000 crore in the first year. "
       "Officials said procurement will begin in April at all notified mandis. "
       "The ministry added that payments will be made directly to farmers' bank accounts within seven days of procurement.")
HINDU = ("The Cabinet on Tuesday cleared a 15 percent hike in the minimum support price for wheat, according to officials. "
         "Opposition leader Sunil Verma alleged that the hike was timed to influence the upcoming state elections and does not address input costs. "
         "Farmer unions said the increase is lower than the rise in fertiliser prices over the past two years. "
         "The state elections are scheduled for 14 November. "
         "Economists cited by the newspaper said the fiscal impact would be about Rs 11000 crore in the first year.")
BLOG = ("Insiders say the cabinet was scared of farmer protests and panicked into the decision. "
        "The hike is 15 percent according to our sources and will help 2 crore farmers. "
        "This blog believes the move is a very big deal for everyone who follows agriculture policy in India closely. "
        "Readers are welcome to share their own thoughts about the announcement in the comments section below this post.")
TEXTS = {"https://pib.gov.in/pr1": ("Cabinet approves MSP hike", "2026-10-01", PIB),
         "https://www.thehindu.com/a1": ("Cabinet clears MSP hike | The Hindu", "2026-10-01", HINDU),
         "https://someblog.example.com/p": ("My take", "", BLOG)}


def fake_fetch(url):
    return TEXTS[url]


def C(text, kind="fact", ev=None, **kw):
    return {"text": text, "kind": kind, "evidence": ev or text, **kw}


def sentence(text, start):
    return next(s for s in re.split(r'(?<=[.!?"])\s+(?=[A-Z])', text) if s.startswith(start))


def extract(kw):
    content = kw["messages"][0]["content"]
    if "(pib.gov.in)" in content:
        return {"claims": [
            C("The Union Cabinet approved a 15 percent increase in the MSP for wheat.", ev=sentence(PIB, "The Union Cabinet")),
            C("Agriculture Minister Anil Rao said the decision will benefit more than 2 crore farmers.", "quote",
              ev=sentence(PIB, "The Agriculture Minister"), speaker="Anil Rao",
              quote="This decision will benefit more than 2 crore farmers across the country."),
            C("The decision will cost the exchequer Rs 12000 crore in the first year.", "number", ev=sentence(PIB, "The government said")),
            C("Procurement will begin in April at notified mandis.", ev=sentence(PIB, "Officials said")),
            C("The cabinet also approved a 20 percent rise for rice.")]}                       # fabricated: not in the source
    if "(thehindu.com)" in content:
        return {"claims": [
            C("The Cabinet cleared a 15 percent hike in the MSP for wheat.", ev=sentence(HINDU, "The Cabinet on Tuesday")),
            C("Sunil Verma alleged the hike was timed to influence the upcoming state elections.", "allegation",
              ev=sentence(HINDU, "Opposition leader"), speaker="Sunil Verma"),
            C("State elections are scheduled for 14 November.", ev=sentence(HINDU, "The state elections"), date="14 November"),
            C("The fiscal impact would be about Rs 11000 crore in the first year, economists said.", "number",
              ev=sentence(HINDU, "Economists")),
            C("Farmer unions said the increase is 5 percent lower than the rise in fertiliser prices.",
              ev=sentence(HINDU, "Farmer unions"))]}                                          # number 5 is not in the evidence
    return {"claims": [
        C("The hike is 15 percent.", ev=sentence(BLOG, "The hike")),
        C("The cabinet panicked into the decision.", ev=sentence(BLOG, "Insiders"))]}


def group(kw):
    listing = kw["messages"][0]["content"]
    ids = {re.match(r"(X\d+)", ln).group(1): ln for ln in listing.splitlines()}
    fifteen = [i for i, ln in ids.items() if "15 percent" in ln and ("MSP" in ln or "hike" in ln)]
    cost = [i for i, ln in ids.items() if "Rs 1" in ln]
    return {"groups": [{"ids": fifteen}, {"ids": cost, "disputed": True, "note": "12000 vs 11000 crore"}]}


def client_for_research(stance="critical", minutes=7):
    return FakeClient({"submit_claims": extract, "group_claims": group,
                       "recommend_angle": {"stance": stance, "why": "Verified claims show a disputed cost and an election-timing allegation.",
                                           "minutes": minutes, "minutes_why": "8 claims"}})


@pytest.fixture
def led(tmp_path):
    srcs = [Source(f"S{i + 1}", u, t, L.outlet_of(u), L.classify_tier(u), p, txt)
            for i, (u, (t, p, txt)) in enumerate(TEXTS.items())]
    return deep.build_ledger(client_for_research(), "m", "MSP hike", srcs, "2 Oct 2026, 7:30 PM IST", lambda *_: None)


def by_text(led, frag):
    return next(c for c in led.claims if frag in c.text)


# ------------------------------------------------------------------ sources & verification (code, not AI)
def test_source_tiers():
    assert L.classify_tier("https://pib.gov.in/x") == 1 and L.classify_tier("https://some.gov.in/x") == 1
    assert L.classify_tier("https://www.thehindu.com/x") == 2 and L.classify_tier("https://random-blog.example.com") == 3
    assert L.friendly("pib.gov.in") == "PIB" and L.friendly("www.x.com".replace("www.", "")) == "X"


def test_article_extraction_ignores_navigation():
    html = ("<html><head><title>T | X</title></head><body><nav><p>menu item menu item menu item menu item menu</p></nav>"
            "<article><p>" + "A real paragraph of the article text. " * 3 + "</p><p>short</p></article></body></html>")
    title, _, text = research.extract_article(html)
    assert title == "T | X" and "real paragraph" in text and "menu item" not in text


def test_fabricated_claims_and_bad_numbers_are_dropped(led):
    texts = [c.text for c in led.claims]
    assert not any("rice" in t for t in texts)                       # evidence not in source
    assert not any("5 percent lower" in t for t in texts)            # number not in its evidence


def test_statuses_follow_source_quality_not_ai_opinion(led):
    assert by_text(led, "15 percent increase").status == "confirmed"          # official source + 3 outlets
    assert by_text(led, "Procurement will begin").status == "confirmed"       # official source alone
    assert by_text(led, "State elections").status == "reported"               # one major outlet only
    assert by_text(led, "alleged").status == "alleged"
    assert by_text(led, "Anil Rao said").status == "quoted"
    assert by_text(led, "panicked").status == "unverified"                    # weak source only -> never usable
    assert {by_text(led, "Rs 12000").status, by_text(led, "Rs 11000").status} == {"disputed"}
    assert "unverified" not in {c.status for c in led.usable()}
    assert set(by_text(led, "15 percent increase").source_ids) == {"S1", "S2", "S3"}


def test_quote_must_be_verbatim_and_named():
    c = Claim("X", "Someone said something.", "quote", speaker="A", quote="words that are not in the source at all", evidence="Hello world this is a sentence.")
    assert "evidence" in L.verify_claim(c, "Totally different text.")
    c2 = Claim("X", "Someone said x.", "quote", speaker="", quote="Hello world this is a sentence.", evidence="Hello world this is a sentence.")
    assert "speaker" in L.verify_claim(c2, "Hello world this is a sentence.")


def test_recommendations_and_fallback(led):
    assert deep.recommend(client_for_research("supportive", 9), "m", led)["stance"] == "supportive"
    assert deep.recommend(None, "m", led)["stance"] == "neutral"
    assert deep.recommend_minutes(3) == 5 and deep.recommend_minutes(12) == 9 and deep.recommend_minutes(40) == 15
    bogus = FakeClient({"recommend_angle": {"stance": "angry", "why": "", "minutes": 99}})
    r = deep.recommend(bogus, "m", led)
    assert r["stance"] == "neutral" and r["minutes"] == 15


# ------------------------------------------------------------------ the script checker
def good_scenes(led):
    c15, quote = by_text(led, "15 percent increase").id, by_text(led, "Anil Rao said").id
    elec, alleg = by_text(led, "State elections").id, by_text(led, "alleged").id
    c12, c11 = by_text(led, "Rs 12000").id, by_text(led, "Rs 11000").id
    B = lambda typ, text, ids=(), at="": {"type": typ, "text": text, "claim_ids": list(ids), "attributed_to": at}  # noqa: E731
    return [
        {"section": "hook", "headline": "MSP hike: sach kya hai?", "visual_query": "wheat farmers",
         "beats": [B("question", "Kya yeh MSP hike sach mein kisaan ke liye hai? Chaliye facts dekhte hain.")]},
        {"section": "recap", "headline": "Cabinet ka faisla", "visual_query": "cabinet meeting",
         "card": {"type": "number", "claim_ids": [c12], "label": "Pehle saal ka kharcha"},
         "beats": [B("fact", "Cabinet ne wheat ka MSP 15 percent badhaya.", [c15]),
                   B("fact", "Kharche par reports alag hain: PIB ke mutabik 12000 crore, The Hindu ke mutabik 11000 crore.", [c12, c11], "PIB")]},
        {"section": "context", "headline": "Elections ka calendar", "visual_query": "voting india",
         "beats": [B("fact", "The Hindu ke mutabik state elections 14 November ko hain.", [elec], "The Hindu")]},
        {"section": "positions", "headline": "Mantri ne kya kaha", "visual_query": "minister press conference",
         "card": {"type": "quote", "claim_ids": [quote], "label": ""},
         "beats": [B("quote", "Agriculture Minister Anil Rao ne kaha: \"This decision will benefit more than 2 crore farmers across the country.\"", [quote], "Anil Rao")]},
        {"section": "analysis", "headline": "Timing ka sawal", "visual_query": "indian politics",
         "beats": [B("analysis", "Evidence yeh suggest karta hai ki timing ka sawal jayaz hai, kyunki hike aur elections paas hain.", [c15, elec])]},
        {"section": "counterpoint", "headline": "Doosra paksh", "visual_query": "farmers india",
         "beats": [B("fact", "Opposition leader Sunil Verma ne aarop lagaya ki hike election se jodi gayi hai.", [alleg], "Sunil Verma"),
                   B("gap", "Hamein farmer unions ka official reply nahi mila.")]},
        {"section": "scenarios", "headline": "Aage kya?", "visual_query": "mandi india",
         "beats": [B("analysis", "Dekhna hoga ki 14 November se pehle procurement ka kya hota hai.", [elec])]},
        {"section": "close", "headline": "Aapki raay?", "visual_query": "india flag",
         "beats": [B("cta", "Aapki raay comment mein batayein aur channel subscribe karein.")]},
    ]


def words_of(scenes):
    return sum(len(b["text"].split()) for s in scenes for b in s["beats"])


def minutes_for(scenes):
    return words_of(scenes) / (60 * deep.WORDS_PER_SEC * 0.92)


def check(led, scenes, stance="critical"):
    return lint(scenes, led, stance, minutes_for(scenes))


def test_a_properly_sourced_script_passes(led):
    bad, soft = check(led, good_scenes(led))
    assert bad == [] and soft == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda s, led: s[1]["beats"][0].update(claim_ids=[]), "must cite at least one ledger claim"),
    (lambda s, led: s[1]["beats"][0].update(text="Cabinet ne wheat ka MSP 18 percent badhaya."), "not in the cited claims"),
    (lambda s, led: s[2]["beats"][0].update(attributed_to="", text="State elections 14 November ko hain."), "needs attribution"),
    (lambda s, led: s[3]["beats"][0].update(text='Anil Rao ne kaha: "This decision will help every single farmer in India."'), "not word-for-word"),
    (lambda s, led: s[4]["beats"][0].update(text="Sarkar kisaan protest se ghabra gayi thi, yeh saaf hai."), "banned wording"),
    (lambda s, led: s[4]["beats"][0].update(text="Sutron ke mutabik andar ki khabar yeh hai ki hike gir sakti hai."), "banned wording"),
    (lambda s, led: s[5]["beats"][0].update(claim_ids=[by_text(led, "panicked").id]), "unverified"),
    (lambda s, led: s[1]["beats"][0].update(claim_ids=["C99"]), "not in the ledger"),
    (lambda s, led: s.pop(5), "counterpoint"),
    (lambda s, led: s.insert(0, s.pop(1)), "must be section=hook"),
    (lambda s, led: s[3].update(card={"type": "quote", "claim_ids": [by_text(led, "15 percent increase").id]}), "needs quote claims"),
])
def test_checker_rejects_unsupported_scripts(led, mutate, expect):
    scenes = good_scenes(led)
    mutate(scenes, led)
    bad, _ = lint(scenes, led, "critical", minutes_for(good_scenes(led)))
    assert any(expect in b for b in bad), bad


def test_neutral_does_not_require_counterpoint_but_critical_does(led):
    scenes = [s for s in good_scenes(led) if s["section"] != "counterpoint"]
    assert not any("counterpoint" in b for b in check(led, scenes, "neutral")[0])
    assert any("counterpoint" in b for b in check(led, scenes, "critical")[0])


def test_loaded_words_are_warned_not_blocked(led):
    scenes = good_scenes(led)
    scenes[4]["beats"][0]["text"] = "Hike ke baad sarkar ne surrender kar diya, yeh evidence se jayaz lagta hai."
    bad, soft = check(led, scenes)
    assert not any("banned" in b for b in bad) and any("surrender" in s for s in soft)


def test_word_budget_is_enforced(led):
    scenes = good_scenes(led)
    bad, _ = lint(scenes, led, "critical", 6.0)
    assert any("words" in b for b in bad)


def test_writer_retries_with_the_checker_feedback(led):
    good = {"title": "MSP hike: kya sach hai?", "description": "d", "tags": ["msp"], "scenes": good_scenes(led)}
    bad_scenes = good_scenes(led)
    bad_scenes[1]["beats"][0]["text"] = "Cabinet ne wheat ka MSP 18 percent badhaya."
    answers = iter([{**good, "scenes": bad_scenes}, good])
    client = FakeClient({"submit_analysis": lambda kw: next(answers)})
    w = deep.write_analysis(client, "m", led, "critical", minutes_for(good_scenes(led)), "hinglish", "Chan", log=lambda *_: None)
    assert w.violations == [] and len(client.calls) == 2
    assert "FAILED the automatic fact check" in client.calls[1][1]["messages"][0]["content"]
    assert "STANCE - critical" in client.calls[0][1]["system"] and "Hinglish" in client.calls[0][1]["system"]
    s = w.script
    assert s.scenes[3].card["type"] == "quote" and "PIB" in s.scenes[1].source_tag and s.stance == "critical"
    assert s.scenes[3].label == "Statement" and s.scenes[4].kind == "analysis"


def test_writer_gives_up_after_three_tries_and_reports_violations(led):
    bad_scenes = good_scenes(led)
    bad_scenes[1]["beats"][0]["text"] = "Cabinet ne wheat ka MSP 18 percent badhaya."
    bad = {"title": "t", "description": "d", "tags": [], "scenes": bad_scenes}
    client = FakeClient({"submit_analysis": lambda kw: bad})
    w = deep.write_analysis(client, "m", led, "neutral", minutes_for(bad_scenes), "hinglish", "Chan", log=lambda *_: None)
    assert w.violations and len(client.calls) == 3


def test_chapters_follow_sections_and_respect_youtube_rules(led):
    from newschannel.models import Scene, Script
    secs = ["hook", "recap", "recap", "context", "positions", "analysis", "analysis", "close"]
    script = Script("t", "d", [], [Scene("n", "h", section=s) for s in secs])
    starts = [2, 30, 55, 80, 120, 160, 200, 240]
    ch = deep.chapters(script, starts, 2.0, "hinglish")
    assert ch[0] == ("0:00", "Intro") and ("0:30", "Kya hua") in ch and ("2:40", "Vishleshan") in ch and len(ch) == 6
    assert deep.chapters(Script("t", "d", [], [Scene("n", "h", section="hook")] * 2), [2, 8], 2.0, "hinglish") == []   # < 3 chapters


# ------------------------------------------------------------------ cards
def test_cards_use_ledger_text_only(led, tmp_path):
    from newschannel.pipeline import brand_from
    from newschannel.config import Config
    brand = brand_from(Config.load())
    q = by_text(led, "Anil Rao said").id
    n = by_text(led, "Rs 12000").id
    assert cards.build_card(led, {"type": "quote", "claim_ids": [q]}, brand, "hinglish", 640, 360, tmp_path / "q.jpg")
    assert cards.build_card(led, {"type": "number", "claim_ids": [n], "label": "Kharcha"}, brand, "hinglish", 640, 360, tmp_path / "n.jpg")
    assert cards._headline_number(by_text(led, "Rs 12000")) == "12000 crore" and cards._headline_number(by_text(led, "15 percent increase")) == "15%"
    assert cards.build_card(led, {"type": "number", "claim_ids": ["C99"]}, brand, "hinglish", 640, 360, tmp_path / "x.jpg") is None


# ------------------------------------------------------------------ end to end
def test_research_then_video_end_to_end(cfg, tmp_path):
    cfg.data["content"]["language"] = "hinglish"
    cfg.data["formats"]["analysis"].update(width=640, height=360, fps=12, max_scenes=40)
    cfg.data["endscreen"] = {"enabled": True, "seconds": 4}
    cfg.data["analysis"]["min_minutes"] = 0.2
    store = Store(Path(cfg["youtube"]["output_dir"]))
    client = client_for_research("critical", 0.2)
    res = analysis.run_research(cfg, client, store, "MSP hike", urls=list(TEXTS), fetch=fake_fetch, log=lambda *_: None)
    assert res["enough"] and res["recommend"]["stance"] == "critical" and res["counts"]["confirmed"] >= 2
    assert not any(c["status"] == "unverified" for c in res["ledger"]["claims"] if "panic" not in c["text"])
    led = Ledger.load(store.root / "_research" / res["research_id"] / "ledger.json")
    scenes = good_scenes(led)
    script = {"title": "MSP hike: kya sach hai?", "description": "Cabinet ka faisla aur uske sawal.", "tags": ["msp", "farmers"], "scenes": scenes}
    client.answers["submit_analysis"] = script
    client.answers["arrange_photos"] = lambda kw: {"scenes": []}
    meta = analysis.make_video(cfg, client, MockTTS(), store, res["research_id"], stance="auto", minutes=minutes_for(scenes),
                               preset="ultrafast", log=lambda *_: None)
    run = Path(meta["video"]).parent
    assert meta["format"] == "analysis" and meta["language"] == "hinglish" and meta["analysis"]["stance"] == "critical"
    assert not [i for i in meta["issues"] if i["level"] == "block"], meta["issues"]
    assert (run / "ledger.json").exists() and (run / "bed.wav").exists()                 # music bed was generated
    assert list((run / "cards").glob("*.jpg"))                                           # quote + number cards
    d = meta["description"]
    assert "[Official / primary]" in d and "https://pib.gov.in/pr1" in d and "[Other]" in d and "someblog.example.com" in d
    assert "As of " in d and "'Vishleshan' wale hisse hamari vyakhya" in d
    assert meta["analysis"]["sources"][0]["tier"] == 1
    # the finished video carries the on-screen source tag spans (via credits) and real audio
    info = json.loads(__import__("subprocess").run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json",
                                                    meta["video"]], capture_output=True, text=True).stdout)
    assert {s["codec_type"] for s in info["streams"]} == {"video", "audio"}


def test_script_with_failed_fact_check_is_blocked_from_approval(cfg, tmp_path):
    cfg.data["content"]["language"] = "hinglish"
    cfg.data["formats"]["analysis"].update(width=640, height=360, fps=12, max_scenes=40)
    cfg.data["endscreen"] = {"enabled": False}
    cfg.data["analysis"]["min_minutes"] = 0.2
    store = Store(Path(cfg["youtube"]["output_dir"]))
    client = client_for_research("neutral", 0.2)
    res = analysis.run_research(cfg, client, store, "MSP hike", urls=list(TEXTS), fetch=fake_fetch, log=lambda *_: None)
    led = Ledger.load(store.root / "_research" / res["research_id"] / "ledger.json")
    scenes = good_scenes(led)
    scenes[1]["beats"][0]["text"] = "Cabinet ne wheat ka MSP 18 percent badhaya."
    client.answers["submit_analysis"] = {"title": "t", "description": "d", "tags": [], "scenes": scenes}
    client.answers["arrange_photos"] = lambda kw: {"scenes": []}
    meta = analysis.make_video(cfg, client, MockTTS(), store, res["research_id"], stance="neutral", minutes=minutes_for(scenes),
                               preset="ultrafast", log=lambda *_: None)
    blocks = [i for i in meta["issues"] if i["level"] == "block"]
    assert blocks and "Fact check" in blocks[0]["msg"] and "18" in blocks[0]["msg"]


def test_research_needs_real_sources(cfg, tmp_path):
    store = Store(Path(cfg["youtube"]["output_dir"]))
    with pytest.raises(analysis.NotEnoughMaterial):
        analysis.run_research(cfg, client_for_research(), store, "Nothing", urls=[], fetch=fake_fetch, log=lambda *_: None)
    store2 = Store(tmp_path / "o2")
    client = FakeClient({"submit_claims": {"claims": []}, "recommend_angle": {"stance": "neutral", "why": "", "minutes": 5}})
    res = analysis.run_research(cfg, client, store2, "Thin", urls=["https://pib.gov.in/pr1"], fetch=fake_fetch, log=lambda *_: None)
    assert not res["enough"]
    with pytest.raises(analysis.NotEnoughMaterial):
        analysis.make_video(cfg, client, MockTTS(), store2, res["research_id"], log=lambda *_: None)
