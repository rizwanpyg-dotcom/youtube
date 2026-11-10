"""Brand style: colours, fonts and shared drawing helpers."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from .. import config


def color(name: str) -> str:
    return config.get().get(f"brand.colors.{name}")


def rgb(name: str) -> tuple[int, int, int]:
    h = color(name).lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _fonts_dir() -> Path:
    return config.get().paths.assets / "fonts"


@lru_cache(maxsize=64)
def font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    """kind: display | regular | medium | semibold | bold."""
    d = _fonts_dir()
    try:
        if kind == "display":
            return ImageFont.truetype(str(d / "ArchivoBlack-Regular.ttf"), size)
        f = ImageFont.truetype(str(d / "Inter.ttf"), size)
        f.set_variation_by_name({"regular": "Regular", "medium": "Medium", "semibold": "SemiBold",
                                 "bold": "Bold"}[kind])
        return f
    except OSError:  # fonts missing: fall back to matplotlib's bundled DejaVu
        import matplotlib
        base = Path(matplotlib.get_data_path()) / "fonts" / "ttf"
        return ImageFont.truetype(str(base / ("DejaVuSans-Bold.ttf" if kind in ("display", "bold") else "DejaVuSans.ttf")), size)


def font_path(kind: str) -> Path:
    d = _fonts_dir()
    return d / ("ArchivoBlack-Regular.ttf" if kind == "display" else "Inter.ttf")


def background(w: int, h: int) -> np.ndarray:
    """Dark brand background with a soft radial vignette (float32 RGB 0..255)."""
    base = np.array(rgb("bg"), dtype=np.float32)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    cx, cy = w * 0.42, h * 0.45
    r = np.sqrt(((xx - cx) / w) ** 2 + ((yy - cy) / h) ** 2)
    lift = np.clip(1.0 - r * 1.6, 0, 1)[..., None]
    glow = np.array([18, 20, 24], dtype=np.float32)
    img = base + glow * lift - 10 * np.clip(r - 0.45, 0, 1)[..., None]
    return np.clip(img, 0, 255)


@lru_cache(maxsize=4)
def grain_texture(w: int, h: int, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0, 1, (h + 64, w + 64)).astype(np.float32)


def fit_text(draw: ImageDraw.ImageDraw, text: str, kind: str, max_w: int, start: int, min_size: int = 28):
    size = start
    while size > min_size:
        f = font(kind, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return font(kind, min_size)


def wrap(draw: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont, max_w: int) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = f"{cur} {w}".strip()
        if draw.textlength(trial, font=f) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def text_layer(w: int, h: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    return img, ImageDraw.Draw(img)
