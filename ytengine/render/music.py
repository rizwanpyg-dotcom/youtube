"""Original generative ambient music bed (no third-party licence needed).

A slow chord progression of soft detuned sine/triangle pads with a gentle low pulse.
It is deliberately unobtrusive: it sits ~26 dB under the narration and ducks further
whenever the narrator speaks.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

SR = 48000

# Minor-key progressions (semitone offsets from A2 = 110 Hz); chosen per video for variety.
PROGRESSIONS = [
    [[0, 7, 12, 15], [-4, 3, 8, 12], [-9, -2, 3, 7], [-5, 2, 7, 10]],   # Am  F   C   G
    [[0, 7, 12, 15], [-2, 5, 10, 14], [-4, 3, 8, 12], [-5, 2, 7, 11]],  # Am  G   F   E
    [[0, 7, 10, 15], [-7, 0, 5, 9], [-4, 3, 8, 12], [-5, 2, 7, 10]],    # Am7 Dm  F   G
]


def _note(semi: float) -> float:
    return 110.0 * 2 ** (semi / 12)


def generate(seconds: float, path: Path, seed: int = 0, chord_s: float = 8.0) -> Path:
    rng = np.random.default_rng(seed)
    prog = PROGRESSIONS[seed % len(PROGRESSIONS)]
    n = int(seconds * SR)
    t = np.arange(n) / SR
    out = np.zeros((n, 2), dtype=np.float32)
    n_chords = int(np.ceil(seconds / chord_s)) + 1
    xfade = 2.5
    for ci in range(n_chords):
        start = ci * chord_s - xfade / 2
        a, b = max(0, int(start * SR)), min(n, int((start + chord_s + xfade) * SR))
        if a >= b:
            continue
        seg_t = t[a:b] - start
        dur = chord_s + xfade
        env = np.clip(np.minimum(seg_t / xfade, (dur - seg_t) / xfade), 0, 1) ** 1.5
        for semi in prog[ci % len(prog)]:
            f = _note(semi)
            for ch, detune in enumerate((-0.15, 0.15)):
                ph = rng.uniform(0, 2 * np.pi)
                w = 2 * np.pi * (f + detune) * seg_t + ph
                tri = 2 / np.pi * np.arcsin(np.sin(w))
                out[a:b, ch] += (0.6 * np.sin(w) + 0.15 * tri) * env * 0.06
        # soft low pulse on the root, once per bar
        root = _note(prog[ci % len(prog)][0] - 12)
        pulse_env = np.exp(-((seg_t % 2.0) * 3.0)) * env
        out[a:b, :] += (np.sin(2 * np.pi * root * seg_t) * pulse_env * 0.05)[:, None]
    # gentle global fade in/out and slow tremolo
    fade = np.clip(np.minimum(t / 3.0, (seconds - t) / 4.0), 0, 1)
    trem = 1 + 0.05 * np.sin(2 * np.pi * 0.07 * t)
    out *= (fade * trem)[:, None]
    out /= max(1e-6, np.abs(out).max()) / 0.5
    sf.write(path, out, SR)
    return path
