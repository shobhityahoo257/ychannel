import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import FakeClient

from newschannel import analysis, cards, data, deep, discover, explainer, i18n, ledger as L, workflow as W
from newschannel.deep import lint
from newschannel.ledger import Claim, Ledger, Source
from newschannel.review import Store
from newschannel.tts import MockTTS, normalize

IMF = ("Central banks raise interest rates to slow inflation when prices are rising too fast. "
       "Higher policy rates make borrowing more expensive for households and firms, which reduces spending. "
       "Global inflation peaked at 8.7 percent in 2022 according to the IMF World Economic Outlook. "
       "Many central banks began tightening in 2022 after a long period of low rates. "
       "Economists disagree about how quickly higher rates feed through to prices in different economies.")
REUTERS = ("The US Federal Reserve raised its policy rate by 5.25 percentage points between March 2022 and July 2023, Reuters reported. "
           "The increase was the fastest tightening cycle in four decades, according to analysts cited by the agency. "
           "Officials said they would watch incoming data before deciding on further changes to the rate. "
           "Markets have been volatile as investors weigh how long rates will stay high this year.")
BLOG = ("My friend says rates are basically a magic lever and everyone who disagrees is silly. "
        "You should buy bonds now before everyone else does, according to this author. "
        "This blog believes inflation is entirely caused by greed and nothing else, which is a very strong claim indeed. "
        "Readers are welcome to leave their own thoughts about the topic in the comments section below this post today.")
TEXTS = {"https://www.imf.org/en/blogs/rates": ("How interest rates work", "2026-09-01", IMF),
         "https://www.reuters.com/markets/fed": ("Fed tightening explained | Reuters", "2026-09-02", REUTERS),
         "https://someblog.example.com/rates": ("My take", "", BLOG)}


def fake_fetch(url):
    return TEXTS[url]


def wb_http(url, params=None, headers=None, timeout=None):
    """Stands in for requests.get against the World Bank API."""
    code = url.rstrip("/").split("/")[-1]
    country = url.split("/country/")[1].split("/")[0] if "/country/" in url else ""
    if url.endswith("/country"):
        body = [{"page": 1}, [{"id": "BRA", "iso2Code": "BR", "name": "Brazil"}, {"id": "IND", "iso2Code": "IN", "name": "India"}]]
    else:
        vals = {"NY.GDP.MKTP.KD.ZG": [3.9, -5.8, 9.7, 7.0, 8.2], "FP.CPI.TOTL.ZG": [7.7, 6.2, 5.5, 6.7, 5.4]}[code]
        body = [{"page": 1, "lastupdated": "2025-07-01"},
                [{"indicator": {"id": code}, "country": {"id": country}, "date": str(2019 + i), "value": v} for i, v in enumerate(vals)]
                + [{"date": "2024", "value": None}]]
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


def wiki_http(url, params=None, headers=None, timeout=None):
    if params.get("list") == "search":
        body = {"query": {"search": [{"title": "Interest rate"}, {"title": "Monetary policy"}]}}
    else:
        body = {"parse": {"externallinks": [
            "https://www.imf.org/en/blogs/rates", "https://en.wikipedia.org/wiki/X", "https://www.reuters.com/markets/fed",
            "https://someblog.example.com/rates", "https://www.imf.org/report.pdf", "https://web.archive.org/web/2020/https://www.ft.com/a"]}}
    return SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)


def C(text, kind="fact", ev=None, **kw):
    return {"text": text, "kind": kind, "evidence": ev or text, **kw}


def sentence(text, start):
    return next(s for s in re.split(r'(?<=[.!?"])\s+(?=[A-Z])', text) if s.startswith(start))


def extract(kw):
    content = kw["messages"][0]["content"]
    if "(imf.org)" in content:
        return {"claims": [
            C("Central banks raise interest rates to slow inflation.", ev=sentence(IMF, "Central banks")),
            C("Higher policy rates make borrowing more expensive for households and firms.", ev=sentence(IMF, "Higher policy rates")),
            C("Global inflation peaked at 8.7 percent in 2022.", "number", ev=sentence(IMF, "Global inflation"), date="2022"),
            C("Economists disagree about how quickly rates feed through to prices.", ev=sentence(IMF, "Economists disagree"))]}
    if "(reuters.com)" in content:
        return {"claims": [
            C("The Fed raised its policy rate by 5.25 percentage points between March 2022 and July 2023.", "number",
              ev=sentence(REUTERS, "The US Federal Reserve"), date="March 2022 - July 2023"),
            C("The increase was the fastest tightening cycle in four decades.", ev=sentence(REUTERS, "The increase"))]}
    if "(data.worldbank.org)" in content:
        lines = [ln for ln in content.splitlines() if ln.startswith("In 20")][:3]
        return {"claims": [C(re.sub(r"^In (\d+), (.*)$", r"In \1: \2", ln), "number", ev=ln, date=ln[3:7]) for ln in lines]}
    return {"claims": [C("Rates are a magic lever.", ev=sentence(BLOG, "My friend"))]}


def client_for_research():
    return FakeClient({"submit_claims": extract, "group_claims": {"groups": []}})


def research(cfg, tmp_path, **kw):
    store = Store(Path(cfg["youtube"]["output_dir"]))
    args = dict(urls=list(TEXTS), fetch=fake_fetch, style="explainer", log=lambda *_: None)
    args.update(kw)
    res = analysis.run_research(cfg, client_for_research(), store, "How central banks fight inflation", **args)
    return store, res, analysis.load_research(store, res["research_id"])[0]


@pytest.fixture
def led(cfg, tmp_path):
    return research(cfg, tmp_path, data=[{"country": "India", "indicators": ["gdp_growth"]}], http=wb_http)[2]


def by_text(led, frag):
    return next(c for c in led.claims if frag in c.text)


def B(typ, text, ids=(), at=""):
    return {"type": typ, "text": text, "claim_ids": list(ids), "attributed_to": at}


def good_scenes(led):
    cb, borrow = by_text(led, "Central banks raise").id, by_text(led, "Higher policy rates").id
    infl, fed = by_text(led, "Global inflation").id, by_text(led, "Fed raised").id
    gdp = [c.id for c in led.claims if "India" in c.text]
    dis = by_text(led, "Economists disagree").id
    return [
        {"section": "hook", "headline": "Why do rates rise?", "visual_query": "central bank building",
         "beats": [B("question", "Why do central banks make borrowing more expensive on purpose? By the end you will see the logic.")]},
        {"section": "setup", "headline": "The question", "visual_query": "shopping prices",
         "beats": [B("fact", "Raising interest rates is the standard way central banks try to cool inflation.", [cb])]},
        {"section": "background", "headline": "A fast cycle", "visual_query": "federal reserve building",
         "beats": [B("fact", "According to Reuters, between March 2022 and July 2023 the Fed lifted rates by 5.25 percentage points.",
                     [fed], "Reuters")]},
        {"section": "mechanism", "headline": "How rates bite", "visual_query": "bank customer",
         "beats": [B("concept", "Think of the economy as a room and the rate as a thermostat: turning it up cools things down."),
                   B("fact", "When the policy rate goes up, loans cost more for families and companies.", [borrow])]},
        {"section": "evidence", "headline": "What the data shows", "visual_query": "inflation chart",
         "card": {"type": "chart", "claim_ids": gdp[:1], "label": "GDP growth in India"},
         "beats": [B("fact", "In 2022 worldwide inflation reached its peak, at 8.7 percent.", [infl]),
                   B("fact", "India's economy grew 3.9 percent in 2019.", [gdp[0]])]},
        {"section": "impact", "headline": "Who feels it", "visual_query": "mortgage house",
         "beats": [B("analysis", "This suggests households with loans feel the change first, because borrowing costs rise for them.", [borrow])]},
        {"section": "counterview", "headline": "Not settled", "visual_query": "economists meeting",
         "beats": [B("fact", "Experts do not agree on how fast rate changes reach prices.", [dis]),
                   B("gap", "We could not find a source on how this played out for small firms.")]},
        {"section": "takeaway", "headline": "Remember this", "visual_query": "city skyline",
         "beats": [B("cta", "That is the logic of higher rates. What would you like explained next? Tell us in the comments and subscribe.")]},
    ]


def minutes_for(scenes):
    return sum(len(b["text"].split()) for s in scenes for b in s["beats"]) / (60 * deep.WORDS_PER_SEC * 0.92)


def check(led, scenes):
    return lint(scenes, led, "neutral", minutes_for(scenes), style="explainer")


# ------------------------------------------------------------------ English + money spoken correctly
def test_english_is_a_complete_language_with_correct_money_pronunciation():
    base = set(i18n.LABELS["hinglish"])
    assert all(set(i18n.LABELS[lang]) == base for lang in i18n.LANGS) and i18n.norm("english") == "english"
    assert "GLOBAL audience" in i18n.LANG_RULES["english"]
    assert normalize("GDP hit $2.5 trillion, up 7% & US$300 billion, €5 million", "english") == \
        "GDP hit 2.5 trillion dollars, up 7 percent and 300 billion US dollars, 5 million euros"


def test_english_voice_and_youtube_language_follow_the_script(cfg):
    from newschannel.tts import OpenAITTS
    cfg.data["content"]["language"] = "english"
    import os
    os.environ.setdefault("OPENAI_API_KEY", "x")
    t = OpenAITTS(cfg)
    assert t.stt_lang == "en" and "documentary narrator" in t.instructions


# ------------------------------------------------------------------ official data
def test_country_resolution_and_unknown_country():
    assert data.resolve_country("India") == ("IND", "India")
    assert data.resolve_country("usa")[0] == "USA" and data.resolve_country("IND")[0] == "IND"
    assert data.resolve_country("brazil", wb_http) == ("BRA", "Brazil")
    with pytest.raises(data.DataError):
        data.resolve_country("Atlantis", wb_http)


def test_world_bank_series_becomes_an_official_source_with_chart_data():
    srcs = data.gather_data([{"country": "India", "indicators": ["gdp_growth", "inflation", "nonsense"]},
                             {"country": "Atlantis", "indicators": ["gdp"]}], http=wb_http, log=lambda *_: None)
    assert [s.id for s in srcs] == ["S1", "S2"] and all(s.tier == 1 and s.outlet == "data.worldbank.org" for s in srcs)
    s = srcs[0]
    assert s.series["points"][0] == [2019, 3.9] and len(s.series["points"]) == 5          # the empty 2024 value is skipped
    assert "In 2020, GDP growth in India was minus 5.8 percent." in s.text and "highest in 2021 at 9.7 percent" in s.text
    assert s.published == "2025-07-01" and L.friendly(s.outlet) == "World Bank"


def test_data_claims_are_verified_and_confirmed_by_code(led):
    c = by_text(led, "India")
    assert c.status == "confirmed" and "S" in c.source_ids[0] and c.numbers
    assert any(s.series for s in led.sources)


def test_series_survive_saving_the_ledger(led, tmp_path):
    led.save(tmp_path / "l.json")
    again = Ledger.load(tmp_path / "l.json")
    assert [s.series["points"] for s in again.sources if s.series] == [s.series["points"] for s in led.sources if s.series]


# ------------------------------------------------------------------ finding sources
def test_discovery_follows_wikipedia_citations_but_never_uses_wikipedia_itself():
    urls = discover.discover("interest rates", http=wiki_http, log=lambda *_: None)
    assert "https://www.imf.org/en/blogs/rates" in urls and "https://www.reuters.com/markets/fed" in urls
    assert any("ft.com" in u for u in urls)                                                # archive copy judged by the original site
    assert not any("wikipedia" in u or "someblog" in u or u.endswith(".pdf") for u in urls)
    assert urls.index("https://www.imf.org/en/blogs/rates") < urls.index("https://www.reuters.com/markets/fed")   # official first


def test_research_combines_discovered_user_and_data_sources(cfg, tmp_path):
    store, res, led = research(cfg, tmp_path, urls=["https://someblog.example.com/rates"], discover=True,
                               data=[{"country": "India", "indicators": ["inflation"]}], http=lambda *a, **k: (
                                   wb_http(*a, **k) if "worldbank" in a[0] else wiki_http(*a, **k)))
    outlets = {s.outlet for s in led.sources}
    assert {"imf.org", "reuters.com", "data.worldbank.org", "someblog.example.com"} <= outlets
    assert res["style"] == "explainer" and res["recommend"]["stance"] == "neutral" and res["recommend"]["minutes"] >= 5
    assert res["enough"] and res["sources_found"] == len(led.sources)
    assert by_text(led, "Central banks raise").status == "confirmed"                       # official source
    assert by_text(led, "fastest tightening").status == "reported"                         # one outlet: must be credited by name
    assert by_text(led, "magic lever").status == "unverified"                              # weak blog: never usable
    assert "Wikipedia" not in " ".join(s.title for s in led.sources)


# ------------------------------------------------------------------ the script checker (explainer rules)
def test_a_well_sourced_explainer_passes(led):
    bad, soft = check(led, good_scenes(led))
    assert bad == [] and soft == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda s, led: s.pop(3), "section=mechanism"),
    (lambda s, led: s.pop(4), "section=evidence"),
    (lambda s, led: s.pop(6), "section=counterview"),
    (lambda s, led: s.pop(), "last scene must be section=takeaway"),
    (lambda s, led: s[3]["beats"][0].update(text="Think of 3 thermostats in a room."), "must not contain digits"),
    (lambda s, led: s[3]["beats"][0].update(text="Think of the economy as a room, and the central bank as a thermostat."
                                                  " You should buy bonds now."), "banned wording"),
    (lambda s, led: s[4]["beats"][0].update(text="Global inflation peaked at 9.9 percent in 2022."), "not in the cited claims"),
    (lambda s, led: s[2]["beats"][0].update(attributed_to="", text="The Fed raised its policy rate by 5.25 percentage points between March 2022 and July 2023."),
     "needs attribution"),
    (lambda s, led: s[4].update(card={"type": "chart", "claim_ids": [by_text(led, "Central banks raise").id]}), "needs claims that come from a data series"),
    (lambda s, led: s[4].update(card={"type": "chart", "claim_ids": ["C99"]}), "unknown/unusable claims"),
])
def test_explainer_checker_rejects_unsupported_scripts(led, mutate, expect):
    scenes = good_scenes(led)
    mutate(scenes, led)
    bad, _ = lint(scenes, led, "neutral", minutes_for(good_scenes(led)), style="explainer")
    assert any(expect in b for b in bad), bad


def test_copying_a_source_sentence_is_caught_before_rendering(led):
    scenes = good_scenes(led)
    scenes[1]["beats"][0]["text"] = "Central banks raise interest rates to slow inflation when prices are rising too fast."
    bad, _ = check(led, scenes)
    assert any("copies 8+ consecutive words from a source" in b for b in bad)
    scenes = good_scenes(led)       # a verbatim quote is allowed to match its source
    scenes[1]["beats"][0]["text"] = 'The IMF wrote: "Higher policy rates make borrowing more expensive for households and firms".'
    assert not any("copies 8+" in b for b in check(led, scenes)[0])


def test_too_much_unsourced_explanation_is_blocked(led):
    scenes = good_scenes(led)
    scenes[3]["beats"].append(B("concept", "Imagine a garden where water is rationed so every plant gets a little less. " * 6))
    bad, _ = lint(scenes, led, "neutral", minutes_for(scenes) * 1.0, style="explainer")
    assert any("unsourced concept explanation" in b for b in bad)


def test_concept_beats_are_not_allowed_in_political_analysis(led):
    scenes = good_scenes(led)
    bad, _ = lint(scenes, led, "neutral", minutes_for(scenes), style="analysis")
    assert any("only allowed in explainers" in b for b in bad)


def test_invented_names_in_a_concept_beat_are_flagged(led):
    scenes = good_scenes(led)
    scenes[3]["beats"][0]["text"] = "Think of the economy as a room. Then Goldman and Brazil turn the thermostat."
    bad, soft = check(led, scenes)
    assert any("Goldman" in s for s in soft)


# ------------------------------------------------------------------ writing + cards
def test_writer_uses_the_explainer_prompt_and_marks_the_script(led):
    scenes = good_scenes(led)
    client = FakeClient({"submit_analysis": {"title": "Why central banks raise rates", "description": "How rates fight inflation.",
                                              "tags": ["rates"], "scenes": scenes}})
    w = deep.write_analysis(client, "m", led, "neutral", minutes_for(scenes), "english", "Money Explained", style="explainer",
                            log=lambda *_: None)
    system, user = client.calls[0][1]["system"], client.calls[0][1]["messages"][0]["content"]
    assert w.violations == [] and w.script.style == "explainer" and w.script.language == "english"
    assert "documentary narrator" in system and "NOT investment advice" in system and "GLOBAL audience" in system
    assert "DATA SERIES" in user and "GDP growth - India" in user                          # chart-able claims are listed for the writer
    schema = client.calls[0][1]["tools"][0]["input_schema"]
    sections = schema["properties"]["scenes"]["items"]["properties"]["section"]["enum"]
    assert "mechanism" in sections and "recap" not in sections
    s = w.script
    assert [sc.section for sc in s.scenes][:2] == ["hook", "setup"] and s.scenes[3].kind == "analysis" and s.scenes[3].label == "How it works"
    assert s.scenes[4].card["type"] == "chart" and s.scenes[0].kind == "news" and s.scenes[-1].kind == "outro"


def test_chart_card_draws_the_world_bank_series(led, tmp_path):
    from newschannel.config import Config
    from newschannel.pipeline import brand_from
    gdp = [c.id for c in led.claims if "India" in c.text][:1]
    assert cards.build_card(led, {"type": "chart", "claim_ids": gdp, "label": "GDP growth"}, brand_from(Config.load()), "english",
                            640, 360, tmp_path / "c.jpg")
    assert (tmp_path / "c.jpg").stat().st_size > 2000
    assert cards.build_card(led, {"type": "chart", "claim_ids": [by_text(led, "Central banks raise").id]}, brand_from(Config.load()),
                            "english", 640, 360, tmp_path / "n.jpg") is None


def test_chart_compares_countries_only_for_the_same_unit(led):
    srcs = data.gather_data([{"country": "India", "indicators": ["gdp_growth"]}, {"country": "Brazil", "indicators": ["gdp_growth"]}],
                            http=wb_http, log=lambda *_: None)
    pct = srcs[0]
    usd = data.data_source("S3", "India", "IND", "gdp", [(2020, 2e12), (2021, 3e12), (2022, 3.3e12)], "")
    lg = Ledger("t", [pct, srcs[1], usd], [Claim("C1", "a", source_ids=["S1"], status="confirmed"),
                                           Claim("C2", "b", source_ids=["S2"], status="confirmed"),
                                           Claim("C3", "c", source_ids=["S3"], status="confirmed")])
    assert [s.id for s in deep.chart_series(lg, lg.claims)] == ["S1", "S2"]               # the US$ series is not mixed with percentages


# ------------------------------------------------------------------ topic ideas
def raw_ideas():
    return [{"title": "How central banks fight inflation", "question": "q", "series": "how_it_works", "why": "w",
             "data_hooks": [{"country": "India", "indicators": ["inflation", "made_up"]}], "source_hints": ["IMF"], "difficulty": "easy"},
            {"title": "10 ways money works", "question": "q", "series": "how_it_works"},                    # digits: dropped
            {"title": "How central banks fight inflation today", "question": "q", "series": "how_it_works"},   # near-duplicate: dropped
            {"title": "Why currencies collapse", "question": "q", "series": "nonsense", "difficulty": "wild"},
            {"title": "", "question": "q", "series": "how_it_works"}]


def test_ideas_are_cleaned_in_code():
    out = explainer.clean_ideas(raw_ideas(), taken=[], n=8)
    assert [i["title"] for i in out] == ["How central banks fight inflation", "Why currencies collapse"]
    assert out[0]["data_hooks"] == [{"country": "India", "indicators": ["inflation"]}]
    assert out[1]["series"] == "how_it_works" and out[1]["difficulty"] == "medium"
    assert explainer.clean_ideas(raw_ideas(), taken=["How central banks fight inflation"], n=8)[0]["title"] == "Why currencies collapse"


def test_the_idea_bank_never_repeats_itself(cfg, tmp_path):
    ideas = explainer.Ideas(tmp_path / "ideas.json")
    client = FakeClient({"submit_ideas": lambda kw: {"ideas": raw_ideas()}})
    first = explainer.suggest(cfg, client, ideas, n=5, focus="inflation", log=lambda *_: None)
    assert len(first) == 2 and "inflation" in client.calls[0][1]["messages"][0]["content"]
    assert "NO numbers" in client.calls[0][1]["system"] and "economics and business" in client.calls[0][1]["system"]
    assert explainer.suggest(cfg, client, ideas, n=5, log=lambda *_: None) == []          # everything was already suggested
    assert "How central banks fight inflation" in client.calls[1][1]["messages"][0]["content"]
    ideas.mark_made("Why currencies collapse")
    assert {i["title"]: i["status"] for i in ideas.load()}["Why currencies collapse"] == "made"


# ------------------------------------------------------------------ channel profile
def test_explainer_profile_changes_channel_category_and_playlists_without_touching_the_original(cfg):
    ex = explainer.profile_copy(cfg, "explainer", "english")
    assert ex["channel"]["name"] == "Money Explained" and ex["youtube"]["category_id"] == "27" and ex.lang == "english"
    assert ex["playlists"]["format_playlists"] == {"analysis": "Explainers"} and "Economy Explained" in ex["playlists"]["categories"]
    assert cfg["channel"]["name"] != "Money Explained" and cfg["youtube"]["category_id"] == "25"      # the news channel is unchanged
    assert explainer.profile_copy(cfg, "analysis")["channel"] == cfg["channel"]


# ------------------------------------------------------------------ end to end through the approval workflow
def test_explainer_goes_through_the_approval_steps_and_is_packaged_for_a_global_channel(cfg, tmp_path):
    cfg.data["formats"]["analysis"].update(width=640, height=360, fps=12, max_scenes=40)
    cfg.data["endscreen"] = {"enabled": True, "seconds": 4}
    cfg.data["analysis"]["min_minutes"] = 0.2
    store, res, led = research(cfg, tmp_path, data=[{"country": "India", "indicators": ["gdp_growth"]}], http=wb_http)
    scenes = good_scenes(led)
    client = client_for_research()
    client.answers["submit_analysis"] = {"title": "Why central banks raise rates", "description": "How rates fight inflation.",
                                         "tags": ["interest rates", "inflation"], "scenes": scenes}
    client.answers["arrange_photos"] = lambda kw: {"scenes": []}
    wf = W.Workflow.create(store, "analysis", "How central banks fight inflation", "explainer", "analysis",
                           {"research_id": res["research_id"], "stance": "auto", "minutes": str(minutes_for(scenes)), "language": "english",
                            "music": "calm", "library_ids": [], "style": "explainer"}, {"script": "manual", "photos": "manual"})
    assert W.advance(cfg, client, MockTTS(), wf, lambda *_: None, "ultrafast")["status"] == "script_review"
    assert wf.params()["stance"] == "neutral"
    view = W.describe_script(wf)
    assert view["style"] == "explainer" and any(b["type"] == "concept" for b in view["scenes"][3]["beats"])
    assert W.list_drafts(store)[0]["style"] == "explainer"
    # the editor's changes are held to the explainer rules
    r = W.save_script_edits(cfg, wf, {"scenes": [{}, {}, {}, {"beats": [{"text": "Think of 3 thermostats in a room."}]}]})
    assert any("digits" in v for v in r["violations"])
    r = W.save_script_edits(cfg, wf, {"scenes": [{}, {}, {}, {"beats": [{"text": "Think of the economy as a room and the rate as a thermostat."}]}]})
    assert r["violations"] == []
    assert W.approve_script(cfg, client, MockTTS(), wf, lambda *_: None, preset="ultrafast")["status"] == "media_review"
    W.approve_media(cfg, client, MockTTS(), wf, lambda *_: None, "ultrafast")
    meta = wf.store.meta(wf.id)
    assert not [i for i in meta["issues"] if i["level"] == "block"], meta["issues"]
    assert meta["style"] == "explainer" and meta["language"] == "english" and meta["category_id"] == "27"
    assert "economics" in meta["default_tags"] and meta["analysis"]["style"] == "explainer"
    d = meta["description"]
    assert "not financial, investment or legal advice" in d and "[Official / primary]" in d and "https://www.imf.org/en/blogs/rates" in d
    assert "data.worldbank.org" in d and "Chapters:" in d and "How it works" in d and "Money Explained" in d
    assert any("investment advice" in i["msg"] for i in meta["issues"])                    # reminder shown in the review
    assert list((Path(meta["video"]).parent / "cards").glob("card_4.jpg"))                  # the chart scene
    script = json.loads((Path(meta["video"]).parent / "script.json").read_text(encoding="utf-8"))
    assert script["style"] == "explainer"


# ------------------------------------------------------------------ command line
def test_command_line_explainer_research_and_ideas(cfg, tmp_path, monkeypatch, capsys):
    from newschannel import cli
    cfg.data["youtube"]["output_dir"] = str(tmp_path / "cli_out")
    fc = client_for_research()
    fc.answers["submit_ideas"] = lambda kw: {"ideas": raw_ideas()}
    monkeypatch.setattr(cli, "_client", lambda c, required=True: fc)
    monkeypatch.setattr("newschannel.research.fetch_article", fake_fetch)
    monkeypatch.setattr(data.requests, "get", lambda url, **kw: wb_http(url, **kw) if "worldbank" in url else wiki_http(url, **kw))
    assert cli._parse_data(["India:gdp_growth, inflation", "Brazil:gdp"]) == [
        {"country": "India", "indicators": ["gdp_growth", "inflation"]}, {"country": "Brazil", "indicators": ["gdp"]}]
    assert cli.cmd_explainer(cfg, SimpleNamespace(topic="x", url=[], data=["India:nope"], notes=None, notes_file=None,
                                                  no_discover=True, research_only=True)) == 2
    assert "Choose from" in capsys.readouterr().out
    a = SimpleNamespace(topic="How central banks fight inflation", url=list(TEXTS), data=["India:gdp_growth"], notes=None,
                        notes_file=None, no_discover=True, research_only=True)
    assert cli.cmd_explainer(cfg, a) == 0
    out = capsys.readouterr().out
    assert "Sources read: 4" in out and "Recommended length" in out
    assert cli.cmd_ideas(cfg, SimpleNamespace(n=5, focus="inflation", series="")) == 0
    out = capsys.readouterr().out
    assert "How central banks fight inflation" in out and "Ideas are leads, not facts" in out


# ------------------------------------------------------------------ publishing keeps the two channels apart
def test_explainers_publish_with_their_own_playlists_category_and_never_link_to_news(cfg, tmp_path, monkeypatch):
    from newschannel import daily
    from test_playlists import FakeYT
    yt = FakeYT()
    yt.videos_db["EXPREV"] = {"title": "Older explainer", "categoryId": "27", "description": "d"}
    uploaded = {}
    monkeypatch.setattr("newschannel.youtube.service", lambda *a, **k: yt)
    monkeypatch.setattr("newschannel.youtube.upload", lambda video, thumb, meta, ytcfg, short, at=None:
                        uploaded.update(meta=meta, ytcfg=ytcfg) or "NEWEXP")
    store = Store(Path(cfg["youtube"]["output_dir"]))
    store.add_history("A news video", "t", "NEWSVID", category="Chunav", format="short", style="")
    store.add_history("A political analysis", "t", "ANAVID", category="Chunav", format="analysis", style="analysis")
    store.add_history("Older explainer", "t", "EXPREV", category="Economy Explained", format="analysis", style="explainer")
    d = store.run_dir("e1")
    meta = {"id": "e1", "status": "approved", "format": "analysis", "style": "explainer", "language": "english", "title": "Why rates rise",
            "topic": "rates", "category": "Economy Explained", "category_id": "27", "default_tags": ["economics"],
            "video": str(d / "v.mp4"), "thumbnail": str(d / "t.jpg"), "description": "base", "issues": [], "video_id": None}
    store.save_meta("e1", meta)
    assert daily.publish_one(cfg, store, meta) == "NEWEXP"
    desc = uploaded["meta"]["description"]
    assert "youtu.be/EXPREV" in desc and "NEWSVID" not in desc and "ANAVID" not in desc          # "watch next" stays on this channel
    assert set(yt.playlists_db) >= {"Economy Explained", "Explainers"} and "Deep Analysis" not in yt.playlists_db
    assert "youtu.be/NEWEXP" in yt.videos_db["EXPREV"]["description"]
    assert uploaded["ytcfg"]["category_id"] == "27" and (store.root / "playlists_explainer.json").exists()
    assert not (store.root / "playlists.json").exists() and store.history()[-1]["style"] == "explainer"
    # a political-analysis video still uses the news channel's playlists and links to the news videos
    d2 = store.run_dir("a1")
    meta2 = {"id": "a1", "status": "approved", "format": "analysis", "style": "analysis", "language": "hinglish", "title": "MSP analysis",
             "topic": "msp", "category": "Chunav", "video": str(d2 / "v.mp4"), "thumbnail": str(d2 / "t.jpg"), "description": "base",
             "issues": [], "video_id": None}
    store.save_meta("a1", meta2)
    assert daily.publish_one(cfg, store, meta2) == "NEWEXP"
    desc2 = uploaded["meta"]["description"]
    assert ("youtu.be/NEWSVID" in desc2 or "youtu.be/ANAVID" in desc2) and "EXPREV" not in desc2
    assert "Deep Analysis" in yt.playlists_db and uploaded["ytcfg"]["category_id"] == "25" and (store.root / "playlists.json").exists()


def test_lessons_are_learned_per_channel(cfg, tmp_path):
    from newschannel import learn
    store = Store(Path(cfg["youtube"]["output_dir"]))
    perf = {}
    for i in range(5):
        store.add_history(f"News {i}", "t", f"N{i}", hook=f"news hook {i}", format="long", style="")
        store.add_history(f"Explainer {i}", "t", f"E{i}", hook=f"explainer hook {i}", format="long", style="explainer")
        perf[f"N{i}"] = {"views": 100 + i, "avg_pct": 30.0}
        perf[f"E{i}"] = {"views": 500 + i, "avg_pct": 60.0}
    learn.perf_path(store).write_text(json.dumps(perf), encoding="utf-8")
    assert {r["title"][:4] for r in learn.rows(store, "explainer")} == {"Expl"} and len(learn.rows(store)) == 10
    assert "explainer hook" in learn.prompt_hint(store, "explainer") and "news hook" not in learn.prompt_hint(store, "explainer")
    assert "news hook" in learn.prompt_hint(store, "") and "explainer hook" not in learn.prompt_hint(store, "")
    assert learn.insights(store)["total"] == 10                                                # no style = everything (the Settings page)


def test_a_model_answer_with_null_fields_does_not_crash_research():
    src = Source("S1", "https://www.federalreserve.gov/a", "Fed", "federalreserve.gov", 1, "", IMF)
    ans = {"claims": [
        {"text": "Central banks raise interest rates to slow inflation.", "kind": "fact", "speaker": None, "quote": None, "date": None,
         "evidence": sentence(IMF, "Central banks")},
        {"text": None, "evidence": "x"}, {"text": "No evidence", "evidence": None}, "junk",
        {"text": "Higher policy rates make borrowing more expensive for households and firms.", "kind": None,
         "evidence": sentence(IMF, "Higher policy rates")}]}
    led = deep.build_ledger(FakeClient({"submit_claims": ans, "group_claims": {"groups": []}}), "m", "t", [src], "", lambda *_: None,
                            "explainer")
    assert len(led.claims) == 2 and all(c.status == "confirmed" for c in led.claims)
