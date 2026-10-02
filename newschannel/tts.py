from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import requests

from .config import Config
from .models import SceneAudio, Word

_REPL = [("%", " प्रतिशत"), ("₹", " रुपये "), ("&", " और "), ("PM", "पीएम"), ("CM", "सीएम"),
         ("BJP", "बीजेपी"), ("NDA", "एनडीए"), ("ECI", "चुनाव आयोग")]


def normalize(text: str) -> str:
    for a, b in _REPL:
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def probe_duration(path: str | Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def words_from_alignment(alignment: dict) -> list[Word]:
    chars = alignment["characters"]
    starts = alignment["character_start_times_seconds"]
    ends = alignment["character_end_times_seconds"]
    words: list[Word] = []
    cur, ws, we = "", 0.0, 0.0
    for ch, s, e in zip(chars, starts, ends):
        if ch.isspace():
            if cur:
                words.append(Word(cur, ws, we))
                cur = ""
            continue
        if not cur:
            ws = s
        cur += ch
        we = e
    if cur:
        words.append(Word(cur, ws, we))
    return words


class ElevenLabsTTS:
    URL = "https://api.elevenlabs.io/v1/text-to-speech/{voice}/with-timestamps"

    def __init__(self, cfg: Config):
        t = cfg["tts"]
        self.key = Config.env("ELEVENLABS_API_KEY", required=True)
        self.voice = Config.env("ELEVENLABS_VOICE_ID", required=True)
        self.settings = {"stability": t["stability"], "similarity_boost": t["similarity_boost"],
                         "style": t.get("style", 0.0), "speed": t.get("speed", 1.0),
                         "use_speaker_boost": True}
        self.model = t["model_id"]

    def synthesize(self, text: str, out: Path, prev: str = "", nxt: str = "") -> SceneAudio:
        body = {"text": normalize(text), "model_id": self.model, "voice_settings": self.settings,
                "language_code": "hi"}
        if prev:
            body["previous_text"] = prev
        if nxt:
            body["next_text"] = nxt
        r = requests.post(self.URL.format(voice=self.voice), json=body, timeout=120,
                          params={"output_format": "mp3_44100_128"},
                          headers={"xi-api-key": self.key})
        if r.status_code != 200:
            raise RuntimeError(f"ElevenLabs error {r.status_code}: {r.text[:300]}")
        data = r.json()
        out.write_bytes(base64.b64decode(data["audio_base64"]))
        words = words_from_alignment(data.get("normalized_alignment") or data["alignment"])
        # captions must show the original script words, not the normalized spelling
        orig = text.split()
        if len(orig) == len(words):
            for w, o in zip(words, orig):
                w.text = o
        return SceneAudio(str(out), probe_duration(out), words)


class MockTTS:
    """Silent audio with evenly spaced word timings; for tests and dry runs."""

    def synthesize(self, text: str, out: Path, prev: str = "", nxt: str = "") -> SceneAudio:
        toks = text.split()
        dur = max(1.0, len(toks) / 2.4)
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency=220:duration={dur:.3f}", "-af", "volume=0.05",
                        "-c:a", "libmp3lame", str(out)], check=True)
        step = dur / max(1, len(toks))
        words = [Word(w, i * step, (i + 1) * step - 0.03) for i, w in enumerate(toks)]
        return SceneAudio(str(out), probe_duration(out), words)


def make_tts(cfg: Config):
    return MockTTS() if cfg["tts"]["provider"] == "mock" else ElevenLabsTTS(cfg)


def synthesize_script(tts, scenes, folder: Path) -> list[SceneAudio]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, sc in enumerate(scenes):
        prev = scenes[i - 1].narration[-200:] if i else ""
        nxt = scenes[i + 1].narration[:200] if i + 1 < len(scenes) else ""
        out.append(tts.synthesize(sc.narration, folder / f"scene_{i:02d}.mp3", prev, nxt))
    return out
