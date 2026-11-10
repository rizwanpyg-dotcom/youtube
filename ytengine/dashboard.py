"""Dashboard export: one sanitised JSON file read by the static site in dashboard/.

The site is public on a public repo, so only non-sensitive data is exported: no secrets
(none exist in the DB), errors are redacted and truncated, and approvals show the GitHub login.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, notify, quota
from .log import redact


def _rows(conn, sql, args=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def _clean(text: str | None, n: int = 300) -> str | None:
    return redact(text)[:n] if text else text


def build(conn: sqlite3.Connection) -> dict:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    for item in ("GitHub Actions (public repo)", "GitHub Pages", "Gemini API free tier", "YouTube Data API"):
        conn.execute("INSERT OR IGNORE INTO expenses(month, item, amount_usd, note) VALUES (?,?,0,'free tier')",
                     (month, item))
    repo = os.environ.get("GITHUB_REPOSITORY")
    pipeline = _rows(conn, "SELECT * FROM v_pipeline ORDER BY id DESC LIMIT 60")
    for p in pipeline:
        p["last_error"] = _clean(p["last_error"])
    health = _rows(conn, "SELECT check_name, ok, detail, ts FROM health_checks WHERE id IN "
                         "(SELECT MAX(id) FROM health_checks GROUP BY check_name) ORDER BY check_name")
    data = {
        "generated_at": db.utcnow(),
        "repo": repo,
        "run_url": notify.run_url(),
        "settings": {k: db.get_setting(conn, k) for k in
                     ("pipeline_paused", "publish_mode", "require_script_approval", "control_issue")},
        "health": health,
        "pipeline": pipeline,
        "stage_counts": _rows(conn, "SELECT stage, status, COUNT(*) n FROM videos GROUP BY stage, status"),
        "calendar": _rows(conn, "SELECT c.slot_at, c.format, c.status, c.video_id, v.working_title FROM calendar c "
                                "LEFT JOIN videos v ON v.id=c.video_id WHERE c.slot_at >= date('now','-7 days') "
                                "ORDER BY c.slot_at LIMIT 40"),
        "research_queue": [
            {**{k: r[k] for k in ("id", "title", "category", "angle", "final_score", "demand_score", "gap_score",
                                  "status")},
             "pageviews_12m": json.loads(r["evidence_json"]).get("pageviews_12m"),
             "tier12_refs": json.loads(r["evidence_json"]).get("tier12_refs")}
            for r in conn.execute("SELECT * FROM topics ORDER BY (status='candidate') DESC, final_score DESC LIMIT 30")],
        "scripts": _rows(conn, "SELECT s.video_id, s.version, s.word_count, ROUND(s.est_duration_s/60.0,1) est_min, "
                               "s.status, s.llm_provider, s.created_at FROM scripts s WHERE s.id IN "
                               "(SELECT MAX(id) FROM scripts GROUP BY video_id) ORDER BY s.video_id DESC LIMIT 30"),
        "claims_summary": _rows(conn, "SELECT video_id, status, COUNT(*) n FROM claims GROUP BY video_id, status"),
        "approvals": _rows(conn, "SELECT a.video_id, a.gate, a.subject, a.decision, a.decided_by, a.issue_number, "
                                 "a.requested_at, a.decided_at, v.working_title FROM approvals a LEFT JOIN videos v "
                                 "ON v.id=a.video_id ORDER BY a.id DESC LIMIT 40"),
        "failed_tasks": [{**r, "last_error": _clean(r["last_error"])} for r in
                         _rows(conn, "SELECT * FROM v_failed_tasks LIMIT 30")],
        "qc": _rows(conn, "SELECT video_id, gate, passed, score, created_at FROM qc_reports WHERE id IN "
                          "(SELECT MAX(id) FROM qc_reports GROUP BY video_id, gate) ORDER BY video_id DESC, gate"),
        "channel": _rows(conn, "SELECT * FROM channel_daily ORDER BY day DESC LIMIT 120"),
        "video_performance": _rows(conn, "SELECT v.id, s.title, v.youtube_video_id, v.format, v.published_at, "
                                         "SUM(a.views) views, ROUND(SUM(a.watch_minutes)/60.0,1) watch_hours, "
                                         "ROUND(AVG(a.avg_view_pct),1) avg_view_pct, SUM(a.subs_gained) subs "
                                         "FROM videos v LEFT JOIN seo_metadata s ON s.video_id=v.id "
                                         "LEFT JOIN analytics_daily a ON a.video_id=v.id "
                                         "WHERE v.youtube_video_id IS NOT NULL GROUP BY v.id ORDER BY v.id DESC"),
        "ypp": (_rows(conn, "SELECT * FROM v_monetization") or [None])[0],
        "costs": _rows(conn, "SELECT month, item, amount_usd, amount_pkr, approved_by, note FROM expenses "
                             "ORDER BY month DESC, item"),
        "api_usage": _rows(conn, "SELECT provider, day, units, requests, errors, daily_cap FROM api_usage "
                                 "WHERE day >= date('now','-14 days') ORDER BY day DESC, provider"),
        "youtube_quota_left_today": quota.remaining(conn, "youtube_data"),
        "runs": _rows(conn, "SELECT workflow, run_id, started_at, finished_at, status, ROUND(cpu_seconds) cpu_s, "
                            "ROUND(peak_rss_mb) peak_mb, summary FROM runs ORDER BY started_at DESC LIMIT 40"),
        "render_stats": _rows(conn, "SELECT id, format, ROUND(duration_s) duration_s, ROUND(render_seconds) media_s "
                                    "FROM videos WHERE render_seconds IS NOT NULL ORDER BY id DESC LIMIT 20"),
        "insights": _rows(conn, "SELECT created_at, kind, finding FROM insights ORDER BY id DESC LIMIT 20"),
        "strategy": _rows(conn, "SELECT dimension, value, ROUND(alpha/(alpha+beta),3) mean, n_videos "
                                "FROM strategy_arms ORDER BY dimension, mean DESC"),
        "niche": (_rows(conn, "SELECT name, evidence_json FROM niches WHERE status='active'") or [None])[0],
    }
    for r in data["runs"]:
        r["summary"] = _clean(r["summary"], 200)
    return data


def export(conn: sqlite3.Connection, out_dir: Path) -> Path:
    """Copy the static site and write data/dashboard.json next to it."""
    src = config.get().paths.root / "dashboard"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    shutil.copytree(src, out_dir)
    (out_dir / "data").mkdir(exist_ok=True)
    path = out_dir / "data" / "dashboard.json"
    path.write_text(json.dumps(build(conn), indent=1, default=str), encoding="utf-8")
    return path
