import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import SCRIPT, FakeClient

from newschannel import learn, packaging
from newschannel.config import Config
from newschannel.models import Scene, Script, Story, Topic
from newschannel.pipeline import manual_topic, produce
from newschannel.review import Store
from newschannel.scheduler import Scheduler
from newschannel.tts import MockTTS


def topic():
    return Topic("t", [Story("1", "संसद में बजट पर बहस", "सरकार ने 15 प्रतिशत बढ़ोतरी की बात कही", "u", "A")])


def script():
    return Script("पुराना शीर्षक", "d", [], [Scene("संसद में आज बजट पर बहस हुई और सरकार ने बात रखी", "h"),
                                         Scene("विश्लेषण", "h", kind="analysis")])


def pk_answer(hooks, titles):
    return FakeClient({"submit_packaging": {"hooks": hooks, "titles": titles, "thumb_texts": ["बजट पर घमासान", "सरकार बनाम विपक्ष"]}})


def test_best_safe_hook_and_title_are_chosen_and_invented_numbers_rejected():
    c = pk_answer(
        [{"text": "संसद में बजट पर 99 प्रतिशत कटौती का ऐलान", "score": 9},          # invented number -> rejected
         {"text": "बजट पर संसद में घमासान, सरकार ने रखी 15 प्रतिशत वाली बात", "score": 7},
         {"text": "छोटा", "score": 8}],                                            # too short -> rejected
        [{"text": "बजट पर 500 करोड़ का सच", "score": 9},                             # invented number -> rejected
         {"text": "बजट पर घमासान: सरकार बनाम विपक्ष", "score": 8}])
    s = script()
    pk = packaging.improve(c, "m", s, topic())
    assert s.scenes[0].narration.startswith("बजट पर संसद में घमासान")
    assert s.title == "बजट पर घमासान: सरकार बनाम विपक्ष"
    assert pk.thumb_texts == ["बजट पर घमासान", "सरकार बनाम विपक्ष"]
    assert len(pk.hooks) == 3


def test_packaging_failure_never_blocks():
    s = script()
    pk = packaging.improve(FakeClient({}), "m", s, topic())          # raises KeyError inside -> swallowed
    assert pk.chosen_hook == "" and s.title == "पुराना शीर्षक"


def test_pipeline_produces_title_hook_and_three_thumbnails(cfg):
    client = FakeClient({
        "submit_script": SCRIPT,
        "submit_packaging": {"hooks": [{"text": "संसद में आज अहम बहस हुई जिस पर सबकी नज़र रही और बात बढ़ी", "score": 8}],
                             "titles": [{"text": "संसद की बहस में आज क्या हुआ", "score": 8}],
                             "thumb_texts": ["संसद में घमासान", "आज की बहस", "क्या हुआ"]},
        "arrange_photos": lambda kw: {"scenes": []}})
    store = Store(Path(cfg["youtube"]["output_dir"]))
    meta = produce(cfg, manual_topic("संसद", "संसद में आज बहस हुई।"), "short", client, MockTTS(), store,
                   preset="ultrafast", log=lambda *_: None)
    assert meta["title"] == "संसद की बहस में आज क्या हुआ"
    assert meta["hook"].startswith("संसद में आज अहम बहस")
    assert len(meta["thumbnails"]) == 3 and all(Path(t["file"]).exists() for t in meta["thumbnails"])
    assert meta["title_options"] == ["संसद की बहस में आज क्या हुआ"]


# ---------------------------------------------------------------- analytics loop
def seed_history(store, n):
    for i in range(n):
        store.add_history(f"title {i}", f"topic {i}", f"vid{i}", hook=f"hook {i}", format="short")


def test_insights_need_enough_data_then_give_advice(tmp_path):
    store = Store(tmp_path)
    seed_history(store, 6)
    assert learn.prompt_hint(store) == ""                       # nothing synced yet
    fake = lambda ids: {f"vid{i}": {"views": 100 + i * 50, "avg_pct": 40 + i * 5} for i in range(6)}  # noqa: E731
    assert learn.sync(store, fake) == 6
    ins = learn.insights(store)
    assert ins["ready"] and ins["by_format"]["short"]["videos"] == 6
    hint = learn.prompt_hint(store)
    assert "hook 5" in hint.split("avoid")[0] and "hook 0" in hint.split("avoid")[1]
    assert "title 5" in hint


def test_videos_with_too_few_views_are_ignored(tmp_path):
    store = Store(tmp_path)
    seed_history(store, 5)
    learn.sync(store, lambda ids: {i: {"views": 3, "avg_pct": 99} for i in ids})
    assert not learn.insights(store)["ready"]


def test_script_prompt_receives_insights(cfg):
    client = FakeClient({"submit_script": SCRIPT, "arrange_photos": lambda kw: {"scenes": []}})
    store = Store(Path(cfg["youtube"]["output_dir"]))
    seed_history(store, 5)
    learn.sync(store, lambda ids: {i: {"views": 200, "avg_pct": 50} for i in ids})
    produce(cfg, manual_topic("संसद", "तथ्य"), "short", client, MockTTS(), store, preset="ultrafast", log=lambda *_: None)
    prompt = next(kw for n, kw in client.calls if n == "submit_script")["messages"][0]["content"]
    assert "Openings that kept viewers watching longest" in prompt


# ---------------------------------------------------------------- scheduler
def make_sched(cfg, tmp_path, clock, store_meta=None):
    cfg.data["youtube"]["output_dir"] = str(tmp_path / "out")
    cfg.data["schedule"] = {"timezone": "Asia/Kolkata", "produce_at": "05:30", "publish_slots": ["07:30", "12:30"]}
    log = []
    s = Scheduler(cfg, now=lambda: clock[0], produce=lambda: log.append("produce"),
                  publish=lambda m: (log.append(f"publish {m['id']}"), "VID")[1], poll=lambda: None, log=lambda *_: None)
    return s, log


def at(h, m, day=2):
    return datetime(2026, 10, day, h, m, tzinfo=ZoneInfo("Asia/Kolkata"))


def approved_meta(store, rid):
    d = store.run_dir(rid)
    store.save_meta(rid, {"id": rid, "status": "approved", "format": "short", "title": "t", "topic": "x",
                          "video": str(d / "v.mp4"), "thumbnail": "", "issues": [], "video_id": None})


def test_scheduler_runs_each_job_once_and_publishes_one_per_slot(cfg, tmp_path):
    clock = [at(5, 0)]
    s, log = make_sched(cfg, tmp_path, clock)
    approved_meta(s.store, "a"); approved_meta(s.store, "b")
    assert s.tick() == []                                  # too early
    clock[0] = at(5, 31)
    assert s.tick() == ["produce"] and s.tick() == []      # runs once
    clock[0] = at(7, 35)
    assert s.tick() == ["publish:VID"]
    s.store.set_status("a", "published", video_id="VID")   # as publish_one would
    clock[0] = at(7, 40)
    assert s.tick() == []                                  # same slot never repeats
    clock[0] = at(12, 31)
    assert s.tick() == ["publish:VID"]
    assert log == ["produce", "publish a", "publish b"]


def test_scheduler_skips_missed_slots_and_survives_restart(cfg, tmp_path):
    clock = [at(10, 0)]                                    # laptop was off at 05:30 and 07:30
    s, log = make_sched(cfg, tmp_path, clock)
    assert s.tick() == [] and log == []
    clock[0] = at(5, 35, day=3)
    s.tick()
    s2, log2 = make_sched(cfg, tmp_path, clock)            # "restart" with the same state file
    assert s2.tick() == [] and log2 == []


def test_scheduler_never_publishes_blocked_or_over_limit(cfg, tmp_path):
    clock = [at(7, 31)]
    s, log = make_sched(cfg, tmp_path, clock)
    m = {"id": "bad", "status": "approved", "format": "short", "title": "t", "topic": "x", "video": "", "thumbnail": "",
         "issues": [{"level": "block", "msg": "x"}], "video_id": None}
    s.store.run_dir("bad"); s.store.save_meta("bad", m)
    assert s.tick() == [] and log == []                    # blocked video is never auto-published
