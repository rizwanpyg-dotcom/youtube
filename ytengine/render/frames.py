"""Frame generation for scenes: animated title/number/quote cards and Ken Burns stills.

Frames are produced as uint8 RGB numpy arrays and streamed to an FFmpeg encoder.
Grain and dip-to-background fades are applied by FFmpeg filters (C), not per frame in Python.
"""
from __future__ import annotations

import math
import subprocess
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
from PIL import Image

from .. import config
from . import ffmpeg as ff
from . import style


def ease_out(x: float) -> float:
    x = min(max(x, 0.0), 1.0)
    return 1 - (1 - x) ** 3


class Encoder:
    """Pipe raw RGB frames into libx264."""

    def __init__(self, out: Path, w: int, h: int, fps: int, crf: int, preset: str, duration: float,
                 dip: float = 0.22):
        bg = style.color("bg").lstrip("#")
        vf = (f"noise=alls=5:allf=t+u,fade=t=in:st=0:d={dip}:color=0x{bg},"
              f"fade=t=out:st={max(0.0, duration - dip):.3f}:d={dip}:color=0x{bg},format=yuv420p")
        self.proc = subprocess.Popen(
            [ff.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{w}x{h}", "-r", str(fps), "-i", "-", "-vf", vf, "-c:v", "libx264", "-preset", preset,
             "-crf", str(crf), "-pix_fmt", "yuv420p", "-profile:v", "high", "-r", str(fps), str(out)],
            stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    def write(self, frame: np.ndarray) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        err = self.proc.stderr.read().decode("utf-8", "replace") if self.proc.stderr else ""
        if self.proc.wait() != 0:
            raise RuntimeError(f"encoder failed: {err[-1000:]}")


def encode(out: Path, frames: Iterator[np.ndarray], fmt: str, duration: float) -> int:
    v = config.get().get(f"video.{fmt}")
    enc = Encoder(out, v["width"], v["height"], v["fps"], v["crf"], v["preset"], duration)
    count = 0
    try:
        for f in frames:
            enc.write(f)
            count += 1
    finally:
        enc.close()
    return count


# ------------------------------------------------------------------ motion
def push_in(img: Image.Image | np.ndarray, progress: float, zoom: float = 0.045,
            drift: tuple[float, float] = (0.0, 0.0)) -> np.ndarray:
    """Sub-pixel smooth zoom (Ken Burns) via OpenCV warpAffine.

    ~7 ms per 1080p frame versus ~70 ms for Pillow's transform, which matters on a
    4-vCPU runner rendering 18,000+ frames per long-form video.
    """
    import cv2
    a = np.asarray(img)
    h, w = a.shape[:2]
    s = 1 + zoom * progress
    cx = w / 2 + drift[0] * w * progress
    cy = h / 2 + drift[1] * h * progress
    m = np.array([[s, 0, w / 2 - s * cx], [0, s, h / 2 - s * cy]], dtype=np.float32)
    return cv2.warpAffine(a, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)


# ------------------------------------------------------------------ cards
def card_layers(spec: dict, w: int, h: int) -> list[tuple[Image.Image, float]]:
    """Return [(RGBA layer, appear_time_s)] for a card. Styles: number | statement | quote | title.

    The text block (eyebrow, big text, small text) is measured first and then centred
    vertically, slightly above the optical middle.
    """
    style_name = spec.get("style", "title")
    vertical = h > w
    margin = int(w * (0.09 if not vertical else 0.08))
    max_w = w - 2 * margin
    _, probe = style.text_layer(8, 8)

    eyebrow = spec.get("eyebrow", "").upper()
    f_eb = style.font("semibold", int(h * (0.022 if not vertical else 0.016)))
    eb_lines = style.wrap(probe, eyebrow, f_eb, max_w - 22) if eyebrow else []
    eb_lh = int(f_eb.size * 1.35)

    big = spec.get("big", "")
    if style_name == "quote":
        f_big = style.font("semibold", int(h * (0.062 if not vertical else 0.040)))
        lh_big = int(f_big.size * 1.25)
    else:
        start = int(h * (0.20 if style_name == "number" else 0.14) * (0.62 if vertical else 1))
        f_big = style.fit_text(probe, big, "display", max_w, start)
        lh_big = int(f_big.size * 1.05)
    big_lines = style.wrap(probe, big, f_big, max_w)

    small = spec.get("small", "")
    f_sm = style.font("regular", int(h * (0.032 if not vertical else 0.022)))
    sm_lines = style.wrap(probe, small, f_sm, max_w) if small else []
    lh_sm = int(f_sm.size * 1.35)

    gap1, gap2 = int(f_eb.size * 1.2), int(h * 0.035)
    total = (len(eb_lines) * eb_lh + gap1 + len(big_lines) * lh_big
             + (gap2 + len(sm_lines) * lh_sm if sm_lines else 0))
    y = int((h - total) / 2 - h * 0.03)

    layers: list[tuple[Image.Image, float]] = []
    eb, d = style.text_layer(w, h)
    if eb_lines:
        d.rectangle([margin, y + 6, margin + 6, y + f_eb.size + 4], fill=style.color("accent"))
        for li, line in enumerate(eb_lines):
            d.text((margin + 22, y + li * eb_lh), line, font=f_eb, fill=style.color("accent2"))
    layers.append((eb, 0.15))
    y += len(eb_lines) * eb_lh + gap1

    bl, d = style.text_layer(w, h)
    negative = style_name == "number" and big.strip().startswith(("-", "−"))
    for li, line in enumerate(big_lines):
        d.text((margin - (4 if style_name != "quote" else 0), y + li * lh_big), line, font=f_big,
               fill=style.color("accent") if negative else style.color("ink"))
    layers.append((bl, 0.35))
    y += len(big_lines) * lh_big + gap2

    if sm_lines:
        sl, d = style.text_layer(w, h)
        for li, line in enumerate(sm_lines):
            d.text((margin, y + li * lh_sm), line, font=f_sm, fill=style.color("ink") + "B3")
        layers.append((sl, 0.75))
    return layers


def card_frames(spec: dict, duration: float, fmt: str) -> Iterator[np.ndarray]:
    v = config.get().get(f"video.{fmt}")
    w, h, fps = v["width"], v["height"], v["fps"]
    n = max(1, round(duration * fps))
    bg = Image.fromarray(style.background(w, h).astype(np.uint8)).convert("RGBA")
    layers = card_layers(spec, w, h)
    rise = int(h * 0.025)
    settle = max(a for _, a in layers) + 0.6
    static: np.ndarray | None = None
    for i in range(n):
        t = i / fps
        if t >= settle:  # animation finished: composite once, then only zoom
            if static is None:
                comp = bg.copy()
                for layer, _ in layers:
                    comp.alpha_composite(layer)
                static = np.asarray(comp.convert("RGB"))
            yield push_in(static, i / n, zoom=0.035)
            continue
        frame = bg.copy()
        for layer, appear in layers:
            p = ease_out((t - appear) / 0.6)
            if p <= 0:
                continue
            lay = layer
            if p < 1:
                alpha = lay.getchannel("A").point(lambda a, p=p: int(a * p))
                lay = lay.copy()
                lay.putalpha(alpha)
            frame.alpha_composite(lay, (0, int(rise * (1 - p))))
        yield push_in(frame.convert("RGB"), i / n, zoom=0.035)


def still_frames(image: Image.Image, caption: str, credit: str, duration: float, fmt: str,
                 seed: int = 0) -> Iterator[np.ndarray]:
    """Ken Burns over a licensed still, letterboxed on a blurred copy, with caption and credit."""
    from PIL import ImageFilter
    v = config.get().get(f"video.{fmt}")
    w, h, fps = v["width"], v["height"], v["fps"]
    n = max(1, round(duration * fps))
    img = image.convert("RGB")
    cover = img.copy()
    scale = max(w / cover.width, h / cover.height)
    cover = cover.resize((math.ceil(cover.width * scale), math.ceil(cover.height * scale)), Image.LANCZOS)
    cover = cover.crop(((cover.width - w) // 2, (cover.height - h) // 2, (cover.width - w) // 2 + w,
                        (cover.height - h) // 2 + h)).filter(ImageFilter.GaussianBlur(40))
    cover = Image.blend(cover, Image.new("RGB", (w, h), style.rgb("bg")), 0.55)
    fit = min(w * 0.86 / img.width, h * 0.74 / img.height)
    fg = img.resize((int(img.width * fit), int(img.height * fit)), Image.LANCZOS)
    base = cover.convert("RGBA")
    base.paste(fg, ((w - fg.width) // 2, int(h * 0.07)))
    over, d = style.text_layer(w, h)
    f_cap, f_cr = style.font("medium", int(h * 0.028)), style.font("regular", int(h * 0.018))
    d.text((int(w * 0.07), int(h * 0.86)), caption, font=f_cap, fill=style.color("ink"))
    d.text((int(w * 0.07), int(h * 0.915)), credit, font=f_cr, fill=style.color("ink") + "99")
    base.alpha_composite(over)
    still = np.asarray(base.convert("RGB"))
    drift = [(0.01, 0.0), (-0.01, 0.005), (0.0, -0.008)][seed % 3]
    for i in range(n):
        yield push_in(still, i / n, zoom=0.06, drift=drift)


FrameFactory = Callable[[float], Iterator[np.ndarray]]
