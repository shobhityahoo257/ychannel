import json
import subprocess
from types import SimpleNamespace

import pytest

from newschannel import llm, stt, tts
from newschannel.config import Config


class Resp:
    def __init__(self, status=200, js=None, content=b""):
        self.status_code, self._js, self.content, self.text = status, js, content, json.dumps(js)[:200] if js else ""

    def json(self):
        return self._js


def test_provider_auto_selection(monkeypatch):
    c = Config.load()
    for k in ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ELEVENLABS_API_KEY"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert (c.llm_provider(), c.tts_provider()) == ("openai", "openai")
    assert c.models() == ("gpt-4o", "gpt-4o")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "e")
    assert (c.llm_provider(), c.tts_provider()) == ("anthropic", "elevenlabs")
    assert c.models()[0].startswith("claude")


def test_openai_tool_call_with_images(monkeypatch):
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["body"] = url, kw["json"]
        args = json.dumps({"scenes": [{"scene": 0, "photos": [{"asset_id": "x"}]}]})
        return Resp(js={"choices": [{"message": {"tool_calls": [{"function": {"arguments": args}}]}}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    client = llm.OpenAIClient("sk")
    content = [{"type": "text", "text": "hello"},
               {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "QUJD"}}]
    out = llm.call_tool(client, "gpt-4o", "sys", content, "arrange_photos", {"type": "object"})
    assert out["scenes"][0]["photos"][0]["asset_id"] == "x"
    body = seen["body"]
    assert body["tool_choice"]["function"]["name"] == "arrange_photos"
    parts = body["messages"][1]["content"]
    assert parts[1]["image_url"]["url"] == "data:image/jpeg;base64,QUJD"


def test_openai_error_is_reported(monkeypatch):
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Resp(401, {"error": "bad key"}))
    with pytest.raises(RuntimeError, match="OpenAI error 401"):
        llm.OpenAIClient("x").tool_call("m", "s", "hi", "t", {}, 10)


def test_openai_tts_and_word_alignment(monkeypatch, tmp_path):
    mp3 = tmp_path / "src.mp3"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
                    "-c:a", "libmp3lame", str(mp3)], check=True)
    calls = []

    def fake_post(url, **kw):
        calls.append(url)
        if url.endswith("/audio/speech"):
            assert kw["json"]["model"] == "gpt-4o-mini-tts" and "instructions" in kw["json"]
            return Resp(content=mp3.read_bytes())
        words = [{"word": " संसद", "start": 0.2, "end": 0.7}, {"word": "में", "start": 0.8, "end": 1.0},
                 {"word": "बहस", "start": 1.1, "end": 1.7}]
        return Resp(js={"language": "hindi", "words": words})

    monkeypatch.setattr(tts.requests, "post", fake_post)
    monkeypatch.setattr(stt.requests, "post", fake_post)
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    c = Config.load()
    c.data["tts"]["provider"] = "openai"
    audio = tts.make_tts(c).synthesize("संसद में बहस", tmp_path / "o.mp3")
    assert [w.text for w in audio.words] == ["संसद", "में", "बहस"]
    assert audio.words[0].start == 0.2 and abs(audio.duration - 2.0) < 0.2


def test_align_words_fallback_spreads_by_length():
    heard = [{"text": "x", "start": 1.0, "end": 2.0}]            # count mismatch
    w = tts.align_words(["aa", "bbbb"], heard, 3.0)
    assert w[0].start == 1.0 and w[1].start > w[0].end and w[1].end <= 2.0


def test_whisper_language_names_normalised(monkeypatch, tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"x")
    monkeypatch.setenv("OPENAI_API_KEY", "sk")
    monkeypatch.setattr(stt.requests, "post", lambda *a, **k: Resp(js={"language": "english", "words": [
        {"word": "Hello", "start": 0, "end": 0.4}]}))
    lang, words = stt.transcribe(str(f), "openai")
    assert lang == "en" and words[0]["text"] == "Hello" and not stt.is_hindi(lang) and stt.is_hindi("hindi")
