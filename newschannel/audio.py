from __future__ import annotations

import random
import subprocess
from pathlib import Path

from .models import SceneAudio

LEAD_IN, TAIL = 0.25, 0.45   # silence before / after each scene's narration


def scene_durations(audios: list[SceneAudio]) -> list[float]:
    return [LEAD_IN + a.duration + TAIL for a in audios]


def build_narration(audios: list[SceneAudio], durations: list[float], offset: float,
                    total: float, out: Path) -> Path:
    """One WAV: [offset silence][scene audio padded to its scene duration]... [pad to total]."""
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for a in audios:
        cmd += ["-i", a.path]
    parts, labels = [], []
    for i, (a, d) in enumerate(zip(audios, durations)):
        ms = int(LEAD_IN * 1000)
        parts.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=mono,adelay={ms}:all=1,apad=whole_dur={d:.3f}[s{i}]")
        labels.append(f"[s{i}]")
    parts.append("".join(labels) + f"concat=n={len(audios)}:v=0:a=1,"
                 f"adelay={int(offset * 1000)}:all=1,apad=whole_dur={total:.3f}[out]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[out]", "-t", f"{total:.3f}", str(out)]
    subprocess.run(cmd, check=True)
    return out


def pick_music(music_dir: Path) -> Path | None:
    if not music_dir.is_dir():
        return None
    files = [p for p in music_dir.iterdir() if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg"}]
    return random.choice(files) if files else None


def mux(video: Path, narration: Path, out: Path, total: float, music: Path | None = None,
        music_volume: float = 0.1, lufs: float = -14) -> Path:
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(video), "-i", str(narration)]
    fade = f"afade=t=out:st={max(0, total - 1.2):.2f}:d=1.2"
    if music:
        cmd += ["-stream_loop", "-1", "-i", str(music)]
        fc = (f"[2:a]volume={music_volume},aresample=44100[m];[1:a]aresample=44100,asplit=2[n1][n2];"
              f"[m][n1]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=450[duck];"
              f"[n2][duck]amix=inputs=2:duration=first:normalize=0,"
              f"loudnorm=I={lufs}:TP=-1.5:LRA=11,{fade}[a]")
    else:
        fc = f"[1:a]aresample=44100,loudnorm=I={lufs}:TP=-1.5:LRA=11,{fade}[a]"
    cmd += ["-filter_complex", fc, "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
            "-b:a", "192k", "-t", f"{total:.3f}", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True)
    return out
