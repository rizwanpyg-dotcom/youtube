"""Integration test: two scheduled cycles on a copy of the sample DB, with an owner approval in between.

Run:  python tests/integration_cycle.py      (needs network; renders one Short; ~5-10 min)
Writes work/integration/integration_report.json
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
work = ROOT / "work" / "integration"
if work.exists():
    shutil.rmtree(work)
work.mkdir(parents=True)
shutil.copy2(ROOT / "work" / "sample" / "sample.db", work / "integration.db")
os.environ.update(YT_DB_PATH=str(work / "integration.db"), YT_WORK_DIR=str(work), YT_OUTPUT_DIR=str(work / "output"),
                  YT_CACHE_DIR=str(ROOT / "work" / "cache"), YT_LLM="fixture:sample_dotcom")

from ytengine import config, control, cycle, dashboard, db, log, ops  # noqa: E402

config.load()
log.setup()
conn = db.connect()
db.migrate(conn)
report = {}

t0 = time.time()
report["cycle1"] = cycle.run(conn)
report["cycle1_s"] = round(time.time() - t0, 1)

short = conn.execute("SELECT id FROM videos WHERE format='short'").fetchone()
report["short_id"] = short["id"] if short else None
if short:
    reply = control.handle_comment(conn, 0, f"[video:{short['id']}] Script approval: test", "/approve", "OWNER",
                                   "integration-test (simulated owner)")
    report["approve_reply"] = reply
    # A comment from a non-owner must be ignored:
    report["stranger_reply"] = control.handle_comment(conn, 0, f"[video:{short['id']}] x", "/reject spam",
                                                      "NONE", "stranger")

t1 = time.time()
report["cycle2"] = cycle.run(conn)
report["cycle2_s"] = round(time.time() - t1, 1)
report["videos"] = [dict(r) for r in conn.execute("SELECT * FROM v_pipeline ORDER BY id")]
report["topics"] = [dict(r) for r in conn.execute(
    "SELECT id, title, category, status, final_score, demand_score, rejection_reason FROM topics ORDER BY id")]
report["tasks"] = [dict(r) for r in conn.execute("SELECT idempotency_key, status, attempts, substr(last_error,1,160) "
                                                 "err FROM tasks ORDER BY id")]
report["qc_short"] = [dict(r) for r in conn.execute("SELECT gate, passed, score FROM qc_reports WHERE video_id=? "
                                                    "ORDER BY id", (report["short_id"],))] if short else []
report["health"] = ops.health(conn, check_oauth=False)
report["runs"] = [dict(r) for r in conn.execute("SELECT workflow, status, started_at, finished_at FROM runs")]
dashboard.export(conn, work / "site")
(work / "integration_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
print(json.dumps({k: report[k] for k in ("cycle1_s", "cycle2_s", "short_id", "approve_reply", "stranger_reply")},
                 indent=2))
for v in report["videos"]:
    print(v)
