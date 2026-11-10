"""Operations: run records, watchdog health checks, backups, storage housekeeping."""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, db, notify, quota
from .log import get as get_log

log = get_log("ops")


# ------------------------------------------------------------------ run records
class RunRecorder:
    def __init__(self, conn: sqlite3.Connection, workflow: str):
        self.conn, self.workflow, self.t0 = conn, workflow, time.time()
        self.run_id = os.environ.get("GITHUB_RUN_ID", f"local-{int(self.t0)}")
        self.attempt = int(os.environ.get("GITHUB_RUN_ATTEMPT", "1"))
        conn.execute("INSERT OR REPLACE INTO runs(workflow, run_id, attempt, started_at, status) VALUES (?,?,?,?,?)",
                     (workflow, self.run_id, self.attempt, db.utcnow(), "running"))

    def finish(self, status: str, summary: str = "") -> None:
        try:
            import resource  # POSIX only
            ru = resource.getrusage(resource.RUSAGE_CHILDREN)
            ru_self = resource.getrusage(resource.RUSAGE_SELF)
            cpu = ru.ru_utime + ru.ru_stime + ru_self.ru_utime + ru_self.ru_stime
            peak = max(ru.ru_maxrss, ru_self.ru_maxrss) / 1024  # KB -> MB on Linux
        except ImportError:
            cpu, peak = None, None
        disk = shutil.disk_usage(config.get().paths.work)
        self.conn.execute(
            "UPDATE runs SET finished_at=?, status=?, cpu_seconds=?, peak_rss_mb=?, disk_used_mb=?, summary=? "
            "WHERE run_id=? AND attempt=?",
            (db.utcnow(), status, cpu, peak, (disk.total - disk.free) / 1e6, summary[:1000], self.run_id, self.attempt))


# ------------------------------------------------------------------ watchdog
def _age_hours(ts: str | None) -> float:
    if not ts:
        return 1e9
    t = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600


def health(conn: sqlite3.Connection, check_oauth: bool = True) -> list[dict]:
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        conn.execute("INSERT INTO health_checks(check_name, ok, detail) VALUES (?,?,?)", (name, int(ok), detail))

    reclaimed = db.reclaim_stale(conn)
    add("stale_leases", True, f"{reclaimed} reclaimed")
    last = conn.execute("SELECT MAX(finished_at) FROM runs WHERE workflow='pipeline' AND status='success'").fetchone()[0]
    add("pipeline_recent", _age_hours(last) < 8, f"last success {last or 'never'}")
    dead = conn.execute("SELECT COUNT(*) FROM tasks WHERE status='dead'").fetchone()[0]
    add("dead_tasks", dead == 0, f"{dead} dead tasks")
    integ = conn.execute("PRAGMA integrity_check").fetchone()[0]
    add("db_integrity", integ == "ok", integ)
    yt_left = quota.remaining(conn, "youtube_data")
    add("youtube_quota", yt_left > 2000, f"{yt_left} units left today")
    free_gb = shutil.disk_usage(config.get().paths.work).free / 1e9
    add("disk_free", free_gb > 3, f"{free_gb:.1f} GB free")
    size_mb = os.path.getsize(config.get().paths.db) / 1e6 if os.path.exists(config.get().paths.db) else 0
    add("db_size", size_mb < 100, f"{size_mb:.1f} MB")
    if check_oauth and config.Config.secret("YT_REFRESH_TOKEN"):
        try:
            from .youtube.auth import credentials
            credentials()
            add("youtube_oauth", True, "refresh token valid")
        except Exception as e:  # revoked/expired token
            add("youtube_oauth", False, f"refresh failed: {type(e).__name__}")
    else:
        add("youtube_oauth", True, "not configured (package mode)")
    return checks


def watchdog(conn: sqlite3.Connection) -> list[dict]:
    checks = health(conn)
    failing = [c for c in checks if not c["ok"]]
    title = "[watchdog] System health problem"
    if failing:
        body = "\n".join(f"- **{c['check']}**: {c['detail']}" for c in failing)
        existing = notify.find_open(title)
        if existing:
            notify.comment(existing, f"Still failing at {db.utcnow()}:\n{body}")
        else:
            notify.open_issue(title, f"The watchdog found problems at {db.utcnow()}:\n\n{body}\n\n"
                              "See docs/SECURITY.md → Recovery runbook.", ["watchdog"])
    else:
        existing = notify.find_open(title)
        if existing:
            notify.comment(existing, f"All checks healthy again at {db.utcnow()}.")
            notify.close(existing)
    prune(conn)
    db.set_setting(conn, "heartbeat", db.utcnow())  # also keeps the repo "active" (60-day rule)
    log.info("watchdog: %d checks, %d failing", len(checks), len(failing))
    return checks


def prune(conn: sqlite3.Connection, keep_days: int = 90) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM health_checks WHERE ts < ?", (cutoff,))
    conn.execute("UPDATE sources SET excerpt=NULL WHERE accessed_at < ? AND excerpt IS NOT NULL",
                 ((datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ"),))


# ------------------------------------------------------------------ backup
def backup(conn: sqlite3.Connection, out_dir: Path) -> Path:
    """Consistent online snapshot (VACUUM INTO), gzip-compressed, integrity-checked."""
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    raw = out_dir / f"youtube_automation-{stamp}.db"
    conn.execute("VACUUM INTO ?", (str(raw),))
    check = _integrity(raw)
    if check != "ok":
        raise RuntimeError(f"backup integrity check failed: {check}")
    gz = raw.with_suffix(".db.gz")
    with open(raw, "rb") as f, gzip.open(gz, "wb", compresslevel=9) as g:
        shutil.copyfileobj(f, g)
    raw.unlink()
    log.info("backup %s (%.1f KB)", gz.name, gz.stat().st_size / 1024)
    return gz


def _integrity(path: Path) -> str:
    c = sqlite3.connect(path)
    try:
        return c.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        return f"not a database: {e}"
    finally:
        c.close()  # Windows keeps open files locked; always close before unlink/replace


def restore(gz: Path, target: Path) -> None:
    tmp = target.with_suffix(".restoring")
    with gzip.open(gz, "rb") as g, open(tmp, "wb") as f:
        shutil.copyfileobj(g, f)
    if _integrity(tmp) != "ok":
        tmp.unlink()
        raise RuntimeError("backup is corrupt")
    for suffix in ("-wal", "-shm"):
        Path(str(target) + suffix).unlink(missing_ok=True)
    tmp.replace(target)
