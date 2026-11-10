"""FFmpeg helpers: binary discovery, running, probing, loudness and defect detection."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from ..log import get as get_log

log = get_log("ffmpeg")


@lru_cache(maxsize=1)
def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # bundled static build (local Windows dev)
    return imageio_ffmpeg.get_ffmpeg_exe()


@lru_cache(maxsize=1)
def ffprobe_exe() -> str | None:
    return shutil.which("ffprobe")


def run(args: list[str], *, input_bytes: bytes | None = None, timeout: int = 3600) -> str:
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", *args]
    if input_bytes is None:
        cmd.insert(2, "-nostdin")
    proc = subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout)
    err = proc.stderr.decode("utf-8", "replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed ({proc.returncode}): {err[-1500:]}")
    return err


def probe(path: Path) -> dict:
    """ffprobe-like dict: {"streams": [...], "format": {"duration": str}}."""
    if ffprobe_exe():
        out = subprocess.run([ffprobe_exe(), "-v", "error", "-show_streams", "-show_format", "-of", "json",
                              str(path)], capture_output=True, text=True, check=True).stdout
        return json.loads(out)
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True)
    err = proc.stderr.decode("utf-8", "replace")
    dur = re.search(r"Duration: (\d+):(\d+):(\d+\.\d+)", err)
    seconds = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)) if dur else 0.0
    streams = []
    for m in re.finditer(r"Stream #\d+:\d+[^:]*: (Video|Audio): (\w+)([^\n]*)", err):
        kind, codec, rest = m.group(1).lower(), m.group(2), m.group(3)
        s = {"codec_type": kind, "codec_name": codec}
        if kind == "video":
            wh = re.search(r"(\d{2,5})x(\d{2,5})", rest)
            fps = re.search(r"([\d.]+) fps", rest)
            pix = re.search(r"\b(yuv\w+|rgb\w+|gray\w*)", rest)
            s.update(width=int(wh.group(1)), height=int(wh.group(2)),
                     r_frame_rate=f"{fps.group(1)}/1" if fps else "0/1", pix_fmt=pix.group(1) if pix else None)
        streams.append(s)
    return {"streams": streams, "format": {"duration": str(seconds)}}


def duration(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def loudness(path: Path) -> dict:
    """Integrated loudness (LUFS) and true peak (dBTP) via the loudnorm analyser."""
    err = run(["-i", str(path), "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    data = json.loads(err[err.rindex("{"): err.rindex("}") + 1])
    return {"lufs": float(data["input_i"]), "true_peak": float(data["input_tp"]),
            "lra": float(data["input_lra"]), "thresh": float(data["input_thresh"])}


def loudnorm_filter(path: Path, target: float = -14.0, tp: float = -1.5) -> str:
    """Two-pass loudnorm: measure first, then return the linear-mode filter string."""
    m = loudness(path)
    return (f"loudnorm=I={target}:TP={tp}:LRA=11:measured_I={m['lufs']}:measured_TP={m['true_peak']}:"
            f"measured_LRA={m['lra']}:measured_thresh={m['thresh']}:linear=true")


def normalize_audio(src: Path, dst: Path, target: float = -14.0, ceiling_db: float = -2.0,
                    sample_rate: int = 48000, channels: int = 1) -> dict:
    """Gain to the loudness target, then a brick-wall limiter to keep true peaks under the ceiling.

    loudnorm's linear mode silently falls back to lower loudness when the needed gain
    would clip, so gain and limiting are applied explicitly, iterating once if the
    limiter pulled the loudness down.
    """
    limit = 10 ** (ceiling_db / 20)
    gain = target - loudness(src)["lufs"]
    for _ in range(3):
        # Resample first: resampling after the limiter re-creates inter-sample overs.
        run(["-i", str(src), "-af",
             f"aresample={sample_rate},volume={gain:.2f}dB,"
             f"alimiter=limit={limit:.4f}:attack=1:release=60:level=false",
             "-ar", str(sample_rate), "-ac", str(channels), str(dst)])
        m = loudness(dst)
        if abs(m["lufs"] - target) <= 0.5:
            break
        gain += target - m["lufs"]
    return m


def max_silence(path: Path, noise_db: int = -45, min_d: float = 0.3) -> float:
    err = run(["-i", str(path), "-af", f"silencedetect=noise={noise_db}dB:d={min_d}", "-f", "null", "-"])
    durs = [float(x) for x in re.findall(r"silence_duration: ([\d.]+)", err)]
    return max(durs, default=0.0)


def detect_defects(path: Path) -> dict:
    """Total black time and longest frozen stretch."""
    err = run(["-i", str(path), "-vf", "blackdetect=d=0.5:pix_th=0.05,freezedetect=n=0.001:d=8",
               "-an", "-f", "null", "-"])
    black = sum(float(x) for x in re.findall(r"black_duration:([\d.]+)", err))
    freezes = [float(x) for x in re.findall(r"freeze_duration: ([\d.]+)", err)]
    return {"black_s": black, "max_freeze_s": max(freezes, default=0.0)}
