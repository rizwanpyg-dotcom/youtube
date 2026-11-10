"""SQLite access, migrations, settings and the idempotent task queue."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from . import config

STAGES = [
    "planned", "researched", "fact_checked", "scripted", "script_approval", "voiced",
    "visuals_ready", "rendered", "thumbnailed", "seo_ready", "qc_passed",
    "final_approval", "package_ready", "scheduled", "published",
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_in(seconds: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = str(path or config.get().paths.db)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # autocommit; explicit tx below
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Apply db/schema.sql (idempotent) and any db/migrations/NNN_*.sql not yet applied."""
    paths = config.get().paths
    conn.executescript(paths.schema.read_text(encoding="utf-8"))
    current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    mig_dir = paths.root / "db" / "migrations"
    if mig_dir.exists():
        for f in sorted(mig_dir.glob("*.sql")):
            version = int(f.name.split("_", 1)[0])
            if version > current:
                with tx(conn):
                    conn.executescript(f.read_text(encoding="utf-8"))
                    conn.execute("INSERT INTO schema_version(version, description) VALUES (?,?)",
                                 (version, f.stem))
                current = version
    return current


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


# ---------------------------------------------------------------- settings
def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_bool(conn: sqlite3.Connection, key: str, default: bool = False) -> bool:
    value = get_setting(conn, key)
    return default if value is None else value.strip().lower() in ("1", "true", "yes", "on")


def set_setting(conn: sqlite3.Connection, key: str, value: Any, by: str = "system") -> None:
    conn.execute(
        "INSERT INTO settings(key, value, updated_by) VALUES (?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_by=excluded.updated_by, "
        "updated_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (key, str(value).lower() if isinstance(value, bool) else str(value), by),
    )


# ---------------------------------------------------------------- videos
def set_stage(conn: sqlite3.Connection, video_id: int, stage: str, status: str = "active",
              error: str | None = None) -> None:
    if stage not in STAGES:
        raise ValueError(f"unknown stage {stage}")
    conn.execute("UPDATE videos SET stage=?, status=?, last_error=? WHERE id=?",
                 (stage, status, error, video_id))


def stage_index(stage: str) -> int:
    return STAGES.index(stage)


def video(conn: sqlite3.Connection, video_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT v.*, t.slug, t.title AS topic_title, t.category, t.angle "
        "FROM videos v JOIN topics t ON t.id=v.topic_id WHERE v.id=?", (video_id,)).fetchone()
    if row is None:
        raise KeyError(f"video {video_id} not found")
    return row


# ---------------------------------------------------------------- task queue
LEASE_SECONDS = 90 * 60
BACKOFF_BASE = 600  # 10 min x 3^attempt


def enqueue(conn: sqlite3.Connection, key: str, kind: str, video_id: int | None = None,
            max_attempts: int = 3, run_at: str | None = None) -> int | None:
    """Insert a task once. Returns the new id, or None if the key already exists."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO tasks(idempotency_key, kind, video_id, max_attempts, next_run_at) "
        "VALUES (?,?,?,?,COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ','now')))",
        (key, kind, video_id, max_attempts, run_at),
    )
    return cur.lastrowid if cur.rowcount else None


def claim(conn: sqlite3.Connection, kind: str | None = None, run_id: str | None = None) -> sqlite3.Row | None:
    """Atomically lease the next due task."""
    with tx(conn):
        now = utcnow()
        q = ("SELECT * FROM tasks WHERE status='pending' AND next_run_at<=? "
             + ("AND kind=? " if kind else "") + "ORDER BY next_run_at, id LIMIT 1")
        row = conn.execute(q, (now, kind) if kind else (now,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE tasks SET status='running', attempts=attempts+1, started_at=?, lease_until=?, run_id=? "
            "WHERE id=?", (now, _utc_in(LEASE_SECONDS), run_id, row["id"]))
        return conn.execute("SELECT * FROM tasks WHERE id=?", (row["id"],)).fetchone()


def complete(conn: sqlite3.Connection, task_id: int, result: dict | None = None) -> None:
    conn.execute(
        "UPDATE tasks SET status='done', finished_at=?, lease_until=NULL, last_error=NULL, result_json=? "
        "WHERE id=?", (utcnow(), json.dumps(result) if result is not None else None, task_id))


def fail(conn: sqlite3.Connection, task_id: int, error: str) -> str:
    """Record a failure; schedule a retry with exponential backoff or mark dead."""
    from .log import redact
    row = conn.execute("SELECT attempts, max_attempts FROM tasks WHERE id=?", (task_id,)).fetchone()
    dead = row["attempts"] >= row["max_attempts"]
    status = "dead" if dead else "pending"
    conn.execute(
        "UPDATE tasks SET status=?, last_error=?, finished_at=?, lease_until=NULL, next_run_at=? WHERE id=?",
        (status, redact(error)[:2000], utcnow(), _utc_in(BACKOFF_BASE * 3 ** row["attempts"]), task_id))
    return status


def reclaim_stale(conn: sqlite3.Connection) -> int:
    """Return tasks whose lease expired (crashed runner) to the queue."""
    cur = conn.execute(
        "UPDATE tasks SET status='pending', lease_until=NULL, "
        "last_error=COALESCE(last_error,'') || ' [lease expired]' "
        "WHERE status='running' AND lease_until < ?", (utcnow(),))
    return cur.rowcount


def log_event(conn: sqlite3.Connection, level: str, component: str, message: str,
              video_id: int | None = None) -> None:
    from .log import redact
    conn.execute("INSERT INTO events(level, component, video_id, message) VALUES (?,?,?,?)",
                 (level, component, video_id, redact(message)[:2000]))
