"""Captions from sentence timings: SRT for long-form, burned-in styling for Shorts."""
from __future__ import annotations

from pathlib import Path

MAX_CHARS = 42      # per line (broadcast convention)
MAX_LINES = 2


def _ts(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _wrap(words: list[str], max_chars: int) -> list[str]:
    text = " ".join(words)
    if len(text) <= max_chars:
        return [text]
    # Two lines: pick the split that makes the lines most even (no orphaned last word).
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        if len(a) <= max_chars and len(b) <= max_chars:
            score = max(len(a), len(b))
            if best is None or score < best[0]:
                best = (score, [a, b])
    if best:
        return best[1]
    lines, cur = [], ""
    for w in words:
        if cur and len(cur) + 1 + len(w) > max_chars:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    return lines + ([cur] if cur else [])


def _chunks(text: str, max_chars: int, max_lines: int) -> list[str]:
    """Split a sentence into the fewest cues that fit, with words spread evenly across them,
    so no cue is a lone leftover word flashing on screen."""
    words = text.split()
    n = max(1, -(-len(text) // (max_chars * max_lines)))  # ceil
    while True:
        per = -(-len(words) // n)
        groups = [words[i:i + per] for i in range(0, len(words), per)]
        wrapped = [_wrap(g, max_chars) for g in groups]
        if all(len(w) <= max_lines for w in wrapped) or n >= len(words):
            return ["\n".join(w) for w in wrapped]
        n += 1


def cues(sentences: list[dict], max_chars: int = MAX_CHARS, max_lines: int = MAX_LINES) -> list[tuple[float, float, str]]:
    """Split each sentence into readable cues, timed in proportion to character count."""
    out = []
    for s in sentences:
        parts = _chunks(s["text"], max_chars, max_lines)
        total = sum(len(p) for p in parts) or 1
        t = s["start"]
        span = s["end"] - s["start"]
        for p in parts:
            d = span * len(p) / total
            out.append((t, t + d, p))
            t += d
    return out


def write_srt(sentences: list[dict], path: Path) -> Path:
    lines = []
    for i, (a, b, text) in enumerate(cues(sentences), 1):
        lines += [str(i), f"{_ts(a)} --> {_ts(b)}", text, ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_ass_vertical(sentences: list[dict], path: Path, font: str = "Archivo Black") -> Path:
    """Large, centred captions for 1080x1920 Shorts (burned in)."""
    def ts(t: float) -> str:
        cs = int(round(t * 100))
        h, cs = divmod(cs, 360000)
        m, cs = divmod(cs, 6000)
        s, cs = divmod(cs, 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},64,&H00E4EDF2,&H000000FF,&H00181411,&H80000000,0,0,0,0,100,100,0,0,1,5,0,2,80,80,520,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for a, b, text in cues(sentences, max_chars=22, max_lines=2):
        ass_text = text.replace("\n", r"\N")  # kept outside the f-string for Python 3.11
        events.append(f"Dialogue: 0,{ts(a)},{ts(b)},Default,,0,0,0,,{ass_text}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path
