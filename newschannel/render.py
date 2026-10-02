"""Broadcast-style renderer.

Frames are composed with Pillow (so Devanagari is shaped correctly by Raqm) and piped to
FFmpeg. Photos get a slow Ken-Burns move with a hint of hand-held drift, cross-fades between
shots, a news strap, channel bug, scrolling ticker (landscape) and word-highlighted captions.
Long videos are rendered in parallel time-chunks and concatenated losslessly.
"""
from __future__ import annotations

import bisect
import math
import os
import subprocess
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ZOOM_HEADROOM = 1.18      # canvas is 18% larger than the frame so we can move/zoom without upscaling
XFADE = 0.35              # seconds of cross-fade between shots
CHUNK_SECONDS = 20


@dataclass
class Brand:
    name: str
    handle: str
    logo: str
    accent: tuple[int, int, int]
    dark: tuple[int, int, int]
    font_regular: str
    font_bold: str


@dataclass
class ShotT:
    path: str
    start: float
    end: float
    motion: str = "zoom_in"
    fx: float = 0.5
    fy: float = 0.5
    graphic: bool = False


@dataclass
class CapWord:
    text: str
    start: float
    end: float


@dataclass
class Strap:
    start: float
    end: float
    label: str
    headline: str


@dataclass
class Timeline:
    width: int
    height: int
    fps: int
    duration: float
    shots: list[ShotT]
    brand: Brand
    straps: list[Strap] = field(default_factory=list)
    words: list[CapWord] = field(default_factory=list)
    ticker: str = ""
    main_start: float = 0.0
    main_end: float = 0.0
    preset: str = "fast"
    crf: int = 18


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


# ----------------------------------------------------------------------------- helpers
@lru_cache(maxsize=32)
def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)


def wrap(text: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = f"{cur} {w}".strip()
        if f.getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


@lru_cache(maxsize=4)
def vignette(w: int, h: int) -> np.ndarray:
    y, x = np.ogrid[:h, :w]
    d = np.sqrt(((x - w / 2) / (w / 2)) ** 2 + ((y - h / 2) / (h / 2)) ** 2)
    return (1 - 0.38 * np.clip(d - 0.55, 0, 1) ** 1.6)[..., None].astype(np.float32)


def build_canvas(path: str, W: int, H: int, fx: float, fy: float, graphic: bool) -> Image.Image:
    """Photo -> canvas of the frame's aspect, 18% larger than the frame, graded + vignetted."""
    cw, ch = int(W * ZOOM_HEADROOM), int(H * ZOOM_HEADROOM)
    im = Image.open(path).convert("RGB")
    ratio = (im.width / im.height) / (cw / ch)
    if graphic or 0.55 <= ratio <= 2.0:                       # fill the frame, keep the subject
        s = max(cw / im.width, ch / im.height)
        rw, rh = max(cw, round(im.width * s)), max(ch, round(im.height * s))
        r = im.resize((rw, rh), Image.LANCZOS)
        left = int(min(max(fx * rw - cw / 2, 0), rw - cw))
        top = int(min(max(fy * rh - ch / 2, 0), rh - ch))
        canvas = r.crop((left, top, left + cw, top + ch))
    else:                                                     # odd aspect: sharp photo over blurred copy
        s = max(cw / im.width, ch / im.height)
        small = im.resize((max(8, int(im.width * s / 10)), max(8, int(im.height * s / 10))))
        bg = small.filter(ImageFilter.GaussianBlur(5)).resize((cw, ch), Image.BILINEAR)
        bg = ImageEnhance.Brightness(bg).enhance(0.55)
        fs = min(cw * 0.96 / im.width, ch * 0.92 / im.height)
        fg = im.resize((int(im.width * fs), int(im.height * fs)), Image.LANCZOS)
        bg.paste(fg, ((cw - fg.width) // 2, (ch - fg.height) // 2))
        canvas = bg
    if not graphic:
        canvas = ImageEnhance.Contrast(canvas).enhance(1.06)
        canvas = ImageEnhance.Color(canvas).enhance(1.04)
        arr = np.asarray(canvas, dtype=np.float32) * vignette(cw, ch)
        canvas = Image.fromarray(arr.astype(np.uint8))
    return canvas


def ken_burns(canvas: Image.Image, W: int, H: int, motion: str, p: float, t: float) -> Image.Image:
    cw, ch = canvas.size
    base = 1 / ZOOM_HEADROOM
    p = 0.5 * p + 0.5 * smooth(p)
    cx = 0.5
    if motion == "zoom_in":
        v = 0.98 + (base + 0.01 - 0.98) * p
    elif motion == "zoom_out":
        v = base + 0.01 + (0.98 - base - 0.01) * p
    elif motion in ("pan_left", "pan_right"):
        v = 0.90
        cx = 0.46 + 0.08 * p if motion == "pan_right" else 0.54 - 0.08 * p
    else:
        v = 1.0
    vw, vh = cw * v, ch * v
    if motion == "still":
        x0, y0, vw, vh = 0, 0, cw, ch          # whole canvas = the original card, uncropped
    else:
        jx = math.sin(t * 1.9) * 0.0018 * cw      # subtle hand-held drift
        jy = math.cos(t * 1.3) * 0.0018 * ch
        x0 = min(max(cw * cx - vw / 2 + jx, 0), cw - vw)
        y0 = min(max((ch - vh) / 2 + jy, 0), ch - vh)
    return canvas.resize((W, H), Image.BICUBIC, box=(x0, y0, x0 + vw, y0 + vh))


# ----------------------------------------------------------------------------- overlays
def rounded(size: tuple[int, int], fill, radius: int) -> Image.Image:
    im = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(im).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=fill)
    return im


def make_bug(tl: Timeline) -> Image.Image:
    b, W = tl.brand, tl.width
    size = max(26, int(W * (0.034 if tl.height > tl.width else 0.017)))
    f = font(b.font_bold, size)
    pad = int(size * 0.55)
    logo = None
    if b.logo and Path(b.logo).exists():
        logo = Image.open(b.logo).convert("RGBA")
        lh = int(size * 1.6)
        logo = logo.resize((int(logo.width * lh / logo.height), lh), Image.LANCZOS)
    tw = int(f.getlength(b.name))
    w = tw + pad * 2 + (logo.width + pad if logo else 0) + int(size * 0.5)
    h = int(size * 1.9)
    im = rounded((w, h), (*b.dark, 215), h // 2)
    d = ImageDraw.Draw(im)
    d.ellipse((pad * 0.6, h / 2 - size * 0.2, pad * 0.6 + size * 0.4, h / 2 + size * 0.2), fill=(*b.accent, 255))
    x = int(pad * 0.6 + size * 0.4 + pad * 0.6)
    if logo:
        im.paste(logo, (x, (h - logo.height) // 2), logo)
        x += logo.width + pad // 2
    d.text((x, h / 2), b.name, font=f, fill=(255, 255, 255, 255), anchor="lm")
    return im


def make_strap(tl: Timeline, s: Strap) -> Image.Image:
    b, W, H = tl.brand, tl.width, tl.height
    portrait = H > W
    size = int(W * (0.058 if portrait else 0.030))
    f = font(b.font_bold, size)
    tag_f = font(b.font_bold, int(size * 0.62))
    box_w = int(W * (0.88 if portrait else 0.80))
    lines = wrap(s.headline, f, box_w - size)[:3]
    lh = int(size * 1.45)
    tag_w, tag_h = int(tag_f.getlength(s.label)) + int(size * 0.9), int(size * 0.95)
    box_h = lh * len(lines) + int(size * 0.5)
    im = Image.new("RGBA", (box_w, tag_h + box_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, tag_h, box_w, tag_h + box_h), fill=(*b.dark, 232))
    d.rectangle((0, tag_h, int(size * 0.18), tag_h + box_h), fill=(*b.accent, 255))
    d.rectangle((0, 0, tag_w, tag_h), fill=(*b.accent, 255))
    d.text((tag_w / 2, tag_h / 2), s.label, font=tag_f, fill=(255, 255, 255, 255), anchor="mm")
    for i, line in enumerate(lines):
        d.text((int(size * 0.5), tag_h + int(size * 0.25) + i * lh), line, font=f, fill=(255, 255, 255, 255))
    return im


def make_ticker_strip(tl: Timeline, h: int) -> tuple[Image.Image, int, int]:
    """Returns (scroll strip, unit width, label width). Separators are drawn, not typed,
    because the Devanagari font has no diamond glyph."""
    b = tl.brand
    f = font(b.font_bold, int(h * 0.56))
    items = [t for t in tl.ticker.split("|") if t.strip()]
    gap = int(h * 1.1)
    unit = sum(int(f.getlength(t)) + gap for t in items)
    reps = (tl.width // max(unit, 1)) + 3
    strip = Image.new("RGB", (unit * reps, h), b.dark)
    d = ImageDraw.Draw(strip)
    x, r = 0, h * 0.16
    for _ in range(reps):
        for t in items:
            d.text((x, h / 2), t, font=f, fill=(255, 255, 255), anchor="lm")
            x += int(f.getlength(t)) + gap // 2
            d.polygon([(x, h / 2 - r), (x + r, h / 2), (x, h / 2 + r), (x - r, h / 2)], fill=b.accent)
            x += gap // 2
    label_w = int(f.getlength("सुर्खियाँ")) + int(h * 0.8)
    return strip, unit, label_w


@dataclass
class Chunk:
    start: float
    end: float
    words: list[CapWord]


def build_chunks(words: list[CapWord], portrait: bool) -> list[Chunk]:
    max_chars = 24 if portrait else 70
    chunks: list[Chunk] = []
    cur: list[CapWord] = []
    chars = 0
    for i, w in enumerate(words):
        cur.append(w)
        chars += len(w.text) + 1
        gap = (words[i + 1].start - w.end) if i + 1 < len(words) else 9
        if chars >= max_chars or w.text[-1:] in "।?!,." or gap > 0.5 or (portrait and len(cur) >= 4):
            chunks.append(Chunk(cur[0].start, cur[-1].end + 0.12, cur))
            cur, chars = [], 0
    if cur:
        chunks.append(Chunk(cur[0].start, cur[-1].end + 0.12, cur))
    return chunks


class Composer:
    """Per-process frame factory; keeps small caches so chunks render sequentially and fast."""

    def __init__(self, tl: Timeline):
        self.tl = tl
        self.W, self.H = tl.width, tl.height
        self.portrait = self.H > self.W
        self.starts = [s.start for s in tl.shots]
        self.canvases: dict[tuple, Image.Image] = {}
        self.bug = make_bug(tl)
        self.straps = [make_strap(tl, s) for s in tl.straps]
        self.chunks = build_chunks(tl.words, self.portrait)
        self.chunk_starts = [c.start for c in self.chunks]
        self.cap_cache: dict[tuple, tuple[Image.Image, int]] = {}
        self.ticker = None
        if tl.ticker:
            self.tick_h = int(self.H * 0.054)
            self.ticker = make_ticker_strip(tl, self.tick_h)
            self.tick_label = self._ticker_label()
        self.black = Image.new("RGB", (self.W, self.H))

    # -- shots
    def canvas(self, s: ShotT) -> Image.Image:
        key = (s.path, s.fx, s.fy)
        if key not in self.canvases:
            if len(self.canvases) >= 4:
                self.canvases.pop(next(iter(self.canvases)))
            self.canvases[key] = build_canvas(s.path, self.W, self.H, s.fx, s.fy, s.graphic)
        return self.canvases[key]

    def shot_frame(self, s: ShotT, t: float) -> Image.Image:
        p = (t - s.start) / max(1e-6, s.end - s.start)
        return ken_burns(self.canvas(s), self.W, self.H, "still" if s.graphic else s.motion,
                         min(1.0, max(0.0, p)), t)

    def base_frame(self, t: float) -> Image.Image:
        i = max(0, bisect.bisect_right(self.starts, t) - 1)
        s = self.tl.shots[i]
        frame = self.shot_frame(s, t)
        if i > 0 and t - s.start < XFADE:
            prev = self.shot_frame(self.tl.shots[i - 1], t)
            frame = Image.blend(prev, frame, smooth((t - s.start) / XFADE))
        return frame

    # -- overlays
    def _ticker_label(self) -> Image.Image:
        b, h = self.tl.brand, self.tick_h
        _, _, lw = self.ticker
        im = Image.new("RGB", (lw, h), b.accent)
        ImageDraw.Draw(im).text((lw / 2, h / 2), "सुर्खियाँ", font=font(b.font_bold, int(h * 0.56)),
                                fill=(255, 255, 255), anchor="mm")
        return im

    def caption(self, ci: int, active: int) -> Image.Image:
        key = (ci, active)
        if key in self.cap_cache:
            return self.cap_cache[key]
        if len(self.cap_cache) > 12:
            self.cap_cache.clear()
        ch = self.chunks[ci]
        size = int(self.W * (0.066 if self.portrait else 0.034))
        f = font(self.tl.brand.font_bold, size)
        maxw = int(self.W * (0.86 if self.portrait else 0.78))
        space = f.getlength(" ")
        lines: list[list[int]] = [[]]
        cur = 0.0
        for i, w in enumerate(ch.words):
            wl = f.getlength(w.text)
            if lines[-1] and cur + space + wl > maxw:
                lines.append([])
                cur = 0.0
            cur += (space if lines[-1] else 0) + wl
            lines[-1].append(i)
        lh = int(size * 1.4)
        pad = 10
        im = Image.new("RGBA", (self.W, lh * len(lines) + pad * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        for li, idxs in enumerate(lines):
            total = sum(f.getlength(ch.words[i].text) for i in idxs) + space * (len(idxs) - 1)
            x = (self.W - total) / 2
            for i in idxs:
                color = (255, 214, 10, 255) if i == active else (255, 255, 255, 255)
                d.text((x, pad + li * lh), ch.words[i].text, font=f, fill=color,
                       stroke_width=max(3, size // 12), stroke_fill=(0, 0, 0, 255))
                x += f.getlength(ch.words[i].text) + space
        self.cap_cache[key] = (im, len(lines))
        return self.cap_cache[key]

    def overlays(self, frame: Image.Image, t: float) -> None:
        tl, W, H = self.tl, self.W, self.H
        if not (tl.main_start <= t < tl.main_end):
            return
        frame.paste(self.bug, (int(W * 0.04), int(H * (0.055 if self.portrait else 0.05))), self.bug)
        # strap
        for s, im in zip(tl.straps, self.straps):
            if s.start <= t < s.end:
                a_in, a_out = smooth((t - s.start) / 0.45), smooth((s.end - t) / 0.3)
                x_final = (W - im.width) // 2 if self.portrait else int(W * 0.045)
                y = int(H * 0.13) if self.portrait else H - self.tick_h - 150 - im.height if self.ticker \
                    else H - 170 - im.height
                x = int(x_final - (1 - a_in) * (im.width + 40))
                if a_out < 1:
                    im = im.copy()
                    im.putalpha(im.getchannel("A").point(lambda v: int(v * a_out)))
                frame.paste(im, (x, y), im)
                break
        # captions
        ci = bisect.bisect_right(self.chunk_starts, t) - 1
        if ci >= 0 and t < self.chunks[ci].end:
            ch = self.chunks[ci]
            active = max(0, bisect.bisect_right([w.start for w in ch.words], t) - 1)
            im, nlines = self.caption(ci, active)
            if self.portrait:
                y = int(H * 0.66)
            else:
                y = H - (self.tick_h if self.ticker else 0) - 30 - im.height
            frame.paste(im, (0, y), im)
        # ticker
        if self.ticker:
            strip, unit, lw = self.ticker
            off = int(t * 110) % unit
            region = strip.crop((off, 0, off + W - lw, self.tick_h))
            frame.paste(region, (lw, H - self.tick_h))
            frame.paste(self.tick_label, (0, H - self.tick_h))

    def frame(self, t: float) -> Image.Image:
        img = self.base_frame(t)
        self.overlays(img, t)
        tl = self.tl
        fade = min(1.0, t / 0.4, (tl.duration - t) / 0.5)
        if fade < 1.0:
            img = Image.blend(self.black, img, max(0.0, fade))
        return img


# ----------------------------------------------------------------------------- driver
def _render_chunk(args: tuple[Timeline, int, int, str]) -> str:
    tl, f0, f1, out = args
    comp = Composer(tl)
    cmd = ["ffmpeg", "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{tl.width}x{tl.height}", "-r", str(tl.fps), "-i", "-",
           "-c:v", "libx264", "-preset", tl.preset, "-crf", str(tl.crf), "-pix_fmt", "yuv420p",
           "-g", str(tl.fps * 2), "-bf", "2", "-an", out]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    assert proc.stdin
    for f in range(f0, f1):
        proc.stdin.write(comp.frame(f / tl.fps).tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError(f"ffmpeg failed for chunk {f0}-{f1}")
    return out


def render_video(tl: Timeline, out_video: Path, workers: int | None = None) -> Path:
    """Render silent video of `tl` to out_video (H.264)."""
    work = out_video.parent / "_chunks"
    work.mkdir(parents=True, exist_ok=True)
    total = int(round(tl.duration * tl.fps))
    step = CHUNK_SECONDS * tl.fps
    jobs = [(tl, f, min(total, f + step), str(work / f"c{n:03d}.mp4"))
            for n, f in enumerate(range(0, total, step))]
    workers = workers or min(os.cpu_count() or 2, 8, len(jobs))
    if workers > 1:
        with ProcessPoolExecutor(workers) as ex:
            files = list(ex.map(_render_chunk, jobs))
    else:
        files = [_render_chunk(j) for j in jobs]
    lst = work / "list.txt"
    lst.write_text("".join(f"file '{Path(f).resolve()}'\n" for f in files))
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c", "copy", str(out_video)], check=True)
    for f in files:
        Path(f).unlink(missing_ok=True)
    lst.unlink(missing_ok=True)
    return out_video


# ----------------------------------------------------------------------------- cards
def make_card(path: Path, W: int, H: int, brand: Brand, title: str, subtitle: str = "") -> Path:
    """Intro / outro still: dark gradient, accent bar, centred Devanagari text."""
    arr = np.zeros((H, W, 3), np.uint8)
    g = np.linspace(1.0, 0.25, H)[:, None]
    for c in range(3):
        arr[..., c] = (brand.dark[c] * g * 1.1).clip(0, 255)
    im = Image.fromarray(arr)
    d = ImageDraw.Draw(im)
    big = font(brand.font_bold, int(min(W, H * 1.4) * 0.075))
    small = font(brand.font_regular, int(min(W, H * 1.4) * 0.036))
    lines = wrap(title, big, int(W * 0.84))
    lh = int(big.size * 1.4)
    y = H / 2 - lh * len(lines) / 2 - (small.size if subtitle else 0)
    for line in lines:
        d.text((W / 2, y), line, font=big, fill=(255, 255, 255), anchor="mt")
        y += lh
    d.rectangle((W / 2 - W * 0.12, y + 14, W / 2 + W * 0.12, y + 22), fill=brand.accent)
    if subtitle:
        d.text((W / 2, y + 48), subtitle, font=small, fill=(220, 220, 230), anchor="mt")
    im.save(path, quality=95)
    return path
