"""`python -m newschannel demo` - render a sample video with NO API keys.

Uses a canned Hindi script, silent placeholder audio and generated placeholder pictures, so you
can check that FFmpeg, fonts and the renderer work on your machine before adding real keys."""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
from PIL import Image, ImageDraw

from .config import Config
from .pipeline import manual_topic, produce
from .review import Store
from .tts import MockTTS

SCRIPT = {
    "title": "डेमो: संसद में आज की बहस",
    "description": "यह एक डेमो वीडियो है।",
    "tags": ["डेमो"],
    "scenes": [
        {"narration": "यह एक डेमो वीडियो है जो दिखाता है कि हमारा न्यूज़ चैनल टूल कैसे काम करता है",
         "headline": "डेमो: टूल कैसे काम करता है", "visual_query": "parliament", "kind": "news"},
        {"narration": "असली वीडियो में स्क्रिप्ट क्लाउड लिखता है और आवाज़ एलेवनलैब्स से आती है",
         "headline": "स्क्रिप्ट और आवाज़ ऑटोमैटिक", "visual_query": "newsroom", "kind": "news"},
        {"narration": "आपकी दी हुई तस्वीरें एआई चुनता है और सही क्रम में लगा देता है",
         "headline": "आपकी तस्वीरें, एआई का चुनाव", "visual_query": "photos", "kind": "analysis"},
        {"narration": "अब अपनी चाबियाँ डालिए और पहला असली वीडियो बनाइए", "headline": "अगला कदम: API कुंजियाँ",
         "visual_query": "start", "kind": "outro"},
    ],
}


SCRIPT_HINGLISH = {
    "title": "Demo: Tool kaise kaam karta hai",
    "description": "Yeh ek demo video hai.",
    "tags": ["demo"],
    "scenes": [
        {"narration": "Yeh ek demo video hai jo dikhata hai ki hamara news channel tool kaise kaam karta hai",
         "headline": "Demo: Tool kaise kaam karta hai", "visual_query": "parliament", "kind": "news"},
        {"narration": "Asli video mein script AI likhta hai aur awaaz bhi AI se aati hai",
         "headline": "Script aur awaaz automatic", "visual_query": "newsroom", "kind": "news"},
        {"narration": "Aapki di hui photos AI chunta hai aur sahi order mein laga deta hai",
         "headline": "Aapki photos, AI ka selection", "visual_query": "photos", "kind": "analysis"},
        {"narration": "Ab apni keys daaliye aur pehla asli video banaiye", "headline": "Agla step: API keys",
         "visual_query": "start", "kind": "outro"},
    ],
}


def _photo(path: Path, w: int, h: int, seed: int) -> None:
    rng = np.random.default_rng(seed)
    a, b = rng.integers(30, 220, 3), rng.integers(30, 220, 3)
    t = np.linspace(0, 1, w)[None, :, None]
    arr = (a * (1 - t) + b * t) * np.ones((h, 1, 1))
    im = Image.fromarray(arr.astype(np.uint8))
    d = ImageDraw.Draw(im)
    d.ellipse((w * .3, h * .2, w * .7, h * .8), fill=tuple(int(x) for x in (a + b) / 2 + 40))
    im.save(path, quality=92)


class _Canned:
    def __init__(self, lang: str = "hindi"):
        self.messages = self
        self.script = SCRIPT_HINGLISH if lang == "hinglish" else SCRIPT

    def create(self, **kw):
        name = kw["tool_choice"]["name"]
        if name == "submit_script":
            data = self.script
        else:   # arrange_photos: one photo per scene, in order
            ids = [ln.split("asset_id=")[1].split()[0] for blk in kw["messages"][0]["content"]
                   if blk["type"] == "text" for ln in blk["text"].split("\n") if ln.startswith("asset_id=")]
            data = {"scenes": [{"scene": i, "photos": [{"asset_id": ids[i % len(ids)]}]} for i in range(4)]}
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=data)])


def run(cfg: Config, fmt: str = "short", out_dir: Path | None = None) -> str:
    work = Path(tempfile.mkdtemp(prefix="newschannel-demo-"))
    inbox = work / "inbox"
    inbox.mkdir()
    for i, (w, h) in enumerate([(1600, 1000), (1000, 1500), (1800, 900)]):
        _photo(inbox / f"sample_{i}.jpg", w, h, i)
    cfg.data["images"].update(inbox_dir=str(inbox), stock_mode="off")
    cfg.data["tts"]["provider"] = "mock"
    store = Store(out_dir or work / "out")
    meta = produce(cfg, manual_topic("Demo", "Demo"), fmt, _Canned(cfg.lang), MockTTS(), store, preset="veryfast")
    return meta["video"]
