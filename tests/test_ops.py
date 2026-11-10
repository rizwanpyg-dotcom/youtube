"""Tests for the control plane, scheduling, Shorts, backups and dashboard export."""
import json
from datetime import datetime, timezone

import pytest


def _video(conn, slug="t1", fmt="long", title="Test topic", stage="planned", status="active", parent=None):
    conn.execute("INSERT OR IGNORE INTO topics(slug, fingerprint, title, category, evidence_json) VALUES (?,?,?,?,?)",
                 (slug, " ".join(sorted(title.lower().split())), title, "bubble", "{}"))
    tid = conn.execute("SELECT id FROM topics WHERE slug=?", (slug,)).fetchone()["id"]
    cur = conn.execute("INSERT INTO videos(topic_id, format, stage, status, parent_video_id) VALUES (?,?,?,?,?)",
                       (tid, fmt, stage, status, parent))
    return cur.lastrowid


# ------------------------------------------------------------------ control
def test_non_owner_commands_are_ignored(conn):
    from ytengine import control, db
    reply = control.handle_comment(conn, 1, "[control] Owner commands", "/pause", "NONE", "stranger")
    assert reply == ""
    assert not db.get_bool(conn, "pipeline_paused")
    assert control.handle_comment(conn, 1, "x", "/pause", "CONTRIBUTOR", "someone") == ""


def test_owner_pause_resume(conn):
    from ytengine import control, db
    control.handle_comment(conn, 1, "[control] Owner commands", "/pause", "OWNER", "me")
    assert db.get_bool(conn, "pipeline_paused")
    control.handle_comment(conn, 1, "[control] Owner commands", "/resume", "OWNER", "me")
    assert not db.get_bool(conn, "pipeline_paused")


def test_owner_approve_and_reject(conn):
    from ytengine import control
    v = _video(conn, stage="script_approval", status="waiting")
    conn.execute("INSERT INTO approvals(video_id, gate) VALUES (?, 'script')", (v,))
    control.handle_comment(conn, 5, f"[video:{v}] Script approval: X", "/approve", "OWNER", "me")
    a = conn.execute("SELECT decision, decided_by FROM approvals WHERE video_id=?", (v,)).fetchone()
    assert (a["decision"], a["decided_by"]) == ("approved", "me")
    assert conn.execute("SELECT status FROM videos WHERE id=?", (v,)).fetchone()[0] == "active"
    v2 = _video(conn, slug="t2", title="Other topic")
    control.handle_comment(conn, 6, f"[video:{v2}] Script approval: Y", "/reject not interesting", "OWNER", "me")
    assert conn.execute("SELECT status FROM videos WHERE id=?", (v2,)).fetchone()[0] == "rejected"


def test_changes_request_bumps_script_round(conn):
    from ytengine import control, db
    v = _video(conn, stage="script_approval", status="waiting")
    conn.execute("INSERT INTO approvals(video_id, gate, issue_number) VALUES (?, 'script', 9)", (v,))
    control.handle_comment(conn, 9, f"[video:{v}] Script approval: X", "/changes shorter intro", "OWNER", "me")
    assert db.get_setting(conn, f"script_round:{v}") == "1"
    row = conn.execute("SELECT stage, status FROM videos WHERE id=?", (v,)).fetchone()
    assert (row["stage"], row["status"]) == ("fact_checked", "active")


def test_published_validates_id(conn):
    from ytengine import control
    v = _video(conn, stage="package_ready", status="waiting")
    assert "Usage" in control.handle_comment(conn, 3, f"[video:{v}] Ready", "/published notanid", "OWNER", "me")
    control.handle_comment(conn, 3, f"[video:{v}] Ready", "/published dQw4w9WgXcQ", "OWNER", "me")
    row = conn.execute("SELECT stage, youtube_video_id FROM videos WHERE id=?", (v,)).fetchone()
    assert (row["stage"], row["youtube_video_id"]) == ("published", "dQw4w9WgXcQ")


def test_mode_change_is_recorded_as_approval(conn):
    from ytengine import control, db
    control.handle_comment(conn, 1, "[control]", "/mode review_window", "OWNER", "me")
    assert db.get_setting(conn, "publish_mode") == "review_window"
    assert conn.execute("SELECT COUNT(*) FROM approvals WHERE gate='publish_mode'").fetchone()[0] == 1
    assert "Usage" in control.handle_comment(conn, 1, "[control]", "/mode yolo", "OWNER", "me")


def test_approval_toggle_recorded(conn):
    from ytengine import control, db
    control.handle_comment(conn, 1, "[control]", "/approval off", "OWNER", "me")
    assert not db.get_bool(conn, "require_script_approval", True)
    assert conn.execute("SELECT subject FROM approvals WHERE gate='policy'").fetchone()[0] == \
        "require_script_approval=off"
    assert control.handle_comment(conn, 1, "[control]", "/approval off", "MEMBER", "x") == ""


def test_retry_resets_dead_tasks(conn):
    from ytengine import control, db
    v = _video(conn, status="failed")
    db.enqueue(conn, f"video:{v}:media:v1", "media", v)
    conn.execute("UPDATE tasks SET status='dead', attempts=3")
    control.handle_comment(conn, 2, f"[video:{v}] Task failed", "/retry", "OWNER", "me")
    t = conn.execute("SELECT status, attempts FROM tasks").fetchone()
    assert (t["status"], t["attempts"]) == ("pending", 0)


def test_pipeline_respects_pause(conn):
    from ytengine import db, pipeline
    v = _video(conn)
    db.set_setting(conn, "pipeline_paused", True)
    assert pipeline.produce(conn, v) == {"paused": True}


# ------------------------------------------------------------------ calendar & topics
def test_slots_follow_schedule(conn):
    from ytengine.agents import topics
    now = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)  # a Monday
    s = topics.slots(weeks=1, now=now)
    longs = [t for t, f in s if f == "long"]
    shorts = [t for t, f in s if f == "short"]
    assert longs == ["2026-11-10T14:00:00Z", "2026-11-13T14:00:00Z"]     # Tue, Fri
    assert shorts[0] == "2026-11-09T16:00:00Z"                           # Mon later today
    assert len(shorts) == 2


def test_duplicate_topics_detected(conn):
    from ytengine import textutil
    from ytengine.agents import topics
    conn.execute("INSERT INTO topics(slug, fingerprint, title, category) VALUES (?,?,?,?)",
                 ("dot-com-bubble", " ".join(sorted(textutil.keywords("The Dot-com Bubble crash"))),
                  "The Dot-com Bubble", "tech-failure"))
    assert topics._is_duplicate(conn, "dot-com-bubble", set())
    assert topics._is_duplicate(conn, "other-slug", textutil.keywords("The dot com bubble crash"))
    assert not topics._is_duplicate(conn, "tulip-mania", textutil.keywords("Tulip mania in Holland"))


def test_same_wikipedia_article_is_duplicate(conn):
    """Regression: 'The Dot-com Bubble' slipped past slug/fingerprint checks in the integration run."""
    from ytengine import textutil
    from ytengine.agents import topics
    conn.execute("INSERT INTO topics(slug, fingerprint, title, category, evidence_json) VALUES (?,?,?,?,?)",
                 ("dot-com-bubble-2000", textutil.fingerprint("The Dot-com Bubble"), "The Dot-com Bubble", "x",
                  json.dumps({"research_plan": {"wikipedia": ["Dot-com bubble"]}})))
    assert topics._is_duplicate(conn, "the-dot-com-bubble", textutil.keywords("Something else entirely"),
                                ["Dot-com_bubble"])
    assert not topics._is_duplicate(conn, "tulip-mania", textutil.keywords("Tulip Mania"), ["Tulip mania"])


def test_failed_task_waits_for_backoff(conn):
    """Regression: cycle 2 re-ran a failed research task immediately instead of honouring backoff."""
    from ytengine import db, pipeline
    v = _video(conn)
    calls = []

    def boom():
        calls.append(1)
        raise ConnectionError("network down")
    with pytest.raises(ConnectionError):
        pipeline._run_task(conn, v, "research", boom)
    with pytest.raises(pipeline.Waiting):
        pipeline._run_task(conn, v, "research", boom)       # still in backoff: not executed
    assert len(calls) == 1
    conn.execute("UPDATE tasks SET next_run_at='2000-01-01T00:00:00Z'")
    with pytest.raises(ConnectionError):
        pipeline._run_task(conn, v, "research", boom)       # backoff elapsed: retried
    assert len(calls) == 2


def test_insufficient_evidence_rejects_without_retry(conn, monkeypatch):
    """Regression: a topic failing the research gate was retried 3 times."""
    from ytengine import db, pipeline
    from ytengine.agents import fact_check
    v = _video(conn)
    conn.execute("INSERT INTO calendar(slot_at, format, video_id, status) VALUES ('2030-01-01T14:00:00Z','long',?,'assigned')", (v,))
    monkeypatch.setattr(fact_check, "run", lambda c, vid: {"stats": {"verified": 0}})
    out = pipeline.produce(conn, v)
    assert "rejected" in out
    assert conn.execute("SELECT status FROM videos WHERE id=?", (v,)).fetchone()[0] == "rejected"
    assert conn.execute("SELECT status FROM calendar").fetchone()[0] == "open"      # slot freed for another topic
    assert conn.execute("SELECT status FROM tasks").fetchone()[0] == "done"        # not queued for retry
    assert pipeline.produce(conn, v) == {"skipped": "rejected"}


def test_fill_calendar_assigns_best_topic_and_avoids_repeat_category(conn):
    from ytengine.agents import topics
    for slug, cat, score in (("a", "bank-panic", 0.9), ("b", "bank-panic", 0.8), ("c", "tech-failure", 0.7)):
        conn.execute("INSERT INTO topics(slug, fingerprint, title, category, final_score, status, evidence_json) "
                     "VALUES (?,?,?,?,?,'candidate','{}')", (slug, slug, slug.upper(), cat, score))
    topics.fill_calendar(conn, weeks=2)
    cats = [r["category"] for r in conn.execute(
        "SELECT t.category FROM videos v JOIN topics t ON t.id=v.topic_id ORDER BY v.publish_slot")]
    assert cats[0] == "bank-panic"
    assert all(a != b for a, b in zip(cats, cats[1:]))  # no two consecutive in the same category
    # idempotent: a second fill creates nothing new
    n = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
    topics.fill_calendar(conn, weeks=2)
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == n


# ------------------------------------------------------------------ shorts
def test_short_created_once_and_inherits_only_verified_claims(conn):
    from ytengine.agents import shorts
    parent = _video(conn, stage="package_ready", status="waiting")
    conn.execute("INSERT INTO sources(url, publisher, tier) VALUES ('https://x.org/a','x.org',2)")
    for ref, status in (("C1", "verified"), ("C2", "rejected"), ("C3", "verified")):
        cur = conn.execute("INSERT INTO claims(video_id, ref, text, claim_type, status) VALUES (?,?,?,?,?)",
                           (parent, ref, f"claim {ref}", "event", status))
        conn.execute("INSERT INTO claim_sources(claim_id, source_id, stance, evidence_quote, quote_verified) "
                     "VALUES (?,1,'supports','q',1)", (cur.lastrowid,))
    assert shorts.create_children(conn, per_week=2) == 1
    assert shorts.create_children(conn, per_week=2) == 0   # no duplicate child
    child = conn.execute("SELECT id FROM videos WHERE parent_video_id=?", (parent,)).fetchone()["id"]
    shorts.inherit_research(conn, child)
    refs = sorted(r["ref"] for r in conn.execute("SELECT ref FROM claims WHERE video_id=?", (child,)))
    assert refs == ["C1", "C3"]
    assert conn.execute("SELECT COUNT(*) FROM claim_sources cs JOIN claims c ON c.id=cs.claim_id "
                        "WHERE c.video_id=?", (child,)).fetchone()[0] == 2


def test_weekly_short_limit(conn):
    from ytengine.agents import shorts
    for i in range(3):
        _video(conn, slug=f"p{i}", title=f"Parent topic {i}", stage="package_ready", status="waiting")
    assert shorts.create_children(conn, per_week=2) == 2


# ------------------------------------------------------------------ backup / restore
def test_backup_restore_roundtrip(conn, tmp_path):
    from ytengine import config, ops
    _video(conn)
    gz = ops.backup(conn, tmp_path / "b")
    target = tmp_path / "restored.db"
    ops.restore(gz, target)
    import sqlite3
    assert sqlite3.connect(target).execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1


def test_restore_rejects_corrupt_backup(tmp_path):
    import gzip
    from ytengine import ops
    bad = tmp_path / "bad.db.gz"
    with gzip.open(bad, "wb") as g:
        g.write(b"not a database" * 100)
    with pytest.raises(Exception):
        ops.restore(bad, tmp_path / "x.db")


def test_health_checks_record(conn):
    from ytengine import ops
    checks = {c["check"]: c["ok"] for c in ops.health(conn, check_oauth=False)}
    assert checks["db_integrity"] and not checks["pipeline_recent"]   # never ran yet
    assert conn.execute("SELECT COUNT(*) FROM health_checks").fetchone()[0] == len(checks)


# ------------------------------------------------------------------ dashboard
def test_dashboard_export_is_sanitised(conn, tmp_path, monkeypatch):
    from ytengine import dashboard
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyLEAKLEAKLEAKLEAKLEAKLEAKLEAK12345")
    v = _video(conn)
    conn.execute("UPDATE videos SET last_error=? WHERE id=?",
                 ("HTTP 400 for key=AIzaSyLEAKLEAKLEAKLEAKLEAKLEAKLEAK12345 " + "x" * 1000, v))
    path = dashboard.export(conn, tmp_path / "site")
    text = path.read_text(encoding="utf-8")
    assert "AIzaSyLEAK" not in text
    data = json.loads(text)
    assert len(data["pipeline"][0]["last_error"]) <= 300
    assert (tmp_path / "site" / "index.html").exists()
    assert sum(c["amount_usd"] for c in data["costs"]) == 0


def test_research_command_and_force_flag(conn, monkeypatch):
    from ytengine import control, cycle, db as dbm
    from ytengine.agents import topics as tp
    dbm.set_setting(conn, "last_topic_research", dbm.utcnow())
    reply = control.handle_comment(conn, 1, "[control] Owner commands", "/research", "OWNER", "me")
    assert "next pipeline cycle" in reply
    assert dbm.get_setting(conn, "last_topic_research").startswith("2000")
    # force flag bypasses the once-a-day gate
    dbm.set_setting(conn, "last_topic_research", dbm.utcnow())
    ran = []
    monkeypatch.setattr(cycle, "_llm_available", lambda: True)
    monkeypatch.setattr(tp, "research", lambda c: ran.append(1) or {"added": 0, "skipped": []})
    monkeypatch.setenv("YT_FORCE_RESEARCH", "1")
    cycle.run(conn, budget_s=5)
    assert ran == [1]
