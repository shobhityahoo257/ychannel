from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import requests

from . import stt
from .config import Config
from .i18n import TTS_REPLACE, norm
from .models import SceneAudio, Word

_MONEY = re.compile(r"(US\$|\$|€|£)\s?(\d[\d,]*(?:\.\d+)?)(\s?(?:trillion|billion|million|thousand|lakh crore|crore|lakh))?", re.I)
_CCY = {"US$": "US dollars", "$": "dollars", "€": "euros", "£": "pounds"}


def _speak_money(m: "re.Match") -> str:
    sym = "US$" if m.group(1).upper() == "US$" else m.group(1)
    return f"{m.group(2)}{m.group(3) or ''} {_CCY[sym]}"


def normalize(text: str, lang: str = "hinglish") -> str:
    if norm(lang) == "english":              # "$2.5 trillion" must be spoken "2.5 trillion dollars"
        text = _MONEY.sub(_speak_money, text)
    for a, b in TTS_REPLACE[norm(lang)]:
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
        self.lang = cfg.lang
        self.language_code = "en" if self.lang == "english" else t.get("language_code", "hi")

    def synthesize(self, text: str, out: Path, prev: str = "", nxt: str = "") -> SceneAudio:
        body = {"text": normalize(text, self.lang), "model_id": self.model, "voice_settings": self.settings,
                "language_code": self.language_code}
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


class OpenAITTS:
    """OpenAI speech (handles Hindi). It returns no timings, so Whisper is run on the result to
    time the words for captions; if its word count differs from the script, timings are spread
    across the spoken span in proportion to word length."""

    URL = "https://api.openai.com/v1/audio/speech"

    def __init__(self, cfg: Config):
        t = cfg["tts"]
        self.key = Config.env("OPENAI_API_KEY", required=True)
        self.model = t.get("openai_model", "gpt-4o-mini-tts")
        self.voice = Config.env("OPENAI_VOICE") or t.get("openai_voice", "onyx")
        ins = t.get("openai_instructions", "")
        self.lang = cfg.lang
        self.instructions = ins.get(self.lang, "") if isinstance(ins, dict) else ins
        self.stt_lang = "en" if self.lang == "english" else "hi"
        self.speed = t.get("speed", 1.0)

    def synthesize(self, text: str, out: Path, prev: str = "", nxt: str = "") -> SceneAudio:
        body = {"model": self.model, "voice": self.voice, "input": normalize(text, self.lang),
                "response_format": "mp3", "speed": self.speed}
        if self.instructions and "tts-1" not in self.model:     # `instructions` is unsupported by tts-1
            body["instructions"] = self.instructions
        r = requests.post(self.URL, json=body, headers={"Authorization": f"Bearer {self.key}"}, timeout=180)
        if r.status_code != 200:
            raise RuntimeError(f"OpenAI TTS error {r.status_code}: {r.text[:300]}")
        out.write_bytes(r.content)
        dur = probe_duration(out)
        try:
            _, heard = stt.openai_words(str(out), language=self.stt_lang)
        except Exception:
            heard = []
        return SceneAudio(str(out), dur, align_words(text.split(), heard, dur))


def align_words(script_words: list[str], heard: list[dict], duration: float) -> list[Word]:
    if not script_words:
        return []
    if heard and len(heard) == len(script_words):
        return [Word(o, h["start"], h["end"]) for o, h in zip(script_words, heard)]
    t0 = heard[0]["start"] if heard else 0.0
    t1 = heard[-1]["end"] if heard else duration
    total = sum(len(w) for w in script_words) or 1
    out, cur = [], t0
    for w in script_words:
        span = (t1 - t0) * len(w) / total
        out.append(Word(w, cur, cur + span * 0.92))
        cur += span
    return out


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
    p = cfg.tts_provider()
    return MockTTS() if p == "mock" else OpenAITTS(cfg) if p == "openai" else ElevenLabsTTS(cfg)


def synthesize_script(tts, scenes, folder: Path) -> list[SceneAudio]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for i, sc in enumerate(scenes):
        prev = scenes[i - 1].narration[-200:] if i else ""
        nxt = scenes[i + 1].narration[:200] if i + 1 < len(scenes) else ""
        out.append(tts.synthesize(sc.narration, folder / f"scene_{i:02d}.mp3", prev, nxt))
    return out
