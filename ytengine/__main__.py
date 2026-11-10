"""Command-line entry point: python -m ytengine <command>.

Scheduled (GitHub Actions)
  cycle                       one pipeline cycle: topics, calendar, produce, notify
  analytics                   collect YouTube analytics + learning loop
  market                      Agent 1 market research (monthly)
  watchdog                    health checks, stale-lease recovery, alerts, heartbeat
  backup --out DIR            gzip snapshot of the DB
  export-dashboard --out DIR  static dashboard + sanitised JSON
  control                     apply an owner comment (reads env: ISSUE_NUMBER, ISSUE_TITLE,
                              COMMENT_BODY, AUTHOR_ASSOCIATION, GITHUB_ACTOR)
Manual
  init-db | status | topics | add-topic FILE.json | produce [--video N]
  approve --video N --gate script|final [--reject TEXT | --changes TEXT]
  restore FILE.db.gz
  sample                      end-to-end demo on a fresh DB with the fixture LLM
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ytengine")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init-db", "status", "cycle", "analytics", "market", "watchdog", "control", "topics"):
        sub.add_parser(name)
    a = sub.add_parser("add-topic")
    a.add_argument("file")
    p = sub.add_parser("produce")
    p.add_argument("--video", type=int)
    ap_ = sub.add_parser("approve")
    ap_.add_argument("--video", type=int, required=True)
    ap_.add_argument("--gate", choices=["script", "final"], required=True)
    ap_.add_argument("--by", default=os.environ.get("GITHUB_ACTOR", "local-owner"))
    g = ap_.add_mutually_exclusive_group()
    g.add_argument("--reject")
    g.add_argument("--changes")
    b = sub.add_parser("backup")
    b.add_argument("--out", default="backups")
    r = sub.add_parser("restore")
    r.add_argument("file")
    e = sub.add_parser("export-dashboard")
    e.add_argument("--out", default="site")
    s = sub.add_parser("sample")
    s.add_argument("--keep-db", action="store_true")
    args = ap.parse_args(argv)

    if args.cmd == "sample":
        from . import sample
        return sample.run(keep_db=args.keep_db)

    from . import config, db, log
    config.load()
    log.setup()
    if args.cmd == "restore":
        from . import ops
        ops.restore(Path(args.file), config.get().paths.db)
        print("restored", args.file, "->", config.get().paths.db)
        return 0
    conn = db.connect()
    db.migrate(conn)
    log.attach_db(lambda: conn)
    rc = 0

    if args.cmd == "init-db":
        print("database ready:", config.get().paths.db)
    elif args.cmd == "status":
        for row in conn.execute("SELECT * FROM v_pipeline ORDER BY id"):
            print(dict(row))
    elif args.cmd == "cycle":
        from . import cycle, notify
        _ensure_control_issue(conn, notify)
        summary = cycle.run(conn)
        print(json.dumps(summary, indent=2, default=str))
        rc = 1 if summary.get("errors") else 0
    elif args.cmd == "analytics":
        from .agents import analytics
        from .ops import RunRecorder
        rec = RunRecorder(conn, "analytics")
        out = analytics.collect(conn)
        learned = analytics.learn(conn)
        rec.finish("success", json.dumps({"collect": out, "learned": learned})[:900])
        print(json.dumps({"collect": out, "learned": learned}, indent=2))
    elif args.cmd == "market":
        from .agents import market
        print(json.dumps(market.run(conn), indent=2))
    elif args.cmd == "topics":
        from .agents import topics
        print(json.dumps(topics.research(conn), indent=2, default=str))
        print(json.dumps(topics.fill_calendar(conn), indent=2))
    elif args.cmd == "watchdog":
        from . import ops
        rec = ops.RunRecorder(conn, "watchdog")
        checks = ops.watchdog(conn)
        rec.finish("success", json.dumps(checks)[:900])
        print(json.dumps(checks, indent=2))
    elif args.cmd == "backup":
        from . import ops
        print(ops.backup(conn, Path(args.out)))
    elif args.cmd == "export-dashboard":
        from . import dashboard
        print(dashboard.export(conn, Path(args.out)))
    elif args.cmd == "control":
        from . import control, notify
        reply = control.handle_comment(conn, int(os.environ.get("ISSUE_NUMBER", "0")),
                                       os.environ.get("ISSUE_TITLE", ""), os.environ.get("COMMENT_BODY", ""),
                                       os.environ.get("AUTHOR_ASSOCIATION", ""), os.environ.get("GITHUB_ACTOR", ""))
        if reply:
            notify.comment(int(os.environ.get("ISSUE_NUMBER", "0")), reply)
        print(reply or "(ignored)")
    elif args.cmd == "add-topic":
        print("video id:", add_topic(conn, json.loads(Path(args.file).read_text(encoding="utf-8"))))
    elif args.cmd == "produce":
        from . import pipeline
        vid = args.video or _next_video(conn)
        if vid is None:
            print("nothing to produce")
        else:
            print(json.dumps(pipeline.produce(conn, vid), indent=2, default=str))
    elif args.cmd == "approve":
        from . import pipeline
        decision = "rejected" if args.reject else "changes_requested" if args.changes else "approved"
        pipeline.decide(conn, args.video, args.gate, decision, args.by, args.reject or args.changes or "")
    return rc


def _ensure_control_issue(conn, notify) -> None:
    from . import db
    if db.get_setting(conn, "control_issue") or not notify.enabled():
        return
    num = notify.find_open("[control]") or notify.open_issue(
        "[control] Owner commands",
        "Comment here to control the whole pipeline (only the repository owner is obeyed):\n\n"
        "- `/pause`: stop producing and publishing\n- `/resume`: start again\n"
        "- `/mode package|review_window|auto`: change publish mode\n"
        "- `/approval on|off`: require script approval before rendering\n- `/status`: summary of recent videos\n\n"
        "Video-specific commands (`/approve`, `/reject`, `/changes`, `/published`, `/retry`) go on that video's issue.",
        ["control"])
    if num:
        db.set_setting(conn, "control_issue", num)


def add_topic(conn, t: dict) -> int:
    from . import textutil
    slug = t.get("slug") or textutil.slugify(t["title"])
    evidence = {"research_plan": t["research_plan"]}
    if "target_words" in t:
        evidence["target_words"] = t["target_words"]
    conn.execute("INSERT INTO topics(slug, fingerprint, title, category, era, region, angle, status, evidence_json) "
                 "VALUES (?,?,?,?,?,?,?, 'selected', ?)",
                 (slug, textutil.fingerprint(t["title"]), t["title"],
                  t.get("category", "other"), t.get("era"), t.get("region"), t.get("angle"), json.dumps(evidence)))
    topic_id = conn.execute("SELECT id FROM topics WHERE slug=?", (slug,)).fetchone()["id"]
    conn.execute("INSERT INTO videos(topic_id, format, publish_slot) VALUES (?,?,?)",
                 (topic_id, t.get("format", "long"), t.get("publish_slot")))
    conn.execute("UPDATE topics SET status='in_production' WHERE id=?", (topic_id,))
    return conn.execute("SELECT id FROM videos WHERE topic_id=? ORDER BY id DESC LIMIT 1", (topic_id,)).fetchone()["id"]


def _next_video(conn) -> int | None:
    row = conn.execute("SELECT id FROM videos WHERE status='active' ORDER BY publish_slot IS NULL, publish_slot, id "
                       "LIMIT 1").fetchone()
    return row["id"] if row else None


if __name__ == "__main__":
    sys.exit(main())
