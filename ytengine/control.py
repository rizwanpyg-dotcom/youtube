"""Owner control plane: approval issues out, slash commands in.

Issue titles carry the subject, e.g. "[video:12] Script approval: Tulip Mania".
Commands (comment on that issue; only the repository OWNER is obeyed):
  /approve                 approve the gate the issue is about
  /reject <reason>         reject the video (its topic is retired)
  /changes <notes>         request a script rewrite with these notes
  /published <youtube-id>  record a manual (package-mode) upload
  /retry                   reset dead/failed tasks of this video
  /pause  /resume          stop or restart the whole pipeline
  /mode package|review_window|auto   change publish mode (recorded as an owner approval)
  /approval on|off         require (or stop requiring) script approval before rendering
  /status                  reply with a status summary
"""
from __future__ import annotations

import json
import re
import sqlite3

from . import db, notify, pipeline
from .agents import scriptwriter
from .log import get as get_log

log = get_log("control")

OWNER_ROLES = {"OWNER"}
_VIDEO_RE = re.compile(r"\[video:(\d+)\]")


# ------------------------------------------------------------------ outgoing
def _script_issue_body(conn: sqlite3.Connection, video_id: int) -> str:
    row = scriptwriter.latest(conn, video_id)
    script = json.loads(row["outline_json"])
    claims = conn.execute("SELECT ref, status, text, notes FROM claims WHERE video_id=? ORDER BY id",
                          (video_id,)).fetchall()
    qc = conn.execute("SELECT score FROM qc_reports WHERE video_id=? AND gate='script' ORDER BY id DESC LIMIT 1",
                      (video_id,)).fetchone()
    lines = [f"**Working title:** {script['title_working']}",
             f"**Length:** {row['word_count']} words (~{row['est_duration_s'] / 60:.1f} min) · "
             f"**Script QC:** {qc['score'] if qc else 'n/a'} · **Version:** {row['version']}", "",
             "Reply with `/approve`, `/changes <what to fix>` or `/reject <reason>`.", "", "## Narration", ""]
    for p in scriptwriter.paragraphs(script):
        refs = " ".join(p["claims"])
        lines.append(f"> {p['text']}  \n`{p['scene']['type']}` {refs}")
        lines.append("")
    lines += ["## Claims", "", "| Ref | Status | Claim | Basis |", "|---|---|---|---|"]
    for c in claims:
        lines.append(f"| {c['ref']} | {c['status']} | {c['text'].replace('|', '/')} | {(c['notes'] or '').replace('|', '/')} |")
    return "\n".join(lines)


def request_approvals(conn: sqlite3.Connection) -> int:
    """Open one issue per pending approval that doesn't have one yet."""
    opened = 0
    rows = conn.execute(
        "SELECT a.*, v.working_title FROM approvals a JOIN videos v ON v.id=a.video_id "
        "WHERE a.decision='pending' AND a.issue_number IS NULL").fetchall()
    for a in rows:
        if a["gate"] == "script":
            title = f"[video:{a['video_id']}] Script approval: {a['working_title'] or ''}"[:250]
            body = _script_issue_body(conn, a["video_id"])
        else:
            title = f"[video:{a['video_id']}] {a['gate'].title()} approval"
            body = "Reply `/approve` or `/reject <reason>`."
        num = notify.find_open(title.split("]")[0] + "]") or notify.open_issue(title, body, ["approval"])
        if num:
            conn.execute("UPDATE approvals SET issue_number=? WHERE id=?", (num, a["id"]))
            opened += 1
    return opened


def announce_packages(conn: sqlite3.Connection) -> int:
    """Tell the owner when an upload package is ready (package mode)."""
    n = 0
    for v in conn.execute("SELECT v.id, s.title, v.publish_slot FROM videos v JOIN seo_metadata s ON s.video_id=v.id "
                          "WHERE v.stage='package_ready' AND v.status='waiting'").fetchall():
        key = f"announced_package:{v['id']}"
        if db.get_setting(conn, key):
            continue
        url = notify.run_url() or "(local run)"
        body = (f"**{v['title']}** is rendered and passed every QC gate.\n\n"
                f"1. Open {url}, scroll to **Artifacts**, download `package-video-<run id>` (kept 14 days) and "
                f"open the folder starting `{v['id']:04d}-`.\n"
                f"2. Follow `UPLOAD_CHECKLIST.md` inside it. Planned slot: **{v['publish_slot'] or 'none'}**.\n"
                f"3. Comment `/published <youtube-video-id>` here after uploading, or `/reject <reason>`.")
        issue = notify.open_issue(f"[video:{v['id']}] Ready to upload: {v['title']}"[:250], body, ["package"])
        db.set_setting(conn, key, issue or "logged")
        n += 1
    return n


def alert_dead_tasks(conn: sqlite3.Connection) -> int:
    n = 0
    for t in conn.execute("SELECT * FROM tasks WHERE status='dead'").fetchall():
        key = f"alerted_dead:{t['id']}"
        if db.get_setting(conn, key):
            continue
        issue = notify.open_issue(f"[video:{t['video_id']}] Task failed: {t['kind']}",
                                  f"Task `{t['idempotency_key']}` failed {t['attempts']} times.\n\n"
                                  f"```\n{(t['last_error'] or '')[:1500]}\n```\n\nComment `/retry` after fixing the cause.",
                                  ["failure"])
        db.set_setting(conn, key, issue or "logged")
        n += 1
    return n


# ------------------------------------------------------------------ incoming
def handle_comment(conn: sqlite3.Connection, issue_number: int, issue_title: str, body: str,
                   author_association: str, actor: str) -> str:
    """Apply one comment. Returns the reply text (empty if ignored)."""
    text = (body or "").strip()
    if not text.startswith("/"):
        return ""
    if author_association not in OWNER_ROLES:
        log.warning("ignored command from %s (%s)", actor, author_association)
        return ""
    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()
    m = _VIDEO_RE.search(issue_title or "")
    vid = int(m.group(1)) if m else None
    gate = "script" if "Script approval" in (issue_title or "") else "final"

    if cmd == "/pause":
        db.set_setting(conn, "pipeline_paused", True, by=actor)
        return "Pipeline paused. Nothing new will be produced or published until `/resume`."
    if cmd == "/resume":
        db.set_setting(conn, "pipeline_paused", False, by=actor)
        return "Pipeline resumed."
    if cmd == "/mode":
        if arg not in ("package", "review_window", "auto"):
            return "Usage: `/mode package|review_window|auto`"
        conn.execute("INSERT INTO approvals(gate, subject, decision, decided_by, decided_at, issue_number) "
                     "VALUES ('publish_mode', ?, 'approved', ?, ?, ?)", (f"publish_mode={arg}", actor, db.utcnow(),
                                                                         issue_number))
        db.set_setting(conn, "publish_mode", arg, by=actor)
        return f"Publish mode set to `{arg}`."
    if cmd == "/approval":
        if arg not in ("on", "off"):
            return "Usage: `/approval on|off` (script approval before rendering)"
        conn.execute("INSERT INTO approvals(gate, subject, decision, decided_by, decided_at, issue_number) "
                     "VALUES ('policy', ?, 'approved', ?, ?, ?)", (f"require_script_approval={arg}", actor,
                                                                    db.utcnow(), issue_number))
        db.set_setting(conn, "require_script_approval", arg == "on", by=actor)
        return f"Script approval is now **{arg}**."
    if cmd == "/status":
        rows = conn.execute("SELECT id, stage, status, working_title FROM videos ORDER BY id DESC LIMIT 10").fetchall()
        return "\n".join(["| id | stage | status | title |", "|---|---|---|---|"] +
                         [f"| {r['id']} | {r['stage']} | {r['status']} | {r['working_title'] or ''} |" for r in rows])
    if cmd == "/research":
        db.set_setting(conn, "last_topic_research", "2000-01-01T00:00:00Z", by=actor)
        return "Topic research will run on the next pipeline cycle (if the topic backlog is below 10)."
    if vid is None:
        return "This command needs to be used on a `[video:N]` issue."
    if cmd == "/approve":
        pipeline.decide(conn, vid, gate, "approved", actor)
        return f"Approved ({gate}). The next pipeline run continues video {vid}."
    if cmd == "/reject":
        pipeline.decide(conn, vid, gate, "rejected", actor, arg or "no reason given")
        return f"Video {vid} rejected and its topic retired."
    if cmd == "/changes":
        if not arg:
            return "Usage: `/changes <what to fix>`"
        pipeline.decide(conn, vid, "script", "changes_requested", actor, arg)
        conn.execute("UPDATE approvals SET issue_number=NULL WHERE video_id=? AND gate='script'", (vid,))
        return "Rewrite requested. A new script version will be posted for approval."
    if cmd == "/published":
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", arg):
            return "Usage: `/published <11-character YouTube video id>`"
        conn.execute("UPDATE videos SET youtube_video_id=?, stage='published', status='done', published_at=? "
                     "WHERE id=?", (arg, db.utcnow(), vid))
        return f"Recorded: video {vid} is https://youtu.be/{arg}. Analytics will start tracking it."
    if cmd == "/retry":
        n = conn.execute("UPDATE tasks SET status='pending', attempts=0, next_run_at=? WHERE video_id=? "
                         "AND status IN ('dead','failed')", (db.utcnow(), vid)).rowcount
        conn.execute("UPDATE videos SET status='active', last_error=NULL WHERE id=? AND status='failed'", (vid,))
        return f"Reset {n} task(s) for video {vid}; it will be retried on the next run."
    return f"Unknown command `{cmd}`."
