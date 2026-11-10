"""End-to-end demonstration on a fresh database.

Uses the recorded fixture LLM (tests/fixtures/sample_dotcom) so it runs without API keys.
Everything else is live: real source fetching, real FRED data, deterministic fact-checking,
Kokoro narration, rendering, QC gates and an upload package. The script approval is
recorded as 'local-demo', clearly separate from a real owner approval.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def run(keep_db: bool = False) -> int:
    root = Path(__file__).resolve().parent.parent
    work = root / "work" / "sample"
    work.mkdir(parents=True, exist_ok=True)
    db_path = work / "sample.db"
    if not keep_db:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    os.environ["YT_DB_PATH"] = str(db_path)
    os.environ["YT_WORK_DIR"] = str(work)
    os.environ["YT_OUTPUT_DIR"] = str(root / "output")
    os.environ.setdefault("YT_LLM", "fixture:sample_dotcom")

    from . import config, db, log, pipeline
    from .__main__ import add_topic
    config.load()
    log.setup()
    conn = db.connect()
    db.migrate(conn)
    log.attach_db(lambda: conn)
    t0 = time.time()

    topic = json.loads((config.get().paths.fixtures / "sample_dotcom" / "topic.json").read_text(encoding="utf-8"))
    existing = conn.execute("SELECT v.id FROM videos v JOIN topics t ON t.id=v.topic_id WHERE t.slug=?",
                            (topic["slug"],)).fetchone()
    vid = existing["id"] if existing else add_topic(conn, topic)

    first = pipeline.produce(conn, vid)
    print(json.dumps(first, indent=2, default=str))
    if "waiting" in first:
        pipeline.decide(conn, vid, "script", "approved", "local-demo (sample run, not an owner approval)")
    second = pipeline.produce(conn, vid)
    print(json.dumps(second, indent=2, default=str))

    report = {
        "video_id": vid,
        "wall_clock_s": round(time.time() - t0, 1),
        "first_pass": first,
        "second_pass": second,
        "claims": [dict(r) for r in conn.execute("SELECT ref, status, notes FROM claims WHERE video_id=? ORDER BY id",
                                                 (vid,))],
        "qc": [dict(r) for r in conn.execute("SELECT gate, passed, score FROM qc_reports WHERE video_id=? ORDER BY id",
                                             (vid,))],
        "video": dict(db.video(conn, vid)),
    }
    (work / "sample_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print("report:", work / "sample_report.json")
    return 0 if report["video"]["stage"] == "package_ready" else 1
