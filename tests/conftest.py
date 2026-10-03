import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from newschannel.config import Config  # noqa: E402


class FakeClient:
    """Stands in for anthropic.Anthropic; answers each forced tool call with canned data."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []
        self.messages = self

    def create(self, **kw):
        name = kw["tool_choice"]["name"]
        self.calls.append((name, kw))
        ans = self.answers[name]
        data = ans(kw) if callable(ans) else ans
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=data)])


def make_photo(path, w, h, seed):
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 200, 3)
    arr = np.zeros((h, w, 3), np.uint8)
    arr[:] = base
    arr[:, :, 0] = np.linspace(base[0], 255, w).astype(np.uint8)[None, :]
    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)
    for gx in range(8):                       # seed-dependent block pattern so each photo has a distinct hash
        for gy in range(8):
            if rng.random() < 0.5:
                d.rectangle((gx * w / 8, gy * h / 8, (gx + 1) * w / 8, (gy + 1) * h / 8),
                            fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
    d.ellipse((w * .35, h * .25, w * .65, h * .75), fill=(240, 200, 160))
    d.rectangle((w * .1, h * .8, w * .9, h * .95), fill=(30, 30, 30))
    im.save(path, quality=90)


@pytest.fixture
def cfg(tmp_path):
    c = Config.load()
    c.data["tts"]["provider"] = "mock"
    c.data.setdefault("content", {})["language"] = "hindi"      # legacy tests assert Hindi (Devanagari) output
    c.data["images"]["inbox_dir"] = str(tmp_path / "inbox")
    c.data["images"]["stock_mode"] = "off"
    c.data["images"]["library_dir"] = str(tmp_path / "library")
    c.data["youtube"]["output_dir"] = str(tmp_path / "out")
    c.data["audio"]["music_dir"] = str(tmp_path / "nomusic")
    c.data["formats"]["short"].update(width=360, height=640, fps=12, target_seconds=12, max_scenes=5)
    c.data["formats"]["long"].update(width=640, height=360, fps=12, target_seconds=24, max_scenes=5)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    make_photo(inbox / "parliament_building.jpg", 1600, 1000, 1)
    make_photo(inbox / "rally_crowd.jpg", 900, 1400, 2)
    make_photo(inbox / "panorama_vote.jpg", 3000, 700, 3)
    make_photo(inbox / "tiny.jpg", 200, 200, 4)           # must be rejected as too small
    (inbox / "captions.txt").write_text("rally_crowd.jpg: crowd at a political rally\n", encoding="utf-8")
    return c


SCRIPT = {
    "title": "संसद में आज क्या हुआ",
    "description": "संसद की कार्यवाही का सार।",
    "tags": ["संसद", "राजनीति"],
    "scenes": [
        {"narration": "संसद में आज अहम बहस हुई जिस पर सबकी नज़र रही", "headline": "संसद में अहम बहस",
         "visual_query": "Indian parliament", "kind": "news"},
        {"narration": "बीबीसी हिंदी के मुताबिक़ कई दलों ने अपनी बात रखी और सदन में चर्चा चली", "headline": "कई दलों ने रखी बात",
         "visual_query": "crowd", "kind": "news"},
        {"narration": "विश्लेषण यह है कि इस बहस का असर आने वाले सत्र पर पड़ सकता है", "headline": "आगे क्या असर",
         "visual_query": "voters", "kind": "analysis"},
        {"narration": "आपकी राय क्या है कमेंट में बताइए और चैनल को सब्सक्राइब कीजिए", "headline": "आपकी राय?",
         "visual_query": "flag", "kind": "outro"},
    ],
}
