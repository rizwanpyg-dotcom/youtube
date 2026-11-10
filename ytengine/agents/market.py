"""Agent 1: market research for the niche and its sub-categories.

With YouTube API access: search.list (100 units each, capped by config youtube.search_calls_per_day)
plus videos.list/channels.list (1 unit each) give demand (median views/day of results < 3 years
old), competition (share of results from channels with more than 500k subscribers) and gap.
Without it: Wikipedia pageviews only (demand), clearly marked as partial evidence.
The agent RECOMMENDS. A niche change is a 'policy' approval for the owner; it never applies one.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from datetime import datetime, timezone

from .. import config, quota
from ..log import get as get_log
from . import topics

log = get_log("market")

SEEDS = {
    "bank-panic": ["bank run history documentary", "panic of 1907"],
    "currency-crisis": ["currency crisis explained", "hyperinflation history"],
    "speculative-bubble": ["history of market bubbles", "tulip mania explained"],
    "tech-failure": ["failed tech companies documentary", "dot com bubble explained"],
    "corporate-fraud": ["corporate fraud documentary", "enron collapse explained"],
    "industrial-decline": ["why detroit declined", "rise and fall of industry documentary"],
    "sovereign-default": ["country default explained", "argentina default history"],
    "commodity-shock": ["oil shock 1973 explained", "commodity crash history"],
    "failed-standard": ["format war history", "betamax vs vhs"],
    "monetary-experiment": ["gold standard history", "john law mississippi bubble"],
}
WIKI_PROBES = {
    "bank-panic": "Panic of 1907", "currency-crisis": "Hyperinflation in the Weimar Republic",
    "speculative-bubble": "Tulip mania", "tech-failure": "Dot-com bubble", "corporate-fraud": "Enron scandal",
    "industrial-decline": "Decline of Detroit", "sovereign-default": "Argentine debt crisis",
    "commodity-shock": "1973 oil crisis", "failed-standard": "Videotape format war",
    "monetary-experiment": "Mississippi Company",
}


def _youtube_category(conn, yt, query: str) -> dict:
    quota.youtube(conn, "search.list")
    res = yt.search().list(part="snippet", q=query, type="video", maxResults=20, order="relevance",
                           relevanceLanguage="en", videoDuration="medium").execute()
    ids = [i["id"]["videoId"] for i in res.get("items", [])]
    if not ids:
        return {}
    quota.youtube(conn, "videos.list")
    vids = yt.videos().list(part="statistics,snippet", id=",".join(ids)).execute()["items"]
    ch_ids = sorted({v["snippet"]["channelId"] for v in vids})
    quota.youtube(conn, "channels.list")
    chans = {c["id"]: int(c["statistics"].get("subscriberCount", 0))
             for c in yt.channels().list(part="statistics", id=",".join(ch_ids)).execute()["items"]}
    now = datetime.now(timezone.utc)
    rates, big, recent, ages = [], 0, 0, []
    for v in vids:
        pub = datetime.fromisoformat(v["snippet"]["publishedAt"].replace("Z", "+00:00"))
        days = max(1, (now - pub).days)
        ages.append(days)
        if days < 3 * 365:
            recent += 1
            rates.append(int(v["statistics"].get("viewCount", 0)) / days)
        if chans.get(v["snippet"]["channelId"], 0) > 500_000:
            big += 1
    return {"median_views_per_day": statistics.median(rates) if rates else 0, "big_channel_share": big / len(vids),
            "recent_share": recent / len(vids), "newest_top_result_days": min(ages)}


def run(conn: sqlite3.Connection) -> dict:
    budget = int(config.get().get("youtube.search_calls_per_day", 15))
    yt = None
    try:
        from ..youtube.auth import service
        yt = service()
    except Exception as e:
        log.info("YouTube API unavailable (%s); using Wikipedia demand only", type(e).__name__)
    evidence: dict = {"measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "method": "youtube+wikipedia" if yt else "wikipedia-only (partial)", "categories": {}}
    gap: dict[str, float] = {}
    for cat, seeds in SEEDS.items():
        e = {"wikipedia_views_12m": topics.pageviews(WIKI_PROBES[cat])}
        if yt and budget > 0:
            try:
                e["youtube"] = _youtube_category(conn, yt, seeds[0])
                budget -= 1
            except quota.QuotaExceeded:
                yt = None
        y = e.get("youtube")
        if y:
            demand = min(1.0, y["median_views_per_day"] / 2000)
            competition = 1 - y["big_channel_share"]
            staleness = min(1.0, y["newest_top_result_days"] / 365)
            gap[cat] = round(0.5 * demand + 0.3 * competition + 0.2 * staleness, 3)
        evidence["categories"][cat] = e
    if gap:
        evidence["category_gap"] = gap
    conn.execute("UPDATE niches SET evidence_json=? WHERE status='active'", (json.dumps(evidence),))
    log.info("market research done (%s); gaps=%s", evidence["method"], gap)
    return evidence
