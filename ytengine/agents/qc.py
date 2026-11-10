"""Agent 10: quality gates. Every gate writes a qc_reports row and returns the report.

A gate fails if any check marked `hard` fails, or if the weighted score < min_qc_score.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .. import config, textutil
from ..log import get as get_log
from . import fact_check, scriptwriter

log = get_log("qc")


@dataclass
class Report:
    gate: str
    checks: list[dict] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "", weight: float = 1.0, hard: bool = False) -> None:
        self.checks.append({"name": name, "ok": bool(ok), "detail": detail, "weight": weight, "hard": hard})

    @property
    def score(self) -> float:
        total = sum(c["weight"] for c in self.checks) or 1.0
        return round(sum(c["weight"] for c in self.checks if c["ok"]) / total, 3)

    @property
    def passed(self) -> bool:
        if any(c["hard"] and not c["ok"] for c in self.checks):
            return False
        return self.score >= float(config.get().get("quality.min_qc_score", 0.85))

    def failures(self) -> list[str]:
        return [f"{c['name']}: {c['detail']}" for c in self.checks if not c["ok"]]

    def save(self, conn: sqlite3.Connection, video_id: int) -> "Report":
        conn.execute("INSERT INTO qc_reports(video_id, gate, passed, score, checks_json) VALUES (?,?,?,?,?)",
                     (video_id, self.gate, int(self.passed), self.score, json.dumps(self.checks)))
        lvl = log.info if self.passed else log.warning
        lvl("QC %s: %s score=%.2f %s", self.gate, "PASS" if self.passed else "FAIL", self.score,
            "; ".join(self.failures())[:600])
        return self


# ------------------------------------------------------------------ research
def research_gate(conn: sqlite3.Connection, video_id: int, min_claims: int = 12, min_publishers: int = 3) -> Report:
    r = Report("research")
    n = conn.execute("SELECT COUNT(*) FROM claims WHERE video_id=? AND status='verified'", (video_id,)).fetchone()[0]
    r.add("verified_claims", n >= min_claims, f"{n} verified (min {min_claims})", 3, hard=True)
    pubs = conn.execute(
        "SELECT COUNT(DISTINCT s.publisher) FROM claim_sources cs JOIN sources s ON s.id=cs.source_id "
        "JOIN claims c ON c.id=cs.claim_id WHERE c.video_id=? AND c.status='verified' AND s.tier<=2",
        (video_id,)).fetchone()[0]
    r.add("tier12_publishers", pubs >= min_publishers, f"{pubs} tier-1/2 publishers back verified claims", 2, hard=True)
    unverified_quotes = conn.execute(
        "SELECT COUNT(*) FROM claim_sources cs JOIN claims c ON c.id=cs.claim_id "
        "WHERE c.video_id=? AND c.status='verified' AND cs.quote_verified=0", (video_id,)).fetchone()[0]
    r.add("evidence_quotes_verified", unverified_quotes == 0, f"{unverified_quotes} unverified quotes", 2, hard=True)
    return r.save(conn, video_id)


# ------------------------------------------------------------------ script
_ATTRIB = re.compile(r"\b(said|says|wrote|told|according to|commented|called|argued|put it)\b", re.I)
_ADVICE = re.compile(r"\b(you should (buy|sell|invest)|financial advice|guaranteed returns?|buy now)\b", re.I)


def _nums(text: str) -> list[float]:
    return fact_check.numbers_in(text)


def _covered(n: float, allowed: list[float]) -> bool:
    return any(abs(n - a) < 1e-9 for a in allowed)


def script_gate(conn: sqlite3.Connection, video_id: int, series: dict | None = None) -> Report:
    r = Report("script")
    row = scriptwriter.latest(conn, video_id)
    script = json.loads(row["outline_json"])
    claims = {c["ref"]: c for c in conn.execute("SELECT ref, text, status, claim_type FROM claims WHERE video_id=?",
                                                (video_id,))}
    lo, hi = scriptwriter.target_words(conn, video_id)
    wc = row["word_count"]
    r.add("word_count", lo <= wc <= hi, f"{wc} words (target {lo}-{hi})", 2)

    bad_refs, uncited, unsupported_nums, bad_conflict, bad_quotes, screen_nums = [], [], [], [], [], []
    misquotes: list[str] = []
    for i, p in enumerate(scriptwriter.paragraphs(script)):
        refs = p["claims"]
        for ref in refs:
            c = claims.get(ref)
            if c is None or c["status"] not in ("verified", "conflicting"):
                bad_refs.append(f"P{i+1}:{ref}")
            elif c["status"] == "conflicting" and not re.search(r"disagree|vary|varies|range|depend", p["text"], re.I):
                bad_conflict.append(f"P{i+1}:{ref}")
        allowed: list[float] = []
        for ref in refs:
            if ref in claims:
                allowed += _nums(claims[ref]["text"])
        for s in textutil.sentences(p["text"]):
            nums = _nums(s)
            if (nums or '"' in s or "“" in s) and not refs:
                uncited.append(f"P{i+1}: {s[:60]}")
            for n in nums:
                if not _covered(n, allowed):
                    unsupported_nums.append(f"P{i+1}:{n:g}")
        if '"' in p["text"] or "“" in p["text"]:
            has_quote_claim = any(claims.get(ref, {}) and claims[ref]["claim_type"] == "quote" for ref in refs)
            if not (has_quote_claim and _ATTRIB.search(p["text"])):
                bad_quotes.append(f"P{i+1}")
        # quoted words (narration and on-screen) must appear verbatim in the cited evidence
        scene = p["scene"]
        spec = scene.get("spec", {})
        quoted = re.findall(r"[\"“]([^\"”]{6,})[\"”]", p["text"] + " " + str(spec.get("big", "")))
        if quoted:
            evidence = [row["evidence_quote"] for row in conn.execute(
                "SELECT cs.evidence_quote FROM claim_sources cs JOIN claims c ON c.id=cs.claim_id "
                "WHERE c.video_id=? AND c.ref IN (%s)" % ",".join("?" * len(refs)), (video_id, *refs))] if refs else []
            for q in quoted:
                q_clean = q.strip(" .,…")
                if not any(textutil.evidence_match(q_clean, ev) >= 0.95 for ev in evidence):
                    misquotes.append(f"P{i+1}: {q_clean[:50]}")
        if scene["type"] == "card":
            for key in ("eyebrow", "big", "small"):
                for n in _nums(str(spec.get(key, ""))):
                    if not _covered(n, allowed):
                        screen_nums.append(f"P{i+1}.{key}:{n:g}")
        elif scene["type"] == "chart":
            data = (series or {}).get(spec.get("series"), [])
            for h in spec.get("highlights", []):
                date_parts = [float(x) for x in re.findall(r"\d+", h.get("date", ""))]
                val = fact_check._series_value(data, h.get("date", "")) if data else None
                for n in _nums(h.get("label", "")):
                    ok = _covered(n, allowed) or _covered(n, date_parts) or \
                        (val is not None and abs(abs(n) - val[1]) <= 0.01 * val[1])
                    if not ok:
                        screen_nums.append(f"P{i+1}.highlight:{n:g}")

    r.add("claim_refs_valid", not bad_refs, ", ".join(bad_refs), 3, hard=True)
    r.add("factual_sentences_cited", not uncited, "; ".join(uncited[:5]), 3, hard=True)
    r.add("narration_numbers_supported", not unsupported_nums, ", ".join(unsupported_nums[:10]), 3, hard=True)
    r.add("onscreen_numbers_supported", not screen_nums, ", ".join(screen_nums[:10]), 2, hard=True)
    r.add("conflicts_framed", not bad_conflict, ", ".join(bad_conflict), 2, hard=True)
    r.add("quotes_attributed", not bad_quotes, ", ".join(bad_quotes), 2, hard=True)
    r.add("quotes_verbatim", not misquotes, "; ".join(misquotes), 3, hard=True)

    text = scriptwriter.narration_text(script)
    low = text.lower()
    banned = [b for b in scriptwriter._banned_phrases() if b.lower() in low]
    r.add("no_banned_phrases", not banned, ", ".join(banned), 2)
    advice = _ADVICE.findall(text)
    r.add("no_financial_advice", not advice, str(advice), 3, hard=True)
    long_sent = [s[:50] for s in textutil.sentences(text) if textutil.words(s) > 32]
    r.add("sentence_length", len(long_sent) <= 2, f"{len(long_sent)} sentences > 32 words", 1)
    first = " ".join(textutil.sentences(text)[:2])
    r.add("hook_concrete", bool(re.search(r"\d", first)), "first 2 sentences contain a concrete number/date", 1)

    others = conn.execute("SELECT s.body FROM scripts s WHERE s.video_id != ? AND s.status IN ('approved','qc_passed')",
                          (video_id,)).fetchall()
    sim = max((textutil.script_similarity(text, o["body"]) for o in others), default=0.0)
    cap = float(config.get().get("quality.max_similarity_to_past_scripts", 0.2))
    r.add("originality_vs_past", sim <= cap, f"max trigram Jaccard {sim:.3f} (cap {cap})", 2, hard=True)
    rep = r.save(conn, video_id)
    conn.execute("UPDATE scripts SET status=? WHERE id=?",
                 ("qc_passed" if rep.passed else "qc_failed", row["id"]))
    return rep


# ------------------------------------------------------------------ media helpers
def ffprobe(path: Path) -> dict:
    from ..render import ffmpeg as ff
    return ff.probe(path)  # uses ffprobe when installed, otherwise parses ffmpeg's own output


def audio_gate(conn: sqlite3.Connection, video_id: int, wav: Path, expected_s: float, stats: dict) -> Report:
    r = Report("audio")
    dur = stats["duration_s"]
    r.add("duration_vs_estimate", abs(dur - expected_s) / expected_s <= 0.25,
          f"{dur:.1f}s vs estimate {expected_s:.1f}s", 1)
    r.add("loudness", abs(stats["lufs"] - stats["target_lufs"]) <= 1.0,
          f"{stats['lufs']:.1f} LUFS (target {stats['target_lufs']})", 2, hard=True)
    r.add("true_peak", stats["true_peak"] <= -0.5, f"{stats['true_peak']:.1f} dBTP", 1)
    r.add("max_silence", stats["max_silence_s"] <= 1.5, f"{stats['max_silence_s']:.2f}s", 1)
    r.add("failed_sentences", stats.get("failed", 0) == 0, f"{stats.get('failed', 0)} failed", 3, hard=True)
    return r.save(conn, video_id)


def video_gate(conn: sqlite3.Connection, video_id: int, mp4: Path, fmt: str, audio_s: float,
               detect: dict) -> Report:
    r = Report("video")
    info = ffprobe(mp4)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    want = config.get().get(f"video.{fmt}")
    r.add("resolution", (v["width"], v["height"]) == (want["width"], want["height"]),
          f"{v['width']}x{v['height']}", 2, hard=True)
    r.add("codec", v["codec_name"] == "h264" and v.get("pix_fmt") == "yuv420p", f"{v['codec_name']}/{v.get('pix_fmt')}", 2, hard=True)
    fps = eval(v["r_frame_rate"])  # noqa: S307 - ffprobe returns "30/1"
    r.add("fps", abs(fps - want["fps"]) < 0.01, f"{fps}", 1)
    r.add("has_audio", a is not None and a["codec_name"] == "aac", a["codec_name"] if a else "none", 3, hard=True)
    dur = float(info["format"]["duration"])
    r.add("av_duration", abs(dur - audio_s) < 1.0, f"video {dur:.2f}s vs narration {audio_s:.2f}s", 2, hard=True)
    r.add("no_black_segments", detect["black_s"] < 1.5, f"{detect['black_s']:.2f}s black", 1)
    r.add("no_frozen_segments", detect["max_freeze_s"] < 30, f"longest freeze {detect['max_freeze_s']:.1f}s", 1)
    lic = conn.execute("SELECT COUNT(*) FROM assets WHERE video_id=? AND (license IS NULL OR license='')",
                       (video_id,)).fetchone()[0]
    r.add("assets_licensed", lic == 0, f"{lic} assets without licence", 3, hard=True)
    if fmt == "short":
        r.add("short_length", dur <= 60, f"{dur:.1f}s", 3, hard=True)
    return r.save(conn, video_id)


def thumbnail_gate(conn: sqlite3.Connection, video_id: int, png: Path, text: str, title: str) -> Report:
    from PIL import Image
    r = Report("thumbnail")
    im = Image.open(png)
    r.add("size", im.size == (1280, 720), f"{im.size}", 2, hard=True)
    r.add("file_size", png.stat().st_size < 2 * 1024 * 1024, f"{png.stat().st_size // 1024} KB", 2, hard=True)
    r.add("word_count", textutil.words(text) <= 4, f"'{text}'", 1)
    nums = _nums(text)
    claim_nums = [n for row in conn.execute("SELECT text FROM claims WHERE video_id=? AND status='verified'",
                                            (video_id,)) for n in _nums(row["text"])]
    r.add("text_supported", all(_covered(n, claim_nums) for n in nums), f"numbers {nums}", 3, hard=True)
    return r.save(conn, video_id)


def metadata_gate(conn: sqlite3.Connection, video_id: int, meta: dict) -> Report:
    r = Report("metadata")
    r.add("title_length", 20 <= len(meta["title"]) <= 70, f"{len(meta['title'])} chars", 2, hard=True)
    r.add("description_length", len(meta["description"]) <= 5000, f"{len(meta['description'])} chars", 2, hard=True)
    r.add("tags_length", len(",".join(meta["tags"])) <= 480, f"{len(','.join(meta['tags']))} chars", 1, hard=True)
    if meta.get("format", "long") == "long":
        ch = meta.get("chapters", [])
        ok = len(ch) >= 3 and ch[0]["t"] == 0 and all(b["t"] - a["t"] >= 10 for a, b in zip(ch, ch[1:]))
        r.add("chapters_valid", ok, f"{len(ch)} chapters", 1)
    d = meta["description"]
    r.add("sources_listed", "Sources" in d, "", 2, hard=True)
    r.add("ai_disclosure", "AI-generated" in d, "", 2, hard=True)
    r.add("credits_listed", "Credits" in d, "", 1)
    title_nums = _nums(meta["title"])
    claim_nums = [n for row in conn.execute("SELECT text FROM claims WHERE video_id=? AND status='verified'",
                                            (video_id,)) for n in _nums(row["text"])]
    r.add("title_numbers_supported", all(_covered(n, claim_nums) for n in title_nums), f"{title_nums}", 3, hard=True)
    clickbait = re.findall(r"\b(shocking|you won't believe|insane|destroyed|exposed)\b", meta["title"], re.I)
    r.add("no_clickbait_words", not clickbait, str(clickbait), 2)
    return r.save(conn, video_id)


def final_gate(conn: sqlite3.Connection, video_id: int) -> Report:
    r = Report("final")
    latest = {}
    for row in conn.execute("SELECT gate, passed, score FROM qc_reports WHERE video_id=? ORDER BY id", (video_id,)):
        latest[row["gate"]] = row
    weights = {"research": 2, "script": 3, "audio": 1, "video": 2, "thumbnail": 1, "metadata": 1}
    for gate, w in weights.items():
        row = latest.get(gate)
        r.add(f"{gate}_passed", bool(row and row["passed"]),
              f"score {row['score']:.2f}" if row else "missing", w, hard=True)
    return r.save(conn, video_id)
