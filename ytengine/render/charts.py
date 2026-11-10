"""Animated data charts (matplotlib, brand style): the line draws itself, highlights pop in.

The figure is drawn once; each frame only updates the line data and redraws the canvas.
"""
from __future__ import annotations

from datetime import date
from typing import Iterator

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib import font_manager  # noqa: E402

from .. import config  # noqa: E402
from . import style  # noqa: E402
from .frames import ease_out, push_in  # noqa: E402

_registered = False


def _register_fonts() -> str:
    global _registered
    if not _registered:
        for kind in ("display", "regular"):
            p = style.font_path(kind)
            if p.exists():
                font_manager.fontManager.addfont(str(p))
        _registered = True
    names = {f.name for f in font_manager.fontManager.ttflist}
    return "Inter" if "Inter" in names else "DejaVu Sans"


def _fmt_value(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 100 else f"{v:,.2f}"


def chart_frames(spec: dict, series: list[tuple[str, float]], duration: float, fmt: str) -> Iterator[np.ndarray]:
    v = config.get().get(f"video.{fmt}")
    w, h, fps = v["width"], v["height"], v["fps"]
    n = max(1, round(duration * fps))
    family = _register_fonts()
    data = [(d, val) for d, val in series if spec["start"] <= d <= spec["end"]]
    if len(data) < 2:
        raise ValueError(f"no data for {spec.get('series')} {spec['start']}..{spec['end']}")
    xs = np.array([mdates.date2num(date.fromisoformat(d)) for d, _ in data])
    ys = np.array([val for _, val in data])

    bg, ink, grid, accent, accent2 = (style.color(k) for k in ("bg", "ink", "grid", "accent", "accent2"))
    dpi = 100
    fig = plt.figure(figsize=(w / dpi, h / dpi), dpi=dpi, facecolor=bg)
    vertical = h > w
    ax = fig.add_axes([0.09, 0.14, 0.84, 0.58] if not vertical else [0.12, 0.25, 0.80, 0.42], facecolor=bg)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(grid)
    ax.grid(axis="y", color=grid, linewidth=1.2)
    ax.tick_params(colors=ink + "99", labelsize=int(h * 0.017), length=0, pad=10)
    for lab in ax.get_xticklabels() + ax.get_yticklabels():
        lab.set_fontfamily(family)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda val, _: f"{val:,.0f}"))
    loc = mdates.AutoDateLocator(maxticks=8)
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.set_xlim(xs[0], xs[-1])
    pad = (ys.max() - ys.min()) * 0.08
    ax.set_ylim(max(0, ys.min() - pad), ys.max() + pad * 1.6)
    fig.text(0.09, 0.86 if not vertical else 0.80, spec.get("title", ""), color=ink, fontsize=int(h * 0.042),
             fontfamily="Archivo Black" if family == "Inter" else family)
    fig.text(0.09, 0.81 if not vertical else 0.77, spec.get("subtitle", ""), color=ink + "B3",
             fontsize=int(h * 0.022), fontfamily=family)
    fig.text(0.09, 0.05 if not vertical else 0.18, f"Source: {spec.get('source', '')}", color=ink + "80",
             fontsize=int(h * 0.015), fontfamily=family)
    fill = ax.fill_between(xs[:1], ys[:1], color=accent, alpha=0.0)
    (line,) = ax.plot(xs[:1], ys[:1], color=accent2, linewidth=3.2, solid_capstyle="round")
    (head,) = ax.plot(xs[:1], ys[:1], "o", color=accent2, markersize=9)

    marks = []
    for hl in spec.get("highlights", []):
        hx = mdates.date2num(date.fromisoformat(hl["date"]))
        idx = int(np.argmin(np.abs(xs - hx)))
        dot = ax.plot([xs[idx]], [ys[idx]], "o", color=accent, markersize=14, alpha=0.0, zorder=5)[0]
        right = idx > len(xs) * 0.7
        txt = ax.annotate(hl.get("label", ""), (xs[idx], ys[idx]), xytext=(-20 if right else 20, 26),
                          textcoords="offset points", ha="right" if right else "left", color=ink,
                          fontsize=int(h * 0.022), fontfamily=family, alpha=0.0,
                          bbox=dict(boxstyle="round,pad=0.4", fc=bg, ec=accent, lw=1.5, alpha=0.0))
        marks.append((idx, dot, txt))

    draw_n = int(n * 0.62)  # line finishes drawing at ~62% of the scene, then holds
    canvas = fig.canvas
    for i in range(n):
        p = ease_out(i / max(1, draw_n)) if i < draw_n else 1.0
        k = max(2, int(p * (len(xs) - 1)) + 1)
        line.set_data(xs[:k], ys[:k])
        head.set_data(xs[k - 1:k], ys[k - 1:k])
        head.set_alpha(1.0 if p < 1 else max(0.0, 1 - (i - draw_n) / (fps * 0.5)))
        fill.remove()
        fill = ax.fill_between(xs[:k], ys[:k], ax.get_ylim()[0], color=accent2, alpha=0.08, linewidth=0)
        for idx, dot, txt in marks:
            if k > idx:
                age = min(1.0, (k - idx) / max(1, len(xs) * 0.04) + (i - draw_n) / fps * 2 if i > draw_n else
                          (k - idx) / max(1, len(xs) * 0.04))
                a = ease_out(age)
                dot.set_alpha(a)
                txt.set_alpha(a)
                txt.get_bbox_patch().set_alpha(a)
        canvas.draw()
        frame = np.asarray(canvas.buffer_rgba())[..., :3]
        if i >= draw_n:  # slow push-in while the finished chart holds
            frame = push_in(frame, (i - draw_n) / max(1, n - draw_n), zoom=0.03)
        yield frame
    plt.close(fig)
