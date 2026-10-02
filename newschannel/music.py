"""Royalty-free background music, generated locally (no licensing or Content ID risk).

A soft ambient pad built from layered sine tones with slow tremolo and reverb-like echo. If you prefer real
tracks, drop them into assets/music/ and they are used instead (YouTube Audio Library / Pixabay are safe sources)."""
from __future__ import annotations

import subprocess
from pathlib import Path

MOODS = {
    # mood: (frequencies Hz, tremolo rate, tremolo depth, lowpass Hz)
    "calm": ([110.0, 164.81, 220.0, 277.18], 0.17, 0.45, 1100),         # A major: open, steady
    "tension": ([110.0, 116.54, 164.81, 233.08], 0.38, 0.65, 900),     # minor-second cluster: uneasy
    "warm": ([130.81, 196.0, 261.63, 329.63], 0.14, 0.40, 1300),        # C major: hopeful
}


def mood_for(stance: str) -> str:
    return {"critical": "tension", "supportive": "warm"}.get(stance, "calm")


def synth(path: Path, seconds: float, mood: str = "calm") -> Path:
    freqs, trem_f, trem_d, lp = MOODS.get(mood, MOODS["calm"])
    d = f"{seconds + 2:.2f}"
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for f in freqs:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={f}:sample_rate=44100:duration={d}"]
    n = len(freqs)
    inputs = "".join(f"[{i}:a]" for i in range(n))
    fc = (f"{inputs}amix=inputs={n}:normalize=0,tremolo=f={trem_f}:d={trem_d},lowpass=f={lp},"
          f"aecho=0.8:0.6:350|600:0.35|0.25,volume=0.9,afade=t=in:d=3,afade=t=out:st={max(0.0, seconds - 3):.2f}:d=4[out]")
    cmd += ["-filter_complex", fc, "-map", "[out]", "-ac", "2", str(path)]
    subprocess.run(cmd, check=True)
    return path


def swell_expr(times: list[float], boost: float = 0.8, length: float = 1.8) -> str:
    """ffmpeg volume expression: gentle swell of the music at each section change."""
    if not times:
        return "1"
    terms = "+".join(f"between(t\\,{t:.2f}\\,{t + length:.2f})" for t in times)
    return f"1+{boost}*({terms})"
