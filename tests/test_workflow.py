import json
import subprocess
from pathlib import Path

import pytest
from conftest import SCRIPT, FakeClient, make_photo

import test_analysis as TA
from newschannel import analysis, workflow as W
from newschannel.library import Library
from newschannel.review import Store
from newschannel.tts import MockTTS


class SpyTTS(MockTTS):
    def __init__(self):
        self.calls = 0

    def synthesize(self, *a, **k):
        self.calls += 1
        return super().synthesize(*a, **k)


def arrange(kw):
    ids = [ln.split("asset_id=")[1].split()[0] for b in kw["messages"][0]["content"] if b["type"] == "text"
           for ln in b["text"].split("\n") if ln.startswith("asset_id=")]
    return {"scenes": [{"scene": i, "photos": [{"asset_id": ids[i % len(ids)]}]} for i in range(4)]}


def news_client():
    return FakeClient({"submit_script": SCRIPT, "arrange_photos": arrange})


def news_wf(cfg, mode):
    for f in Path(cfg["images"]["inbox_dir"]).glob("*"):        # only the photos given to this draft
        f.unlink()
    store = Store(Path(cfg["youtube"]["output_dir"]))
    topic = {"title": "संसद में बहस", "why": "", "stories": [{"id": "m1", "title": "संसद में बहस", "summary": "संसद में आज बहस हुई।",
                                                              "link": "", "source": "manual", "published": 0.0}]}
    wf = W.Workflow.create(store, "news", "संसद में बहस", "abc", "short",
                           {"topic": topic, "format": "short", "language": None, "library_ids": []}, mode)
    inputs = wf.dir / "inputs" / "images"
    inputs.mkdir(parents=True)
    for i, (w, h) in enumerate([(1600, 1000), (900, 1400), (1800, 900)]):
        make_photo(inputs / f"p{i}.jpg", w, h, 30 + i)
    return wf


def advance(cfg, client, tts, wf):
    return W.advance(cfg, client, tts, wf, log=lambda *_: None, preset="ultrafast")


def test_manual_steps_stop_for_approval_and_spend_nothing_on_voice(cfg):
    wf, tts, client = news_wf(cfg, {"script": "manual", "photos": "manual"}), SpyTTS(), news_client()
    assert advance(cfg, client, tts, wf)["status"] == "script_review"
    assert (wf.dir / "script.json").exists() and not (wf.dir / "media.json").exists() and tts.calls == 0
    view = W.describe_script(wf)
    assert view["title"] == SCRIPT["title"] and len(view["scenes"]) == 4 and view["scenes"][0]["narration"]
    # edit, then ask the AI to revise
    res = W.save_script_edits(cfg, wf, {"title": "मेरा शीर्षक", "scenes": [{"headline": "नई हेडलाइन", "narration": "यह मेरा लिखा नया पहला वाक्य है और बाकी वैसा ही रहेगा"}]})
    assert res["violations"] == [] and wf.script().scenes[0].headline == "नई हेडलाइन" and wf.script().title == "मेरा शीर्षक"
    W.stage_script(cfg, client, wf, lambda *_: None, "make the opening shorter")
    prompt = [kw for n, kw in client.calls if n == "submit_script"][-1]["messages"][0]["content"]
    assert "EDITOR'S INSTRUCTION" in prompt and "make the opening shorter" in prompt and "यह मेरा लिखा नया पहला वाक्य" in prompt
    assert wf.state["status"] == "script_review"
    W.save_script_edits(cfg, wf, {"title": "मेरा शीर्षक"})                 # an AI revision replaces the draft; edit again afterwards
    # approve script -> photos stage, still no voice
    assert W.approve_script(cfg, client, tts, wf, lambda *_: None, preset="ultrafast")["status"] == "media_review"
    media = W.describe_media(wf)
    assert len(media["assets"]) == 3 and len(media["plan"]) == 4 and all(media["plan"][i] for i in range(4)) and tts.calls == 0
    # edit the plan: scene 0 gets two photos, scene 1 none (-> repaired to keep the video gap-free), music off
    ids = [a["id"] for a in media["assets"]]
    W.save_plan(wf, [[{"asset_id": ids[2], "motion": "pan_left"}, {"asset_id": ids[1]}], [], [{"asset_id": ids[0]}], [{"asset_id": "bogus"}]], "off")
    saved = W.describe_media(wf)
    assert [s["asset_id"] for s in saved["plan"][0]] == [ids[2], ids[1]] and saved["plan"][0][0]["motion"] == "pan_left"
    assert saved["plan"][1] == [] and saved["plan"][3] == [] and saved["music"] == "off"
    W.approve_media(cfg, client, tts, wf, lambda *_: None, "ultrafast")
    meta = wf.store.meta(wf.id)
    assert wf.state["status"] == "done" and Path(meta["video"]).exists() and tts.calls == 4 and meta["title"] == "मेरा शीर्षक"
    plan = json.loads((wf.dir / "plan.json").read_text())
    assert [s["asset_id"] for s in plan if s["scene"] == 0][0] == ids[2]            # the video follows YOUR arrangement
    assert {s["scene"] for s in plan} == {0, 1, 2, 3}                                  # empty scenes were repaired, no gaps
    assert not (wf.dir / "bed.wav").exists()                                           # music: off respected
    assert W.list_drafts(wf.store) == []                                               # finished drafts leave the list


@pytest.mark.parametrize("mode,stops", [({"script": "auto", "photos": "manual"}, "media_review"),
                                        ({"script": "manual", "photos": "auto"}, "script_review"),
                                        ({"script": "auto", "photos": "auto"}, "done")])
def test_each_step_can_be_manual_or_auto(cfg, mode, stops):
    wf, tts, client = news_wf(cfg, mode), SpyTTS(), news_client()
    assert advance(cfg, client, tts, wf)["status"] == stops
    if stops == "script_review":                      # approving it then runs everything else automatically
        assert W.approve_script(cfg, client, tts, wf, lambda *_: None, preset="ultrafast")["status"] == "done"
    if stops == "media_review":
        assert tts.calls == 0
        assert W.approve_media(cfg, client, tts, wf, lambda *_: None, "ultrafast")["status"] == "done"


def test_approve_and_auto_run_the_rest(cfg):
    wf, tts, client = news_wf(cfg, {"script": "manual", "photos": "manual"}), SpyTTS(), news_client()
    advance(cfg, client, tts, wf)
    st = W.approve_script(cfg, client, tts, wf, lambda *_: None, auto_rest=True, preset="ultrafast")
    assert st["status"] == "done" and st["mode"]["photos"] == "auto"


def test_steps_cannot_be_approved_out_of_order(cfg):
    wf, tts, client = news_wf(cfg, {"script": "manual", "photos": "manual"}), SpyTTS(), news_client()
    advance(cfg, client, tts, wf)
    with pytest.raises(W.Blocked):
        W.approve_media(cfg, client, tts, wf, lambda *_: None)
    W.approve_script(cfg, client, tts, wf, lambda *_: None)
    with pytest.raises(W.Blocked):
        W.approve_script(cfg, client, tts, wf, lambda *_: None)


def test_adding_photos_and_going_back_to_the_script(cfg, tmp_path):
    wf, tts, client = news_wf(cfg, {"script": "manual", "photos": "manual"}), SpyTTS(), news_client()
    advance(cfg, client, tts, wf)
    W.approve_script(cfg, client, tts, wf, lambda *_: None)
    lib = Library(Path(cfg["images"]["library_dir"]))
    pic = tmp_path / "new.jpg"
    make_photo(pic, 1500, 900, 77)
    e, _ = lib.add_file(pic, caption="Sansad", credit="Photo: PIB")
    up = tmp_path / "up.jpg"
    make_photo(up, 1500, 900, 88)
    assert W.add_assets(cfg, wf, [e.id, "missing"], [up, tmp_path / "notes.txt"]) == 2
    assets = W.describe_media(wf)["assets"]
    assert len(assets) == 5 and any(a["credit"] == "Photo: PIB" for a in assets)
    assert W.thumb_path(wf, assets[0]["id"]).exists() and W.thumb_path(wf, "nope") is None
    W.replan(cfg, client, wf, lambda *_: None)
    W.reopen_script(wf)
    assert wf.state["status"] == "script_review" and not (wf.dir / "media.json").exists()


def test_a_failed_render_returns_to_the_review_screen_without_losing_work(cfg):
    class BrokenTTS(MockTTS):
        def synthesize(self, *a, **k):
            raise RuntimeError("voice service is down")
    wf, client = news_wf(cfg, {"script": "manual", "photos": "manual"}), news_client()
    advance(cfg, client, SpyTTS(), wf)
    W.approve_script(cfg, client, SpyTTS(), wf, lambda *_: None)
    with pytest.raises(RuntimeError):
        W.approve_media(cfg, client, BrokenTTS(), wf, lambda *_: None)
    st = wf.state
    assert st["status"] == "media_review" and "voice service is down" in st["error"] and (wf.dir / "media.json").exists()
    assert W.approve_media(cfg, client, SpyTTS(), wf, lambda *_: None, "ultrafast")["status"] == "done"      # retry just works


def test_clips_are_prepared_once_and_reused(cfg, tmp_path):
    wf, tts, client = news_wf(cfg, {"script": "manual", "photos": "manual"}), SpyTTS(), None
    cdir = wf.dir / "inputs" / "clips"
    cdir.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=5", "-f", "lavfi", "-i",
                    "sine=duration=5", "-shortest", "-pix_fmt", "yuv420p", str(cdir / "s.mp4")], check=True)
    (cdir / "clips.txt").write_text("s.mp4: 0:01-0:04 | Sansad TV | भाषण\n", encoding="utf-8")
    sc = json.loads(json.dumps(SCRIPT))
    sc["scenes"].insert(2, {"narration": "", "headline": "भाषण", "kind": "clip", "clip_id": 0})
    client = FakeClient({"submit_script": sc, "arrange_photos": arrange})
    advance(cfg, client, tts, wf)
    assert (wf.dir / "clips.json").exists() and len(wf.clips()) == 1 and wf.clips()[0].credit == "Sansad TV"
    W.approve_script(cfg, client, tts, wf, lambda *_: None)
    plan = W.describe_media(wf)["plan"]
    assert plan[2] == []                                                                # the clip scene gets no photos
    W.approve_media(cfg, client, tts, wf, lambda *_: None, "ultrafast")
    meta = wf.store.meta(wf.id)
    assert meta["clips"][0]["credit"] == "Sansad TV" and Path(meta["video"]).exists()


# ------------------------------------------------------------------ deep analysis
def analysis_wf(cfg, tmp_path, scenes_edit=None):
    cfg.data["content"]["language"] = "hinglish"
    cfg.data["formats"]["analysis"].update(width=640, height=360, fps=12, max_scenes=40)
    cfg.data["endscreen"] = {"enabled": True, "seconds": 4}
    cfg.data["analysis"]["min_minutes"] = 0.2
    store = Store(Path(cfg["youtube"]["output_dir"]))
    client = TA.client_for_research("critical", 0.2)
    res = analysis.run_research(cfg, client, store, "MSP hike", urls=list(TA.TEXTS), fetch=TA.fake_fetch, log=lambda *_: None)
    led = analysis.load_research(store, res["research_id"])[0]
    scenes = TA.good_scenes(led)
    if scenes_edit:
        scenes_edit(scenes)
    client.answers["submit_analysis"] = {"title": "MSP hike: kya sach hai?", "description": "d", "tags": ["msp"], "scenes": scenes}
    client.answers["arrange_photos"] = lambda kw: {"scenes": []}
    wf = W.Workflow.create(store, "analysis", "MSP hike", "analysis", "analysis",
                           {"research_id": res["research_id"], "stance": "auto", "minutes": str(TA.minutes_for(TA.good_scenes(led))),
                            "language": "hinglish", "music": "calm", "library_ids": []}, {"script": "manual", "photos": "manual"})
    return wf, client, led


def test_deep_script_is_reviewed_edited_rechecked_then_approved(cfg, tmp_path):
    def break_it(scenes):
        scenes[1]["beats"][0]["text"] = "Cabinet ne wheat ka MSP 18 percent badhaya."
    wf, client, led = analysis_wf(cfg, tmp_path, break_it)
    tts = SpyTTS()
    assert advance(cfg, client, tts, wf)["status"] == "script_review"
    assert wf.params()["stance"] == "critical"                                         # recommendation was resolved and saved
    view = W.describe_script(wf)
    assert view["scenes"][1]["beats"][0]["claim_ids"] and view["stance"] == "critical"
    assert any("18" in v for v in wf.check()["violations"])
    with pytest.raises(W.Blocked):                                                     # cannot approve a script that fails the fact check
        W.approve_script(cfg, client, tts, wf, lambda *_: None)
    res = W.save_script_edits(cfg, wf, {"scenes": [{}, {"beats": [{"text": "Cabinet ne wheat ka MSP 15 percent badhaya."}]}]})
    assert res["violations"] == [] and wf.check()["violations"] == []                 # fixed by hand, re-checked automatically
    res2 = W.save_script_edits(cfg, wf, {"scenes": [{}, {"beats": [{"text": "Sarkar ghabra gayi thi, yeh saaf hai."}]}]})
    assert any("banned wording" in v for v in res2["violations"])                      # edits are held to the same rules
    W.save_script_edits(cfg, wf, {"scenes": [{}, {"beats": [{"text": "Cabinet ne wheat ka MSP 15 percent badhaya."}]}]})
    assert W.approve_script(cfg, client, tts, wf, lambda *_: None, preset="ultrafast")["status"] == "media_review"
    W.approve_media(cfg, client, tts, wf, lambda *_: None, "ultrafast")
    meta = wf.store.meta(wf.id)
    assert meta["format"] == "analysis" and not [i for i in meta["issues"] if i["level"] == "block"]
    assert (wf.dir / "bed.wav").exists()                                               # music choice from the form was used


def test_ai_revision_of_a_deep_script_is_included_in_the_prompt(cfg, tmp_path):
    wf, client, led = analysis_wf(cfg, tmp_path)
    advance(cfg, client, SpyTTS(), wf)
    W.stage_script(cfg, client, wf, lambda *_: None, "make the analysis section sharper")
    prompt = [kw for n, kw in client.calls if n == "submit_analysis"][-1]["messages"][0]["content"]
    assert "CURRENT DRAFT" in prompt and "make the analysis section sharper" in prompt and "MSP" in prompt


def test_length_is_advice_during_review_but_facts_stay_blocking(cfg, tmp_path):
    wf, client, led = analysis_wf(cfg, tmp_path)
    tts = SpyTTS()
    cfg.data["analysis"]["min_minutes"] = 8.0                  # the draft is far shorter than a 8-minute target
    wf.save(params={**wf.params(), "minutes": "8"})
    advance(cfg, client, tts, wf)
    chk = wf.check()
    assert chk["violations"] == [] and any(w.startswith("Narration is") for w in chk["warnings"])
    # a person shortening or lengthening the text is never blocked by the length target ...
    res = W.save_script_edits(cfg, wf, {"scenes": [{}, {"beats": [{"text": "Cabinet ne wheat ka MSP 15 percent badhaya."}]}]})
    assert res["violations"] == [] and any(w.startswith("Narration is") for w in res["warnings"])
    # ... but an invented number still is
    bad = W.save_script_edits(cfg, wf, {"scenes": [{}, {"beats": [{"text": "Cabinet ne wheat ka MSP 19 percent badhaya."}]}]})
    assert any("19" in v for v in bad["violations"]) and not any(v.startswith("Narration is") for v in bad["violations"])
