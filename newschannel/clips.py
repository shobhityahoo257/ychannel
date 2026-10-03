"""Original video clips (e.g. a politician's speech) supplied by the channel owner.

The tool never downloads copyrighted footage. You give it files (Sansad TV, PIB, your own
recordings...); it trims an excerpt, normalises loudness, transcribes the speech, writes Hindi
subtitles (machine-translated when needed), and credits the source on screen and in the description.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from . import stt
from .llm import call_tool
from .tts import probe_duration

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


@dataclass
class SubLine:
    start: float   # seconds relative to the trimmed clip
    end: float
    text: str


@dataclass
class Clip:
    src: str
    start: float
    end: float
    credit: str = ""          # e.g. "Sansad TV" - required before publishing
    note: str = ""            # what the clip shows (context for the script writer)
    video: str = ""           # trimmed, video-only copy
    wav: str = ""             # trimmed, loudness-normalised audio
    language: str = ""
    transcript: str = ""      # Hindi text of what is said (for the script writer)
    subs: list[SubLine] = field(default_factory=list)
    machine_translated: bool = False
    requested_seconds: float = 0.0   # what you asked for, before the length cap

    @property
    def duration(self) -> float:
        return self.end - self.start


def parse_time(s: str) -> float:
    parts = [float(x) for x in s.strip().split(":")]
    out = 0.0
    for p in parts:
        out = out * 60 + p
    return out


def read_spec(folder: Path) -> dict[str, dict[str, Any]]:
    """`clips.txt` lines: `file.mp4: 00:12-00:38 | Sansad TV | note about the clip`."""
    spec: dict[str, dict[str, Any]] = {}
    f = folder / "clips.txt"
    if not f.exists():
        return spec
    for line in f.read_text(encoding="utf-8").splitlines():
        if ":" not in line or line.strip().startswith("#"):
            continue
        name, rest = line.split(":", 1)
        fields = [x.strip() for x in rest.split("|")]
        d: dict[str, Any] = {}
        m = re.match(r"^([\d:.]+)\s*-\s*([\d:.]+)$", fields[0]) if fields else None
        if m:
            d["start"], d["end"] = parse_time(m.group(1)), parse_time(m.group(2))
            fields = fields[1:]
        elif fields and fields[0] == "":          # no time range given: `file: | credit | note`
            fields = fields[1:]
        if fields:
            d["credit"] = fields[0]
        if len(fields) > 1:
            d["note"] = fields[1]
        spec[name.strip().lower()] = d
    return spec


def discover(folders: list[Path], max_seconds: float) -> list[Clip]:
    clips: list[Clip] = []
    for folder in folders:
        if not folder.is_dir():
            continue
        spec = read_spec(folder)
        for p in sorted(folder.iterdir()):
            if p.suffix.lower() not in VIDEO_EXTS:
                continue
            d = spec.get(p.name.lower(), {})
            total = probe_duration(p)
            start = min(d.get("start", 0.0), max(0.0, total - 1))
            end = min(d.get("end", start + max_seconds), total)
            requested = end - start
            end = min(end, start + max_seconds)          # cap: excerpts support commentary, not replace it
            clips.append(Clip(str(p), start, end, d.get("credit", ""), d.get("note", p.stem),
                              requested_seconds=requested))
    return clips


def _has_audio(path: str) -> bool:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                          "stream=index", "-of", "csv=p=0", path], capture_output=True, text=True)
    return bool(out.stdout.strip())


def prepare(clip: Clip, folder: Path, idx: int, fps: int) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    clip.video = str(folder / f"clip_{idx}.mp4")
    clip.wav = str(folder / f"clip_{idx}.wav")
    d = f"{clip.duration:.3f}"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{clip.start:.3f}", "-i", clip.src, "-t", d,
                    "-an", "-vf", f"fps={fps}", "-c:v", "libx264", "-crf", "16", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", clip.video], check=True)
    if _has_audio(clip.src):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{clip.start:.3f}", "-i", clip.src, "-t", d,
                        "-vn", "-ac", "1", "-ar", "44100", "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                        clip.wav], check=True)
    else:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=mono", "-t", d, clip.wav], check=True)


# ----------------------------------------------------------------------- speech -> subtitles
def group_words(words: list[dict], max_chars: int = 42) -> list[SubLine]:
    lines: list[SubLine] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        cur.append(w)
        text = " ".join(x["text"] for x in cur)
        gap = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else 9
        if len(text) >= max_chars or text[-1:] in "।?!.," and len(text) > 14 or gap > 0.8:
            lines.append(SubLine(cur[0]["start"], cur[-1]["end"] + 0.1, text))
            cur = []
    if cur:
        lines.append(SubLine(cur[0]["start"], cur[-1]["end"] + 0.1, " ".join(x["text"] for x in cur)))
    return lines


TRANSLATE_SYSTEM = """Translate each numbered subtitle line into {target}.
Be strictly faithful: do not add, soften, sharpen or explain anything. Keep names and numbers exact.
Return exactly one Hindi line per input line, in the same order."""
TRANSLATE_SCHEMA = {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
                    "required": ["lines"]}


def build_subtitles(clip: Clip, client: Any, model: str, provider: str, lang: str = "hinglish") -> None:
    """Fill clip.subs / clip.transcript. Silent no-op in mock mode."""
    if provider == "mock":
        return
    lang, words = stt.transcribe(clip.wav, provider)
    clip.language = lang
    subs = group_words(words)
    if subs and not stt.is_hindi(lang) and client is not None:
        target = ("natural spoken Hinglish in Roman script (Hindi grammar with common English words)"
                  if lang == "hinglish" else "natural, simple Hindi (Devanagari)")
        res = call_tool(client, model, TRANSLATE_SYSTEM.format(target=target),
                        "\n".join(f"{i}. {s.text}" for i, s in enumerate(subs)), "submit_translation",
                        TRANSLATE_SCHEMA)
        out = res["lines"]
        if len(out) == len(subs):
            for s, t in zip(subs, out):
                s.text = t.strip()
            clip.machine_translated = True
    clip.subs = subs
    clip.transcript = " ".join(s.text for s in subs)


def load(cfg: Config, folders: list[Path], run: Path, client: Any, fps: int) -> list[Clip]:
    c = cfg.get("clips", {})
    clips = discover(folders, c.get("max_seconds", 30))
    for i, clip in enumerate(clips):
        prepare(clip, run / "clips", i, fps)
        build_subtitles(clip, client, cfg.models()[0], cfg.tts_provider(), cfg.lang)
    return clips


def save_clips(clips: list[Clip], path: Path) -> None:
    import json
    from dataclasses import asdict
    path.write_text(json.dumps([asdict(c) for c in clips], ensure_ascii=False, indent=1), encoding="utf-8")


def load_saved(path: Path) -> list[Clip]:
    import json
    out = []
    for d in json.loads(path.read_text(encoding="utf-8")):
        d["subs"] = [SubLine(**x) for x in d.get("subs", [])]
        out.append(Clip(**d))
    return out
