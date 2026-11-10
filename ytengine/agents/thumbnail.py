"""Agent 7: thumbnails. Three style families; the bandit picks which one is used.

Thumbnail text comes only from verified claims (checked by the QC thumbnail gate),
so a thumbnail can never promise something the video doesn't support.
"""
from __future__ import annotations

import io
import json
import sqlite3
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFilter  # noqa: E402

from ..log import get as get_log  # noqa: E402
from ..render import style  # noqa: E402
from . import strategy  # noqa: E402

log = get_log("thumbnail")

W, H = 1280, 720
STYLES = ["chart-crash", "headline-dark", "number-focus"]


def _chart_png(series: list[tuple[str, float]], start: str, end: str, w: int, h: int) -> Image.Image:
    data = [(d, v) for d, v in series if start <= d <= end]
    xs = [mdates.date2num(date.fromisoformat(d)) for d, _ in data]
    ys = [v for _, v in data]
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor="none")
    ax = fig.add_axes([0, 0, 1, 1], facecolor="none")
    ax.axis("off")
    ax.plot(xs, ys, color=style.color("accent2"), linewidth=7, solid_capstyle="round")
    ax.fill_between(xs, ys, min(ys), color=style.color("accent2"), alpha=0.12, linewidth=0)
    peak = int(np.argmax(ys))
    ax.plot([xs[peak]], [ys[peak]], "o", color=style.color("accent"), markersize=22)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return Image.open(buf).convert("RGBA")


def render(variant_style: str, big: str, label: str, series: list | None, chart_range: tuple[str, str] | None,
           out: Path) -> Path:
    bg = Image.fromarray(style.background(W, H).astype(np.uint8)).convert("RGBA")
    d = ImageDraw.Draw(bg)
    if variant_style == "chart-crash" and series and chart_range:
        # Chart occupies the right/lower area so its peak never collides with the headline.
        chart = _chart_png(series, *chart_range, int(W * 0.66), int(H * 0.62))
        glow = chart.filter(ImageFilter.GaussianBlur(10))
        pos = (int(W * 0.32), int(H * 0.32))
        bg.alpha_composite(glow, pos)
        bg.alpha_composite(chart, pos)
        f = style.fit_text(d, big, "display", int(W * 0.55), 230)
        _shadow_text(bg, (60, 40), big, f, style.color("accent"))
        fl = style.fit_text(d, label.upper(), "display", int(W * 0.5), 70)
        _shadow_text(bg, (66, 40 + int(f.size * 1.05)), label.upper(), fl, style.color("ink"))
    elif variant_style == "headline-dark":
        d.rectangle([0, 0, 22, H], fill=style.color("accent"))
        fl = style.fit_text(d, label.upper(), "display", int(W * 0.86), 120)
        _shadow_text(bg, (70, int(H * 0.18)), label.upper(), fl, style.color("ink"))
        f = style.fit_text(d, big, "display", int(W * 0.86), 260)
        _shadow_text(bg, (64, int(H * 0.18) + int(fl.size * 1.2)), big, f, style.color("accent"))
    else:  # number-focus
        f = style.fit_text(d, big, "display", int(W * 0.9), 360)
        tw = d.textlength(big, font=f)
        _shadow_text(bg, (int((W - tw) / 2), int(H * 0.16)), big, f, style.color("accent"))
        fl = style.fit_text(d, label.upper(), "display", int(W * 0.8), 90)
        lw = d.textlength(label.upper(), font=fl)
        _shadow_text(bg, (int((W - lw) / 2), int(H * 0.16) + int(f.size * 1.1)), label.upper(), fl,
                     style.color("ink"))
    out.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(out, "PNG", optimize=True)
    return out


def _shadow_text(img: Image.Image, xy: tuple[int, int], text: str, font, fill: str) -> None:
    shadow = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((xy[0] + 6, xy[1] + 8), text, font=font, fill=(0, 0, 0, 200))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))
    ImageDraw.Draw(img).text(xy, text, font=font, fill=fill)


def make(conn: sqlite3.Connection, video_id: int, concept: dict, series: dict, out_dir: Path) -> tuple[Path, str]:
    """concept = {"big": "-78%", "label": "Dot-com crash", "series": "NASDAQCOM", "range": [start, end]}"""
    conn.execute("DELETE FROM thumbnails WHERE video_id=?", (video_id,))
    chosen = strategy.choose(conn, "thumbnail_style", STYLES)
    s = series.get(concept.get("series", ""), None)
    rng = tuple(concept["range"]) if concept.get("range") else None
    chosen_path = None
    for i, st in enumerate(STYLES):
        variant = "ABC"[i]
        path = render(st, concept["big"], concept["label"], s, rng, out_dir / f"thumbnail_{variant}.png")
        conn.execute("INSERT INTO thumbnails(video_id, variant, style, text, selected) VALUES (?,?,?,?,?)",
                     (video_id, variant, st, f"{concept['big']} {concept['label']}", int(st == chosen)))
        if st == chosen:
            chosen_path = path
    log.info("thumbnail style %s chosen (bandit)", chosen)
    return chosen_path, f"{concept['big']} {concept['label']}"
