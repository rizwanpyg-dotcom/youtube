"""Agent 5: narration with Kokoro-82M (Apache-2.0), synthesized sentence by sentence.

Sentence-level synthesis gives exact timings for captions and scene cuts, and lets a
single bad sentence be retried without redoing the whole narration.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf
import yaml

from .. import config, textutil
from ..log import get as get_log
from ..render import ffmpeg as ff

log = get_log("voice")

SENTENCE_GAP = 0.20
PARAGRAPH_GAP = 0.55

_ROMAN = {"XXXIII": "thirty-three", "XXXIV": "thirty-four", "XXXV": "thirty-five", "XXXVI": "thirty-six",
          "II": "two", "III": "three", "IV": "four", "VI": "six", "VII": "seven", "VIII": "eight"}
_MONTHS = ("January|February|March|April|May|June|July|August|September|October|November|December")


def speakable(text: str, lexicon: dict[str, str] | None = None) -> str:
    """Rewrite symbols the TTS front-end tends to misread. Meaning is unchanged."""
    t = text.replace("“", "").replace("”", "").replace('"', "")
    for k, v in (lexicon or {}).items():
        t = re.sub(rf"\b{re.escape(k)}\b", v, t)
    t = re.sub(r"(Super Bowl|World War|Title) ([IVXL]+)\b", lambda m: f"{m.group(1)} {_ROMAN.get(m.group(2), m.group(2))}", t)
    t = re.sub(r"-(\d[\d,.]*)%", r"minus \1 percent", t)
    t = re.sub(r"(\d)%", r"\1 percent", t)
    t = re.sub(r"\$(\d[\d,.]*)\s*(trillion|billion|million|thousand)", r"\1 \2 dollars", t)
    t = re.sub(r"\$(\d[\d,.]*)", r"\1 dollars", t)
    # Years: "2000" -> "two thousand", "1995" -> "nineteen ninety-five" (TTS reads 4-digit numbers literally)
    t = re.sub(rf"\b({_MONTHS}) (\d{{1,2}}), (\d{{4}})", lambda m: f"{m.group(1)} {_ordinal(int(m.group(2)))}, {m.group(3)}", t)
    t = re.sub(r"\b(1[5-9]\d\d|20\d\d)\b", lambda m: _year(int(m.group(1))), t)
    t = re.sub(r"(\d),(\d{3})", r"\1\2", t)
    t = re.sub(r"\.com\b", " dot com", t)
    t = t.replace("&", " and ")
    return re.sub(r"\s+", " ", t).strip()


_ONES = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen " \
        "seventeen eighteen nineteen".split()
_TENS = "_ _ twenty thirty forty fifty sixty seventy eighty ninety".split()
_ORD = {"one": "first", "two": "second", "three": "third", "five": "fifth", "eight": "eighth", "nine": "ninth",
        "twelve": "twelfth"}


def _two(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + (f"-{_ONES[o]}" if o else "")


def _year(y: int) -> str:
    if 2000 <= y <= 2009:
        return "two thousand" + (f" {_ONES[y - 2000]}" if y > 2000 else "")
    hi, lo = divmod(y, 100)
    return f"{_two(hi)} {'hundred' if lo == 0 else ('oh ' + _ONES[lo] if lo < 10 else _two(lo))}"


def _ordinal(n: int) -> str:
    words = _two(n)
    last = words.split("-")[-1]
    if last in _ORD:
        return words[: -len(last)] + _ORD[last]
    if last.endswith("y"):
        return words[:-1] + "ieth"
    return words + "th"


@dataclass
class Narration:
    wav: Path
    duration_s: float
    sentences: list[dict]   # {"p": paragraph index, "text", "start", "end"}
    paragraphs: list[dict]  # {"p", "start", "end"}
    synth_seconds: float
    stats: dict


_kokoro = None


def _engine():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro
        models = config.get().paths.models
        _kokoro = Kokoro(str(models / "kokoro-v1.0.onnx"), str(models / "voices-v1.0.bin"))
    return _kokoro


def synthesize(paragraph_texts: list[str], out_dir: Path) -> Narration:
    cfg = config.get()
    voice = cfg.get("tts.voice", "am_michael")
    speed = float(cfg.get("tts.speed", 1.0))
    lex_path = cfg.paths.root / "config" / "lexicon.yaml"
    lexicon = yaml.safe_load(lex_path.read_text(encoding="utf-8")) if lex_path.exists() else {}
    k = _engine()
    sr = 24000
    chunks, sentences, paragraphs = [], [], []
    t = 0.0
    failed = 0
    t0 = time.time()
    for pi, ptext in enumerate(paragraph_texts):
        p_start = t
        for si, s in enumerate(textutil.sentences(ptext)):
            spoken = speakable(s, lexicon)
            audio = None
            for attempt in range(2):
                try:
                    audio, sr = k.create(spoken, voice=voice, speed=speed, lang="en-us")
                    break
                except Exception as e:  # retry once, then record failure
                    log.warning("TTS failed (attempt %d) for %r: %s", attempt + 1, s[:50], e)
            if audio is None:
                failed += 1
                continue
            audio = _trim(np.asarray(audio, dtype=np.float32), sr)
            sentences.append({"p": pi, "text": s, "spoken": spoken, "start": round(t, 3),
                              "end": round(t + len(audio) / sr, 3)})
            chunks.append(audio)
            t += len(audio) / sr
            gap = SENTENCE_GAP
            chunks.append(np.zeros(int(gap * sr), dtype=np.float32))
            t += gap
        # replace trailing sentence gap with paragraph gap
        extra = PARAGRAPH_GAP - SENTENCE_GAP
        chunks.append(np.zeros(int(extra * sr), dtype=np.float32))
        t += extra
        paragraphs.append({"p": pi, "start": round(p_start, 3), "end": round(t, 3)})
    synth_s = time.time() - t0
    out_dir.mkdir(parents=True, exist_ok=True)
    raw = out_dir / "narration_raw.wav"
    sf.write(raw, np.concatenate(chunks), sr)
    wav = out_dir / "narration.wav"
    target = float(cfg.get("tts.target_lufs", -14))
    loud = ff.normalize_audio(raw, wav, target)
    dur = ff.duration(wav)
    stats = {"duration_s": dur, "lufs": loud["lufs"], "true_peak": loud["true_peak"], "target_lufs": target,
             "max_silence_s": ff.max_silence(wav), "failed": failed, "voice": voice,
             "rtf": round(synth_s / max(dur, 1e-6), 3)}
    (out_dir / "timings.json").write_text(json.dumps({"sentences": sentences, "paragraphs": paragraphs}, indent=1),
                                          encoding="utf-8")
    log.info("narration %.1fs synthesized in %.1fs (RTF %.2f), %.1f LUFS", dur, synth_s, stats["rtf"], loud["lufs"])
    return Narration(wav, dur, sentences, paragraphs, synth_s, stats)


def _trim(audio: np.ndarray, sr: int, thresh: float = 0.004, pad: float = 0.04) -> np.ndarray:
    idx = np.where(np.abs(audio) > thresh)[0]
    if len(idx) == 0:
        return audio
    a, b = max(0, idx[0] - int(pad * sr)), min(len(audio), idx[-1] + int(pad * sr))
    return audio[a:b]
