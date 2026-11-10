"""Pipeline orchestrator: moves one video through research -> script -> media -> publish.

Work is split into three idempotent tasks per video:
  research  gather + extract + verify claims, research QC gate
  script    write (up to 3 versions with QC feedback), script QC gate
  media     narration -> scenes -> mix -> thumbnail -> SEO -> QC -> package/upload
            (one task, because rendered media is not kept between CI runs)
Approval gates (settings) pause the video in status 'waiting' without blocking others.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
from pathlib import Path

from . import config, db, sources
from .agents import fact_check, publisher, qc, scriptwriter, seo, shorts, thumbnail, voice
from .log import get as get_log
from .render import compose

log = get_log("pipeline")

MAX_SCRIPT_VERSIONS = 3


class Waiting(Exception):
    """Video is blocked on a human decision; not an error."""


def _run_task(conn: sqlite3.Connection, video_id: int, kind: str, fn, version: int = 1):
    key = f"video:{video_id}:{kind}:v{version}"
    row = conn.execute("SELECT * FROM tasks WHERE idempotency_key=?", (key,)).fetchone()
    if row and row["status"] == "done":
        log.info("skip %s (already done)", key)
        return json.loads(row["result_json"] or "null")
    if row and row["status"] == "dead":
        raise RuntimeError(f"{key} is dead after {row['attempts']} attempts: {row['last_error']}")
    if row and row["status"] == "pending" and row["next_run_at"] > db.utcnow():
        raise Waiting(f"{kind} retry backoff until {row['next_run_at']}")
    db.enqueue(conn, key, kind, video_id)
    task_id = conn.execute("SELECT id FROM tasks WHERE idempotency_key=?", (key,)).fetchone()["id"]
    conn.execute("UPDATE tasks SET status='running', attempts=attempts+1, started_at=?, run_id=? WHERE id=?",
                 (db.utcnow(), os.environ.get("GITHUB_RUN_ID", "local"), task_id))
    try:
        result = fn()
    except Waiting:
        conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (task_id,))
        raise
    except Exception as e:
        status = db.fail(conn, task_id, f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}")
        db.set_stage(conn, video_id, db.video(conn, video_id)["stage"],
                     status="failed" if status == "dead" else "active", error=str(e)[:500])
        raise
    db.complete(conn, task_id, result if isinstance(result, (dict, list)) or result is None else {"value": result})
    return result


def _series_for(conn: sqlite3.Connection, video_id: int) -> dict[str, list]:
    v = db.video(conn, video_id)
    plan = json.loads(conn.execute("SELECT evidence_json FROM topics WHERE id=?",
                                   (v["topic_id"],)).fetchone()[0]).get("research_plan", {})
    return {s["series"]: sources.fred_series(s["series"], s["start"], s["end"]) for s in plan.get("fred", [])}


def _approval(conn: sqlite3.Connection, video_id: int, gate: str) -> str:
    conn.execute("INSERT OR IGNORE INTO approvals(video_id, gate) VALUES (?,?)", (video_id, gate))
    return conn.execute("SELECT decision FROM approvals WHERE video_id=? AND gate=?",
                        (video_id, gate)).fetchone()["decision"]


def decide(conn: sqlite3.Connection, video_id: int, gate: str, decision: str, by: str, comment: str = "") -> None:
    conn.execute("INSERT OR IGNORE INTO approvals(video_id, gate) VALUES (?,?)", (video_id, gate))
    conn.execute("UPDATE approvals SET decision=?, decided_by=?, comment=?, decided_at=? WHERE video_id=? AND gate=?",
                 (decision, by, comment, db.utcnow(), video_id, gate))
    v = db.video(conn, video_id)
    if decision == "approved" and v["status"] == "waiting":
        conn.execute("UPDATE videos SET status='active' WHERE id=?", (video_id,))
    elif decision == "rejected":
        conn.execute("UPDATE videos SET status='rejected', last_error=? WHERE id=?", (f"rejected: {comment}", video_id))
        conn.execute("UPDATE topics SET status='rejected', rejection_reason=? WHERE id=?",
                     (comment, v["topic_id"]))
    elif decision == "changes_requested" and gate == "script":
        conn.execute("UPDATE videos SET status='active', stage='fact_checked' WHERE id=?", (video_id,))
        rounds = int(db.get_setting(conn, f"script_round:{video_id}", "0")) + 1
        db.set_setting(conn, f"script_round:{video_id}", rounds, by=by)
    log.info("video %d %s gate: %s by %s", video_id, gate, decision, by)


# ------------------------------------------------------------------ stages
def stage_research(conn, video_id) -> dict:
    if db.video(conn, video_id)["format"] == "short":
        stats = shorts.inherit_research(conn, video_id)
        gate = qc.research_gate(conn, video_id, min_claims=3, min_publishers=1)
        if not gate.passed:
            raise RuntimeError("short research gate failed: " + "; ".join(gate.failures()))
        return stats
    result = fact_check.run(conn, video_id)  # network errors raise here and are retried with backoff
    gate = qc.research_gate(conn, video_id)
    if not gate.passed:
        # Not enough verifiable evidence is a property of the topic, not a transient error:
        # retire it at once instead of re-fetching the same sources three times.
        reason = "insufficient evidence: " + "; ".join(gate.failures())
        conn.execute("UPDATE topics SET status='rejected', rejection_reason=? WHERE id=?",
                     (reason, db.video(conn, video_id)["topic_id"]))
        conn.execute("UPDATE videos SET status='rejected', last_error=? WHERE id=?", (reason[:500], video_id))
        conn.execute("UPDATE calendar SET status='open', video_id=NULL WHERE video_id=?", (video_id,))
        log.warning("video %d rejected: %s (its calendar slot is reopened)", video_id, reason)
        return {**result["stats"], "rejected": reason}
    return result["stats"]


def stage_script(conn, video_id, series) -> dict:
    feedback = None
    approval = conn.execute("SELECT comment FROM approvals WHERE video_id=? AND gate='script' "
                            "AND decision='changes_requested'", (video_id,)).fetchone()
    if approval:
        feedback = approval["comment"]
    writer = shorts.write if db.video(conn, video_id)["format"] == "short" else scriptwriter.write
    for attempt in range(MAX_SCRIPT_VERSIONS):
        writer(conn, video_id, feedback)
        gate = qc.script_gate(conn, video_id, series)
        if gate.passed:
            if approval:  # rewrite done: ask the owner again
                conn.execute("UPDATE approvals SET decision='pending', issue_number=NULL, decided_at=NULL "
                             "WHERE video_id=? AND gate='script'", (video_id,))
            return {"version": scriptwriter.latest(conn, video_id)["version"], "score": gate.score}
        feedback = "Fix these QC failures: " + "; ".join(gate.failures())
    raise RuntimeError("script QC failed after %d versions: %s" % (MAX_SCRIPT_VERSIONS, feedback))


def stage_media(conn, video_id, series) -> dict:
    cfg = config.get()
    v = db.video(conn, video_id)
    work = cfg.paths.work / f"video_{video_id:04d}"
    script_row = scriptwriter.latest(conn, video_id)
    script = json.loads(script_row["outline_json"])
    paras = scriptwriter.paragraphs(script)
    t0 = time.time()

    narration = voice.synthesize([p["text"] for p in paras], work / "audio")
    db.set_stage(conn, video_id, "voiced")
    if not qc.audio_gate(conn, video_id, narration.wav, script_row["est_duration_s"], narration.stats).passed:
        raise RuntimeError("audio QC failed")

    built = compose.build(conn, video_id, paras, narration, work, v["format"], series, cfg)
    db.set_stage(conn, video_id, "rendered")
    defects = compose.ff.detect_defects(built["video"])
    if not qc.video_gate(conn, video_id, built["video"], v["format"], narration.duration_s, defects).passed:
        raise RuntimeError("video QC failed")

    meta = seo.build(conn, video_id, built["chapters"])
    thumb, thumb_text = thumbnail.make(conn, video_id, meta["thumbnail"], series, work / "thumbs")
    db.set_stage(conn, video_id, "thumbnailed")
    if not qc.thumbnail_gate(conn, video_id, thumb, thumb_text, meta["title"]).passed:
        raise RuntimeError("thumbnail QC failed")
    db.set_stage(conn, video_id, "seo_ready")
    if not qc.metadata_gate(conn, video_id, meta).passed:
        raise RuntimeError("metadata QC failed")
    final = qc.final_gate(conn, video_id)
    if not final.passed:
        raise RuntimeError("final QC failed: " + "; ".join(final.failures()))
    db.set_stage(conn, video_id, "qc_passed")

    files = {"video": built["video"], "thumbnail": thumb, "srt": built["srt"]}
    mode = db.get_setting(conn, "publish_mode", "package")
    total_s = time.time() - t0
    conn.execute("UPDATE videos SET duration_s=?, render_seconds=? WHERE id=?",
                 (narration.duration_s, total_s, video_id))
    if mode == "package":
        out = publisher.package(conn, video_id, files, meta)
        result = {"mode": mode, "package": str(out)}
    else:
        vid = publisher.upload(conn, video_id, files, meta)
        result = {"mode": mode, "youtube_video_id": vid}
    result.update(duration_s=narration.duration_s, tts_s=narration.synth_seconds, tts_rtf=narration.stats["rtf"],
                  render_s=built["render_s"], media_total_s=round(total_s, 1), title=meta["title"])
    return result


def produce(conn: sqlite3.Connection, video_id: int) -> dict:
    """Advance one video as far as it can go. Returns a summary; raises on failure."""
    if db.get_bool(conn, "pipeline_paused"):
        log.warning("pipeline is paused; nothing done")
        return {"paused": True}
    v = db.video(conn, video_id)
    if v["status"] in ("rejected", "done", "failed", "paused"):
        return {"skipped": v["status"]}
    out: dict = {"video_id": video_id}
    try:
        out["research"] = _run_task(conn, video_id, "research", lambda: stage_research(conn, video_id))
        if db.video(conn, video_id)["status"] == "rejected":
            out["rejected"] = (out["research"] or {}).get("rejected", "rejected")
            return out
        series = _series_for(conn, video_id)
        version = 1 + int(db.get_setting(conn, f"script_round:{video_id}", "0"))
        out["script"] = _run_task(conn, video_id, "script", lambda: stage_script(conn, video_id, series), version)

        if db.get_bool(conn, "require_script_approval", True):
            decision = _approval(conn, video_id, "script")
            if decision != "approved":
                db.set_stage(conn, video_id, "script_approval", status="waiting")
                raise Waiting(f"script approval {decision}")

        out["media"] = _run_task(conn, video_id, "media", lambda: stage_media(conn, video_id, series), version)
    except Waiting as w:
        out["waiting"] = str(w)
        log.info("video %d waiting: %s", video_id, w)
    return out
