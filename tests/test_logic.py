from newschannel import curate, monetization as M, planner
from newschannel.models import Asset, Scene, Script, Story, Topic
from newschannel.scriptwriter import validate
from newschannel.config import Config
from newschannel.tts import words_from_alignment


def story(title, source, summary=""):
    return Story(title[:8] + source, title, summary, "http://x/" + title, source)


def test_single_source_topics_are_dropped():
    s = [story("PM announces new farm scheme today", "A"), story("PM announces new farm scheme", "B"),
         story("Lonely rumour about minister", "C")]
    topics = curate.curate(None, "m", s, min_sources=2)
    assert len(topics) == 1 and set(topics[0].sources) == {"A", "B"}


def test_policy_blocks_copying_and_missing_analysis():
    src = "the government announced a new scheme for farmers across the country on monday evening"
    topic = Topic("t", [story("x", "A", src), story("y", "B", "other")])
    script = Script("शीर्षक", "d", [], [Scene(src, "h", kind="news")])
    msgs = [i.msg for i in M.policy_check(script, topic, [], []) if i.level == "block"]
    assert any("copies" in m for m in msgs) and any("analysis" in m for m in msgs)


def test_daily_upload_cap_and_duplicate_title():
    topic = Topic("t", [story("x", "A"), story("y", "B")])
    script = Script("संसद में आज क्या हुआ", "d", [], [Scene("अलग शब्द", "h", kind="analysis")])
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    hist = [{"title": "कुछ और", "date": today}] * 4 + [{"title": "संसद में आज क्या हुआ", "date": "2020-01-01"}]
    msgs = " ".join(i.msg for i in M.policy_check(script, topic, [], hist, max_per_day=4))
    assert "uploads today" in msgs and "identical" in msgs


def test_ypp_progress_text():
    out = M.ypp_progress({"subs": 500, "watch_hours_365d": 2000, "shorts_views_90d": 0})
    assert "50%" in out[0] and "50%" in out[1]


def test_validate_enforces_word_budget():
    fmt = Config.load().fmt("short")
    script = Script("t", "d", [], [Scene("एक दो तीन", "h")] * 3)
    assert any("words" in p for p in validate(script, fmt))


def test_heuristic_planner_prefers_matching_caption_and_user_photos():
    assets = [Asset("a", "a.jpg", "stock", caption="beach"), Asset("b", "b.jpg", "user", caption="parliament building")]
    plan = planner.heuristic_plan([Scene("n", "h", "Indian parliament")], assets)
    assert plan[0][0].asset_id == "b"


def test_time_shots_drops_photos_that_would_flash_by():
    from newschannel.models import Shot
    plan = [[Shot("a", 0), Shot("b", 0), Shot("c", 0)]]
    shots = planner.time_shots(plan, [0.0], [5.0])
    assert len(shots) == 1 and shots[0].end == 5.0


def test_alignment_words():
    al = {"characters": list("हम चल"), "character_start_times_seconds": [0, .1, .2, .3, .4],
          "character_end_times_seconds": [.1, .2, .3, .4, .5]}
    w = words_from_alignment(al)
    assert [x.text for x in w] == ["हम", "चल"] and w[1].start == .3
