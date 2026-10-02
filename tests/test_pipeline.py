import json
import subprocess
from pathlib import Path

from conftest import SCRIPT, FakeClient
from newschannel.pipeline import manual_topic, produce
from newschannel.review import Store
from newschannel.tts import MockTTS


def arrange(kw):
    ids = [p.split("asset_id=")[1].split()[0] for b in kw["messages"][0]["content"]
           if b["type"] == "text" for p in b["text"].split("\n") if p.startswith("asset_id=")]
    return {"scenes": [{"scene": i, "photos": [{"asset_id": ids[i % len(ids)], "focus_x": 0.5, "focus_y": 0.4,
                                                 "motion": "zoom_in"}]} for i in range(3)]}  # scene 3 skipped on purpose


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height:format=duration",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def run(cfg, fmt):
    client = FakeClient({"submit_script": SCRIPT, "arrange_photos": arrange})
    store = Store(Path(cfg["youtube"]["output_dir"]))
    topic = manual_topic("संसद में बहस", "संसद में आज बहस हुई।")
    meta = produce(cfg, topic, fmt, client, MockTTS(), store, preset="ultrafast", log=lambda *_: None)
    return meta, client, store


def test_short_video_end_to_end(cfg):
    meta, client, store = run(cfg, "short")
    info = probe(meta["video"])
    types = {s["codec_type"] for s in info["streams"]}
    assert types == {"video", "audio"}
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (360, 640)
    assert abs(float(info["format"]["duration"]) - meta["duration"]) < 0.5
    assert Path(meta["thumbnail"]).exists()
    assert meta["status"] == "pending"
    assert store.meta(meta["id"])["title"] == SCRIPT["title"]


def test_user_images_are_used_and_small_rejected(cfg):
    meta, client, store = run(cfg, "short")
    run_dir = Path(meta["video"]).parent
    plan = json.loads((run_dir / "plan.json").read_text())
    imgs = list((run_dir / "images").glob("user_*.jpg"))
    assert len(imgs) == 3                      # tiny.jpg rejected
    assert len(plan) >= 4                      # the skipped scene was repaired with a photo
    assert {s["scene"] for s in plan} == {0, 1, 2, 3}
    vision_call = next(kw for name, kw in client.calls if name == "arrange_photos")
    assert "crowd at a political rally" in json.dumps(vision_call["messages"][0]["content"], ensure_ascii=False)


def test_long_video_has_intro_outro_and_ticker(cfg):
    meta, *_ = run(cfg, "long")
    v = next(s for s in probe(meta["video"])["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (640, 360)
    assert meta["duration"] > 6   # includes 2s intro + 4s outro
