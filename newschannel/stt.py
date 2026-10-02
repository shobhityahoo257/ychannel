"""Speech-to-text with word timings: ElevenLabs Scribe or OpenAI Whisper."""
from __future__ import annotations

import requests

from .config import Config

HINDI = {"hi", "hin", "hindi"}
_LANG = {"english": "en", "hindi": "hi"}


def is_hindi(lang: str) -> bool:
    return lang.lower() in HINDI


def elevenlabs_words(path: str) -> tuple[str, list[dict]]:
    key = Config.env("ELEVENLABS_API_KEY", required=True)
    with open(path, "rb") as f:
        r = requests.post("https://api.elevenlabs.io/v1/speech-to-text", headers={"xi-api-key": key},
                          data={"model_id": "scribe_v1", "timestamps_granularity": "word"},
                          files={"file": f}, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"Speech-to-text error {r.status_code}: {r.text[:300]}")
    j = r.json()
    return j.get("language_code", ""), [w for w in j.get("words", []) if w.get("type", "word") == "word"]


def openai_words(path: str, language: str | None = None) -> tuple[str, list[dict]]:
    key = Config.env("OPENAI_API_KEY", required=True)
    data = {"model": "whisper-1", "response_format": "verbose_json", "timestamp_granularities[]": "word"}
    if language:
        data["language"] = language
    with open(path, "rb") as f:
        r = requests.post("https://api.openai.com/v1/audio/transcriptions",
                          headers={"Authorization": f"Bearer {key}"}, data=data, files={"file": f}, timeout=300)
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI transcription error {r.status_code}: {r.text[:300]}")
    j = r.json()
    lang = j.get("language", "")
    return _LANG.get(lang.lower(), lang), [{"text": w["word"].strip(), "start": w["start"], "end": w["end"]}
                                           for w in j.get("words", [])]


def transcribe(path: str, provider: str) -> tuple[str, list[dict]]:
    return openai_words(path) if provider == "openai" else elevenlabs_words(path)
