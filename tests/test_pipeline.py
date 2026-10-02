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


def make_clip(path, seconds=6, size="640x360"):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


def clip_script():
    sc = json.loads(json.dumps(SCRIPT))
    sc["scenes"].insert(2, {"narration": "", "headline": "संसद में भाषण", "kind": "clip", "clip_id": 0})
    return sc


def run_with_clip(cfg, tmp, spec):
    folder = tmp / "myclips"
    folder.mkdir()
    make_clip(folder / "speech.mp4")
    (folder / "clips.txt").write_text(spec, encoding="utf-8")
    client = FakeClient({"submit_script": clip_script(), "arrange_photos": arrange})
    store = Store(Path(cfg["youtube"]["output_dir"]))
    meta = produce(cfg, manual_topic("संसद में बहस", "संसद में आज बहस हुई।"), "short", client, MockTTS(), store,
                   preset="ultrafast", log=lambda *_: None, clip_folders=[folder])
    return meta, client


def test_clip_is_trimmed_credited_and_audio_kept(cfg, tmp_path):
    meta, client = run_with_clip(cfg, tmp_path, "speech.mp4: 00:01-00:05 | Sansad TV | लोकसभा में भाषण\n")
    assert meta["clips"][0]["seconds"] == 4.0
    assert not [i for i in meta["issues"] if i["level"] == "block"]
    assert "Sansad TV" in meta["description"] and "वीडियो अंश" in meta["description"]
    script_call = next(kw for n, kw in client.calls if n == "submit_script")
    assert "clip_id=0" in script_call["messages"][0]["content"]
    info = probe(meta["video"])
    assert abs(float(info["format"]["duration"]) - meta["duration"]) < 0.5
    run_dir = Path(meta["video"]).parent
    plan = json.loads((run_dir / "plan.json").read_text())
    assert 2 not in {s["scene"] for s in plan}          # clip scene gets no photos
    # grab a frame from the middle of the clip scene for eyeballing
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "8", "-i", meta["video"], "-frames:v", "1",
                    str(run_dir / "clipframe.png")], check=True)


def test_clip_without_credit_is_blocked(cfg, tmp_path):
    meta, _ = run_with_clip(cfg, tmp_path, "speech.mp4: 00:00-00:05\n")
    assert any(i["level"] == "block" and "no source credit" in i["msg"] for i in meta["issues"])


def test_clip_length_is_capped(cfg, tmp_path):
    cfg.data["clips"]["max_seconds"] = 3
    meta, _ = run_with_clip(cfg, tmp_path, "speech.mp4: 00:00-00:06 | PIB | note\n")
    assert meta["clips"][0]["seconds"] == 3.0
    assert any("trimmed" in i["msg"] for i in meta["issues"])
