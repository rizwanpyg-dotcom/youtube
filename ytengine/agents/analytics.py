"""Agent 9: performance tracking, YPP progress and the learning loop.

Data: YouTube Analytics API (per-video daily metrics) + Data API (channel totals).
Learning: at day 7 each published video earns a reward
    0.5 * percentile(views in first 7 days) + 0.5 * percentile(average view percentage)
among the channel's own videos, which updates the bandit arms for its category, thumbnail
style and title pattern. Insights are only acted on once an arm has >= 4 videos.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from .. import db, quota
from ..log import get as get_log
from . import strategy

log = get_log("analytics")

METRICS = ("views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,"
           "subscribersGained,subscribersLost")


def _analytics_service():
    from ..youtube.auth import service
    return service("youtubeAnalytics", "v2")


def collect(conn: sqlite3.Connection, days: int = 30) -> dict:
    try:
        ya = _analytics_service()
        from ..youtube.auth import service
        yt = service()
    except Exception as e:
        log.info("analytics skipped: %s", e)
        return {"skipped": str(e)}
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days)
    n = 0
    for v in conn.execute("SELECT id, youtube_video_id FROM videos WHERE youtube_video_id IS NOT NULL").fetchall():
        quota.consume(conn, "youtube_analytics", 1)
        rep = ya.reports().query(ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
                                 metrics=METRICS, dimensions="day", filters=f"video=={v['youtube_video_id']}").execute()
        cols = [h["name"] for h in rep.get("columnHeaders", [])]
        for row in rep.get("rows", []):
            r = dict(zip(cols, row))
            conn.execute(
                "INSERT INTO analytics_daily(video_id, day, views, watch_minutes, avg_view_duration, avg_view_pct, "
                "likes, comments, subs_gained, subs_lost) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(video_id, day) DO UPDATE SET views=excluded.views, watch_minutes=excluded.watch_minutes, "
                "avg_view_duration=excluded.avg_view_duration, avg_view_pct=excluded.avg_view_pct, "
                "likes=excluded.likes, comments=excluded.comments, subs_gained=excluded.subs_gained, "
                "subs_lost=excluded.subs_lost",
                (v["id"], r["day"], r.get("views"), r.get("estimatedMinutesWatched"), r.get("averageViewDuration"),
                 r.get("averageViewPercentage"), r.get("likes"), r.get("comments"), r.get("subscribersGained"),
                 r.get("subscribersLost")))
            n += 1
    # Channel totals and YPP-relevant rolling figures.
    quota.youtube(conn, "channels.list")
    ch = yt.channels().list(part="statistics", mine=True).execute()["items"][0]["statistics"]
    quota.consume(conn, "youtube_analytics", 1)
    yr = ya.reports().query(ids="channel==MINE", startDate=(end - timedelta(days=364)).isoformat(),
                            endDate=end.isoformat(), metrics="estimatedMinutesWatched").execute()
    minutes = (yr.get("rows") or [[0]])[0][0]
    shorts_views = None
    try:  # creatorContentType dimension: availability to be confirmed (Phase 4 open item)
        quota.consume(conn, "youtube_analytics", 1)
        sr = ya.reports().query(ids="channel==MINE", startDate=(end - timedelta(days=89)).isoformat(),
                                endDate=end.isoformat(), metrics="views", dimensions="creatorContentType").execute()
        shorts_views = sum(r[1] for r in sr.get("rows", []) if str(r[0]).upper() == "SHORTS")
    except Exception as e:
        log.info("shorts split unavailable: %s", e)
    conn.execute("INSERT OR REPLACE INTO channel_daily(day, subscribers, total_views, video_count, watch_hours_365d, "
                 "shorts_views_90d) VALUES (?,?,?,?,?,?)",
                 (date.today().isoformat(), int(ch.get("subscriberCount", 0)), int(ch.get("viewCount", 0)),
                  int(ch.get("videoCount", 0)), minutes / 60, shorts_views))
    log.info("analytics: %d video-day rows; %s subs; %.0f watch hours (365d, all content: approximates YPP)",
             n, ch.get("subscriberCount"), minutes / 60)
    return {"rows": n}


def _percentile(value: float, population: list[float]) -> float:
    if not population:
        return 0.5
    return sum(1 for p in population if p <= value) / len(population)


def learn(conn: sqlite3.Connection) -> list[str]:
    """Reward videos that are >= 7 days old and not yet learned from; then write insights."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = {}
    for v in conn.execute("SELECT v.id, v.published_at FROM videos v WHERE v.published_at IS NOT NULL").fetchall():
        rows = conn.execute("SELECT SUM(views) views, AVG(avg_view_pct) pct FROM analytics_daily WHERE video_id=? "
                            "AND day <= date(?, '+7 days')", (v["id"], v["published_at"][:10])).fetchone()
        if rows["views"] is not None:
            stats[v["id"]] = (rows["views"], rows["pct"] or 0)
    views_pop = [s[0] for s in stats.values()]
    pct_pop = [s[1] for s in stats.values()]
    learned = []
    for v in conn.execute("SELECT v.id, t.category, v.format FROM videos v JOIN topics t ON t.id=v.topic_id "
                          "WHERE v.published_at IS NOT NULL AND v.published_at <= ?", (cutoff,)).fetchall():
        key = f"learned:{v['id']}"
        if db.get_setting(conn, key) or v["id"] not in stats:
            continue
        views, pct = stats[v["id"]]
        reward = 0.5 * _percentile(views, views_pop) + 0.5 * _percentile(pct, pct_pop)
        strategy.update(conn, "category", v["category"], reward)
        thumb = conn.execute("SELECT style FROM thumbnails WHERE video_id=? AND selected=1", (v["id"],)).fetchone()
        if thumb:
            strategy.update(conn, "thumbnail_style", thumb["style"], reward)
        pattern = db.get_setting(conn, f"title_pattern:{v['id']}")
        if pattern:
            strategy.update(conn, "title_pattern", pattern, reward)
        db.set_setting(conn, key, round(reward, 3))
        learned.append(f"video {v['id']} reward {reward:.2f}")
    _insights(conn)
    return learned


def _insights(conn: sqlite3.Connection) -> None:
    for dim in ("category", "thumbnail_style", "title_pattern"):
        arms = conn.execute("SELECT value, alpha, beta, n_videos FROM strategy_arms WHERE dimension=? AND n_videos>=4",
                            (dim,)).fetchall()
        if len(arms) < 2:
            continue
        ranked = sorted(arms, key=lambda a: a["alpha"] / (a["alpha"] + a["beta"]), reverse=True)
        best, worst = ranked[0], ranked[-1]
        mb, mw = best["alpha"] / (best["alpha"] + best["beta"]), worst["alpha"] / (worst["alpha"] + worst["beta"])
        if mb - mw < 0.1:
            continue
        finding = f"{dim}: '{best['value']}' outperforms '{worst['value']}' ({mb:.2f} vs {mw:.2f} mean reward)"
        if not conn.execute("SELECT 1 FROM insights WHERE finding=?", (finding,)).fetchone():
            kind = {"category": "topic", "thumbnail_style": "thumbnail", "title_pattern": "title"}[dim]
            conn.execute("INSERT INTO insights(kind, finding, evidence, applied) VALUES (?,?,?,1)",
                         (kind, finding, json.dumps({"best": dict(best), "worst": dict(worst)})))
