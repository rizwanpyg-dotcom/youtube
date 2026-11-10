"""One scheduled pipeline cycle (what pipeline.yml runs every 4 hours).

  1. housekeeping: reclaim stale leases, respect /pause
  2. keep the topic backlog >= 10 (at most one research round per ~20 h)
  3. fill the content calendar; create Shorts from finished long-form videos
  4. produce: at most one long-form media render and two Shorts per run, inside a time budget
  5. notify the owner: approval issues, ready packages, dead tasks
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
import traceback

from . import config, db, llm
from .agents import shorts, topics
from .control import alert_dead_tasks, announce_packages, request_approvals
from .log import get as get_log
from .ops import RunRecorder
from . import pipeline

log = get_log("cycle")

TIME_BUDGET_S = 95 * 60          # stop starting new media work after this (job timeout is 150 min)
LONG_MEDIA_EST_S = 60 * 60       # worst-case estimate for one long-form render on the runner


def _llm_available() -> bool:
    return any(p.available() for p in llm.providers())


def run(conn, budget_s: int = TIME_BUDGET_S) -> dict:
    rec = RunRecorder(conn, "pipeline")
    t0 = time.time()
    summary: dict = {"produced": [], "errors": []}
    try:
        db.reclaim_stale(conn)
        if db.get_bool(conn, "pipeline_paused"):
            summary["paused"] = True
            rec.finish("success", "paused")
            return summary

        last = db.get_setting(conn, "last_topic_research", "2000-01-01T00:00:00Z")
        force = os.environ.get("YT_FORCE_RESEARCH") == "1"
        if topics.backlog(conn) < 10 and _llm_available() and (force or _hours_since(last) > 20):
            try:
                summary["topics"] = topics.research(conn)
                db.set_setting(conn, "last_topic_research", db.utcnow())
            except Exception as e:  # LLM quota or network: retry in ~4 h (next cycle or two), not 20 h
                summary["errors"].append(f"topic research: {e}")
                retry_at = datetime.now(timezone.utc) - timedelta(hours=17)
                db.set_setting(conn, "last_topic_research", retry_at.strftime("%Y-%m-%dT%H:%M:%SZ"))
        summary["calendar"] = topics.fill_calendar(conn)
        summary["new_shorts"] = shorts.create_children(conn, int(config.get().get("schedule.shorts.per_week", 2)))

        long_done = shorts_done = 0
        for v in conn.execute("SELECT id, format, stage FROM videos WHERE status='active' "
                              "ORDER BY publish_slot IS NULL, publish_slot, id").fetchall():
            elapsed = time.time() - t0
            needs_media = db.stage_index(v["stage"]) >= db.stage_index("script_approval")
            if v["format"] == "long":
                if long_done >= 1 and needs_media:
                    continue
                if needs_media and elapsed + LONG_MEDIA_EST_S > budget_s:
                    continue
            elif shorts_done >= 2:
                continue
            if elapsed > budget_s:
                break
            try:
                result = pipeline.produce(conn, v["id"])
                summary["produced"].append({"video": v["id"], **{k: result.get(k) for k in ("waiting", "media")}})
                if "media" in result:
                    if v["format"] == "long":
                        long_done += 1
                    else:
                        shorts_done += 1
            except Exception as e:
                summary["errors"].append(f"video {v['id']}: {type(e).__name__}: {e}")
                log.error("video %d failed: %s\n%s", v["id"], e, traceback.format_exc()[-1500:])

        summary["approval_issues"] = request_approvals(conn)
        summary["package_issues"] = announce_packages(conn)
        summary["failure_issues"] = alert_dead_tasks(conn)
        rec.finish("success" if not summary["errors"] else "failure", str(summary)[:1000])
    except Exception as e:
        rec.finish("failure", f"{type(e).__name__}: {e}")
        raise
    return summary


def _hours_since(ts: str) -> float:
    from .ops import _age_hours
    return _age_hours(ts)
