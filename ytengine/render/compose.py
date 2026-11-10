"""Scene planning, per-scene rendering, concatenation and final audio/video mux."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from .. import config, sources
from ..log import get as get_log
from . import captions, charts, ffmpeg as ff, frames, music

log = get_log("compose")


def plan_scenes(paragraphs: list[dict], para_times: list[dict], total: float) -> list[dict]:
    """Scene i covers paragraph i: from its start to the next paragraph's start (last: to the end)."""
    scenes = []
    for i, p in enumerate(paragraphs):
        start = 0.0 if i == 0 else para_times[i]["start"]
        end = para_times[i + 1]["start"] if i + 1 < len(paragraphs) else total
        scenes.append({"index": i, "start": start, "end": end, "duration": end - start, **p["scene"],
                       "act": p.get("act"), "first_in_act": p.get("first_in_act", False)})
    return scenes


def _frame_bounds(scenes: list[dict], fps: int) -> None:
    """Exact frame counts from cumulative times, so rounding never drifts from the audio."""
    for s in scenes:
        f0, f1 = round(s["start"] * fps), round(s["end"] * fps)
        s["frames"] = max(1, f1 - f0)
        s["duration"] = s["frames"] / fps


def render_scenes(scenes: list[dict], out_dir: Path, fmt: str, series: dict[str, list], fps: int,
                  images: dict | None = None) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    _frame_bounds(scenes, fps)
    paths = []
    for s in scenes:
        path = out_dir / f"scene_{s['index']:03d}.mp4"
        t0 = time.time()
        spec = s["spec"]
        if s["type"] == "chart":
            gen = charts.chart_frames(spec, series[spec["series"]], s["duration"], fmt)
        elif s["type"] == "image" and images and s["index"] in images:
            img, caption, credit = images[s["index"]]
            gen = frames.still_frames(img, caption, credit, s["duration"], fmt, seed=s["index"])
        else:
            if s["type"] == "image":  # image unavailable or unlicensed: fall back to a text card
                spec = {"style": "title", "eyebrow": spec.get("caption", ""), "big": spec.get("fallback_text", ""),
                        "small": ""}
            gen = frames.card_frames(spec, s["duration"], fmt)
        n = frames.encode(path, gen, fmt, s["duration"])
        s["render_s"] = round(time.time() - t0, 2)
        log.info("scene %d (%s) %.1fs -> %d frames in %.1fs", s["index"], s["type"], s["duration"], n, s["render_s"])
        paths.append(path)
    return paths


def concat(paths: list[Path], out: Path) -> Path:
    lst = out.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in paths), encoding="utf-8")
    ff.run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(out)])
    return out


def mix_audio(narration: Path, music_path: Path, out: Path, music_db: float, target_lufs: float) -> dict:
    """Narration over a music bed that ducks (sidechain compression) while the narrator speaks."""
    premix = out.with_name("premix.wav")
    ff.run(["-i", str(narration), "-i", str(music_path), "-filter_complex",
            f"[1:a]volume={music_db}dB,aformat=channel_layouts=stereo[m];"
            f"[0:a]aformat=channel_layouts=stereo,asplit=2[v][sc];"
            f"[m][sc]sidechaincompress=threshold=0.02:ratio=6:attack=30:release=600[duck];"
            f"[v][duck]amix=inputs=2:duration=first:normalize=0[mix]",
            "-map", "[mix]", "-ar", "48000", str(premix)])
    return ff.normalize_audio(premix, out, target_lufs, channels=2)


def mux(video: Path, audio: Path, out: Path, burn_ass: Path | None = None, fonts_dir: Path | None = None) -> Path:
    args = ["-i", str(video), "-i", str(audio)]
    if burn_ass:
        fonts_dir = fonts_dir or burn_ass.parent
        # libass needs escaped paths in filter args
        esc = burn_ass.resolve().as_posix().replace(":", r"\:")
        fdir = fonts_dir.resolve().as_posix().replace(":", r"\:")
        args += ["-vf", f"ass='{esc}':fontsdir='{fdir}'", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                 "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]
    args += ["-c:a", "aac", "-b:a", "192k", "-map", "0:v:0", "-map", "1:a:0", "-shortest",
             "-movflags", "+faststart", str(out)]
    ff.run(args)
    return out


def register_assets(conn: sqlite3.Connection, video_id: int, scenes: list[dict], voice: str,
                    music_path: Path, fonts_dir: Path) -> None:
    """Licence register: every visual and audio element of the video gets a row."""
    owner = config.get().get("channel.name_working", "Channel owner")
    conn.execute("DELETE FROM assets WHERE video_id=?", (video_id,))
    rows = []
    for s in scenes:
        if s["type"] == "chart":
            rows.append(("chart", "generated", sources.fred_url(s["spec"]["series"]), f"scene_{s['index']:03d}",
                         "Original", None, owner,
                         f"Chart data: Federal Reserve Bank of St. Louis, FRED series {s['spec']['series']}"))
        elif s["type"] == "card":
            rows.append(("image", "generated", None, f"scene_{s['index']:03d}", "Original", None,
                         owner, None))
    rows.append(("voice", "generated", "https://huggingface.co/hexgrad/Kokoro-82M", "narration.wav", "Apache-2.0",
                 "https://www.apache.org/licenses/LICENSE-2.0", "hexgrad (Kokoro-82M model)",
                 f"AI narration: Kokoro-82M, voice {voice}"))
    rows.append(("music", "generated", None, music_path.name, "Original", None, owner,
                 "Music: original generative score"))
    for name, lic in (("ArchivoBlack-Regular.ttf", "OFL-1.1"), ("Inter.ttf", "OFL-1.1")):
        rows.append(("font", "brand", "https://github.com/google/fonts", name, lic,
                     "https://openfontlicense.org", None, None))
    conn.executemany(
        "INSERT INTO assets(video_id, kind, origin, origin_url, file_name, license, license_url, author, attribution) "
        "VALUES (?,?,?,?,?,?,?,?,?)", [(video_id, *r) for r in rows])


def build(conn: sqlite3.Connection, video_id: int, paragraphs: list[dict], narration, out_dir: Path, fmt: str,
          series: dict[str, list], cfg) -> dict:
    """Render scenes, mix audio, mux. Returns paths and measured timings."""
    v = cfg.get(f"video.{fmt}")
    t0 = time.time()
    scenes = plan_scenes(paragraphs, narration.paragraphs, narration.duration_s)
    scene_paths = render_scenes(scenes, out_dir / "scenes", fmt, series, v["fps"])
    silent = concat(scene_paths, out_dir / "video_silent.mp4")
    music_path = music.generate(narration.duration_s + 1.0, out_dir / "music.wav", seed=video_id)
    mixed = out_dir / "mix.wav"
    audio_stats = mix_audio(narration.wav, music_path, mixed, float(cfg.get("video.music_bed_db", -26)),
                            float(cfg.get("tts.target_lufs", -14)))
    srt = captions.write_srt(narration.sentences, out_dir / "captions.srt")
    burn = None
    if fmt == "short":
        burn = captions.write_ass_vertical(narration.sentences, out_dir / "captions.ass")
    final = mux(silent, mixed, out_dir / "video.mp4", burn, cfg.paths.assets / "fonts")
    register_assets(conn, video_id, scenes, cfg.get("tts.voice"), music_path, cfg.paths.assets / "fonts")
    render_s = time.time() - t0
    (out_dir / "scenes.json").write_text(json.dumps(scenes, indent=1, default=str), encoding="utf-8")
    log.info("video rendered in %.1fs (%.2fx realtime)", render_s, render_s / narration.duration_s)
    return {"video": final, "srt": srt, "scenes": scenes, "render_s": render_s, "audio": audio_stats,
            "chapters": [{"t": round(s["start"]), "title": s["act"]} for s in scenes if s["first_in_act"]]}
