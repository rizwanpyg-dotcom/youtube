"""Per-provider daily usage counters with self-imposed hard caps.

Caps sit below each provider's free limit, so the system stops before any limit,
and no billing account exists, so stopping is the only possible outcome.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config


class QuotaExceeded(RuntimeError):
    pass


# YouTube quota resets at midnight Pacific time; others are treated as UTC days.
_RESET_TZ = {"youtube_data": "America/Los_Angeles", "youtube_analytics": "America/Los_Angeles"}

# Approximate unit costs. videos.insert was reported cut from 1,600 to ~100 in Dec 2025;
# we budget the old, higher figure until confirmed on Google's quota page.
YOUTUBE_COSTS = {
    "videos.insert": 1600, "thumbnails.set": 50, "captions.insert": 400, "videos.update": 50,
    "videos.list": 1, "channels.list": 1, "search.list": 100, "playlistItems.list": 1,
}


def _day(provider: str) -> str:
    tz = _RESET_TZ.get(provider)
    now = datetime.now(ZoneInfo(tz)) if tz else datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d")


def default_cap(provider: str, model: str | None = None) -> int:
    cfg = config.get()
    if provider == "youtube_data":
        return int(cfg.get("youtube.data_api_daily_cap_units", 8000))
    if provider == "youtube_analytics":
        return 5000
    caps = [p.get("daily_request_cap", 100) for p in cfg.get("llm.providers", [])
            if p["name"] == provider and (model is None or p.get("model") == model)]
    return int(sum(caps) or 100)


def remaining(conn: sqlite3.Connection, provider: str, cap: int | None = None) -> int:
    cap = cap if cap is not None else default_cap(provider)
    row = conn.execute("SELECT units FROM api_usage WHERE provider=? AND day=?",
                       (provider, _day(provider))).fetchone()
    return cap - (row["units"] if row else 0)


def consume(conn: sqlite3.Connection, provider: str, units: int = 1, cap: int | None = None,
            error: bool = False) -> None:
    """Reserve units before a call. Raises QuotaExceeded instead of going over the cap."""
    cap = cap if cap is not None else default_cap(provider)
    day = _day(provider)
    conn.execute("INSERT OR IGNORE INTO api_usage(provider, day, daily_cap) VALUES (?,?,?)",
                 (provider, day, cap))
    cur = conn.execute(
        "UPDATE api_usage SET units=units+?, requests=requests+1, errors=errors+? "
        "WHERE provider=? AND day=? AND units+? <= daily_cap",
        (units, int(error), provider, day, units))
    if cur.rowcount == 0:
        raise QuotaExceeded(f"{provider}: daily cap {cap} would be exceeded by {units} units")


def youtube(conn: sqlite3.Connection, method: str) -> None:
    consume(conn, "youtube_data", YOUTUBE_COSTS.get(method, 1))
