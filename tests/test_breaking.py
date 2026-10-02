import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from conftest import SCRIPT, FakeClient

from newschannel.breaking import BreakingWatcher, State, in_active_hours
from newschannel.models import Story
from newschannel.pipeline import manual_topic, produce
from newschannel.review import Store
from newschannel.tts import MockTTS

IST = ZoneInfo("Asia/Kolkata")
NOON = datetime(2026, 10, 2, 12, 0, tzinfo=IST).timestamp()


def st(i, source, title="PM resigns", t=NOON):
    return Story(f"id{i}", f"{title} {i}", "summary", f"http://x/{i}", source, t)


class Curator(FakeClient):
    """Answers pick_topics: groups the ids it is given into one topic with a chosen importance."""

    def __init__(self, importance=9):
        super().__init__({"pick_topics": self.answer})
        self.importance = importance

    def answer(self, kw):
        n = kw["messages"][0]["content"].count("\n| ") or kw["messages"][0]["content"].count(" | ") // 3
        return {"topics": [{"title": "Big event", "ids": [0, 1, 2], "why": "w", "importance": self.importance}]}


def watcher(cfg, tmp_path, stories, client=None, made=None, now=NOON):
    cfg.data["youtube"]["output_dir"] = str(tmp_path / "out")
    cfg.data["breaking"].update(active_hours="06:00-23:30", min_sources=3, min_importance=7, max_alerts_per_day=2)
    clock = [now]
    feed = [list(stories)]
    w = BreakingWatcher(cfg, client or Curator(), Store(tmp_path / "out"), fetch=lambda feeds, hrs: feed[0],
                        make=lambda t: (made.append(t.title), {"id": f"run{len(made)}"})[1] if made is not None else {"id": "r"},
                        now=lambda: clock[0], log=lambda *_: None)
    return w, clock, feed


def test_three_outlets_and_high_importance_triggers_once(cfg, tmp_path):
    made = []
    w, clock, feed = watcher(cfg, tmp_path, [st(0, "A"), st(1, "B"), st(2, "C")], made=made)
    assert len(w.run_once()) == 1 and made == ["Big event"]
    assert w.run_once() == [] and made == ["Big event"]          # same stories: nothing new, no repeat
    feed[0] += [st(3, "D")]                                       # a new story about the SAME event
    assert w.run_once() == []                                     # already alerted -> no second alert


def test_too_few_outlets_or_low_importance_do_not_trigger(cfg, tmp_path):
    made = []
    w, *_ = watcher(cfg, tmp_path, [st(0, "A"), st(1, "B"), st(2, "A")], made=made)      # only 2 distinct outlets
    assert w.run_once() == [] and made == []
    w2, *_ = watcher(cfg, tmp_path / "x", [st(0, "A"), st(1, "B"), st(2, "C")], client=Curator(importance=5), made=made)
    assert w2.run_once() == [] and made == []


def test_no_llm_call_when_nothing_is_new(cfg, tmp_path):
    client = Curator(importance=4)
    w, clock, feed = watcher(cfg, tmp_path, [st(0, "A"), st(1, "B"), st(2, "C")], client=client)
    w.run_once()
    calls = len(client.calls)
    w.run_once(); w.run_once()
    assert len(client.calls) == calls                              # unchanged feed -> zero extra cost


def test_daily_cap_and_active_hours(cfg, tmp_path):
    made = []
    w, clock, feed = watcher(cfg, tmp_path, [st(0, "A"), st(1, "B"), st(2, "C")], made=made, now=datetime(2026, 10, 2, 3, 0, tzinfo=IST).timestamp())
    assert w.run_once() == []                                      # 03:00 is outside 06:00-23:30
    clock[0] = NOON
    w.state.alerted = [{"title": f"old{i}", "story_ids": [], "ts": NOON - 60, "run_id": ""} for i in range(2)]
    assert w.run_once() == [] and made == []                       # cap of 2 already used today
    assert in_active_hours("06:00-23:30", datetime(2026, 10, 2, 23, 40, tzinfo=IST)) is False


def test_state_survives_restart(cfg, tmp_path):
    made = []
    w, *_ = watcher(cfg, tmp_path, [st(0, "A"), st(1, "B"), st(2, "C")], made=made)
    w.run_once()
    w2, *_ = watcher(cfg, tmp_path, [st(0, "A"), st(1, "B"), st(2, "C")], made=made)
    assert w2.run_once() == [] and made == ["Big event"]
    assert State.load(tmp_path / "out" / "breaking_state.json").alerted[0]["title"] == "Big event"


def test_a_failed_video_is_not_retried_forever(cfg, tmp_path):
    cfg.data["youtube"]["output_dir"] = str(tmp_path / "out")
    w = BreakingWatcher(cfg, Curator(), Store(tmp_path / "out"), fetch=lambda f, h: [st(0, "A"), st(1, "B"), st(2, "C")],
                        make=lambda t: (_ for _ in ()).throw(RuntimeError("boom")), now=lambda: NOON, log=lambda *_: None)
    assert w.run_once() == [] and len(w.state.alerted) == 1


def test_breaking_video_is_labelled_short_and_warns(cfg):
    cfg.data["breaking"]["target_seconds"] = 12
    client = FakeClient({"submit_script": SCRIPT, "arrange_photos": lambda kw: {"scenes": []}})
    store = Store(Path(cfg["youtube"]["output_dir"]))
    meta = produce(cfg, manual_topic("बड़ी खबर", "तथ्य"), "short", client, MockTTS(), store, preset="ultrafast",
                   log=lambda *_: None, breaking=True)
    assert meta["breaking"] is True and meta["format"] == "short"
    assert any("BREAKING" in i["msg"] for i in meta["issues"])
    saved = json.loads((Path(meta["video"]).parent / "script.json").read_text(encoding="utf-8"))
    assert saved["scenes"][0]["label"] == "ब्रेकिंग न्यूज़"
    prompt = next(kw for n, kw in client.calls if n == "submit_script")["messages"][0]["content"]
    assert "BREAKING" in prompt
