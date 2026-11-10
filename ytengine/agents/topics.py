"""Agent 2: topic research, scoring, de-duplication and the content calendar.

Signals (all free):
  demand              Wikipedia pageviews, last 12 months (Wikimedia REST API, no key)
  source_availability count of tier-1/2 references on the topic's Wikipedia page(s)
  evergreen           LLM judgement, 0..1
  gap                 Agent 1's YouTube supply/demand ratio for the category when available, else 0.5
  bandit              Thompson-sampled preference for the category (learned from analytics)
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone

import requests

from .. import config, db, llm, sources, textutil
from ..log import get as get_log
from . import strategy

log = get_log("topics")

CATEGORIES = ["bank-panic", "currency-crisis", "speculative-bubble", "tech-failure", "corporate-fraud",
              "industrial-decline", "sovereign-default", "commodity-shock", "failed-standard", "monetary-experiment"]

TOPICS_SCHEMA = {"type": "object", "required": ["topics"], "properties": {"topics": {
    "type": "array", "minItems": 1, "items": {
        "type": "object", "required": ["title", "category", "angle", "wikipedia", "keywords", "evergreen"],
        "properties": {"title": {"type": "string"}, "category": {"type": "string", "enum": CATEGORIES},
                       "wikipedia": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                       "evergreen": {"type": "number"}}}}}}

PAGEVIEWS = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
             "{title}/monthly/{start}/{end}")
_session = requests.Session()
_session.headers["User-Agent"] = sources.USER_AGENT


def pageviews(title: str, months: int = 12) -> int:
    end = date.today().replace(day=1) - timedelta(days=1)
    start = (end - timedelta(days=31 * months)).replace(day=1)
    url = PAGEVIEWS.format(title=requests.utils.quote(title.replace(" ", "_"), safe=""),
                           start=start.strftime("%Y%m%d"), end=end.strftime("%Y%m%d"))
    r = sources.api_get(url)
    if r.status_code == 404:
        return 0
    r.raise_for_status()
    return sum(i["views"] for i in r.json().get("items", []))


def _demand_score(views: int) -> float:
    # 12-month views: 10k -> 0.25, 100k -> 0.5, 1M -> 0.75, 10M -> 1.0
    return max(0.0, min(1.0, (math.log10(max(views, 1)) - 3) / 4))


def _availability(titles: list[str]) -> tuple[float, int]:
    good = 0
    for t in titles[:2]:
        try:
            good += sum(1 for u in sources.rank_references(sources.wikipedia_references(t)) if sources.tier(u) <= 2)
        except Exception:
            pass
    return min(1.0, good / 12), good


def _norm_wiki(title: str) -> str:
    return title.strip().replace("_", " ").lower()


def _is_duplicate(conn: sqlite3.Connection, slug: str, fp: set[str], wiki: list[str] | None = None) -> bool:
    """Duplicate if: same slug, near-identical title keywords, or the same primary Wikipedia article."""
    if conn.execute("SELECT 1 FROM topics WHERE slug=?", (slug,)).fetchone():
        return True
    wanted = {_norm_wiki(w) for w in (wiki or [])}
    for row in conn.execute("SELECT fingerprint, evidence_json FROM topics"):
        if fp and textutil.jaccard(fp, set(row["fingerprint"].split())) > 0.6:
            return True
        if wanted:
            plan = json.loads(row["evidence_json"] or "{}").get("research_plan", {})
            if wanted & {_norm_wiki(w) for w in plan.get("wikipedia", [])}:
                return True
    return False


def research(conn: sqlite3.Connection, n: int = 12) -> dict:
    existing = [r["title"] for r in conn.execute("SELECT title FROM topics ORDER BY id DESC LIMIT 200")]
    weights = {c: strategy.choose(conn, "category", CATEGORIES) for c in range(3)}
    proposal, provider = llm.complete_json(conn, "topics", f"""Niche: {config.get().get('channel.niche')}
Audience: {config.get().get('channel.audience')}
Propose {n} documentary topics: specific historical episodes (an event, company, panic or policy), not generic themes.
Each needs a distinct angle (the "why it happened" insight), 1-2 exact English Wikipedia article titles that exist,
search keywords, an evergreen score 0..1, optional FRED data series that would chart it
({{"series": id, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}}), and up to 4 "extra_urls": exact URLs of
authoritative pages you are confident exist (e.g. federalreservehistory.org essays, central-bank or government
reports, britannica.com). Every page is fetched and verified; wrong URLs are simply discarded.
Prefer these categories this round: {sorted(set(weights.values()))}. Allowed categories: {CATEGORIES}.
Avoid anything similar to: {existing[:80]}
Return {{"topics": [{{"title","category","era","region","angle","wikipedia":[...],"fred":[...],"keywords":[...],"evergreen"}}]}}""",
                                           TOPICS_SCHEMA, temperature=0.9)
    added, skipped = 0, []
    gap_by_cat = _gap_scores(conn)
    for t in proposal["topics"]:
        slug = textutil.slugify(t["title"])
        fp = textutil.keywords(t["title"])  # title only: angles are long and vary between proposals
        if _is_duplicate(conn, slug, fp, t["wikipedia"]):
            skipped.append((t["title"], "duplicate"))
            continue
        try:
            views = sum(pageviews(w) for w in t["wikipedia"][:2])
        except Exception as e:
            skipped.append((t["title"], f"pageviews: {e}"))
            continue
        if views == 0:
            skipped.append((t["title"], "wikipedia page not found"))
            continue
        avail, n_refs = _availability(t["wikipedia"])
        if n_refs < 3:
            skipped.append((t["title"], f"only {n_refs} tier-1/2 references"))
            continue
        demand = _demand_score(views)
        gap = gap_by_cat.get(t["category"], 0.5)
        bandit = _category_mean(conn, t["category"])
        final = 0.35 * demand + 0.25 * gap + 0.15 * float(t["evergreen"]) + 0.15 * avail + 0.10 * bandit
        plan = {"wikipedia": t["wikipedia"][:2], "fred": t.get("fred") or [], "keywords": t.get("keywords", [])[:10],
                "extra_urls": [u for u in (t.get("extra_urls") or [])[:4] if str(u).startswith("https://")]}
        conn.execute(
            "INSERT INTO topics(slug, fingerprint, title, category, era, region, angle, demand_score, "
            "competition_score, gap_score, final_score, evidence_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (slug, " ".join(sorted(fp)), t["title"], t["category"], t.get("era"), t.get("region"), t["angle"],
             demand, None, gap, round(final, 4),
             json.dumps({"research_plan": plan, "pageviews_12m": views, "tier12_refs": n_refs,
                         "evergreen": t["evergreen"], "provider": provider})))
        added += 1
    log.info("topic research: %d added, %d skipped %s", added, len(skipped), skipped[:5])
    return {"added": added, "skipped": skipped}


def _gap_scores(conn) -> dict[str, float]:
    row = conn.execute("SELECT evidence_json FROM niches WHERE status='active' LIMIT 1").fetchone()
    if not row:
        return {}
    return json.loads(row["evidence_json"]).get("category_gap", {})


def _category_mean(conn, category: str) -> float:
    r = conn.execute("SELECT alpha, beta FROM strategy_arms WHERE dimension='category' AND value=?",
                     (category,)).fetchone()
    return r["alpha"] / (r["alpha"] + r["beta"]) if r else 0.5


# ------------------------------------------------------------------ calendar
_DAYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def slots(weeks: int = 3, now: datetime | None = None) -> list[tuple[str, str]]:
    cfg = config.get()
    now = now or datetime.now(timezone.utc)
    out = []
    for fmt, key in (("long", "long_form"), ("short", "shorts")):
        days = [_DAYS[d] for d in cfg.get(f"schedule.{key}.publish_days", [])]
        hh, mm = map(int, cfg.get(f"schedule.{key}.publish_time", "14:00").split(":"))
        for d in range(weeks * 7):
            day = (now + timedelta(days=d)).date()
            if day.weekday() in days:
                at = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)
                if at > now:
                    out.append((at.strftime("%Y-%m-%dT%H:%M:%SZ"), fmt))
    return sorted(out)


def fill_calendar(conn: sqlite3.Connection, weeks: int = 3) -> dict:
    """Create calendar slots; assign long-form slots inside the production window to the best topics."""
    lead = int(config.get().get("schedule.production_lead_days", 3))
    for at, fmt in slots(weeks):
        conn.execute("INSERT OR IGNORE INTO calendar(slot_at, format) VALUES (?,?)", (at, fmt))
    window_end = (datetime.now(timezone.utc) + timedelta(days=lead + 4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assigned = 0
    last_cat = conn.execute("SELECT t.category FROM videos v JOIN topics t ON t.id=v.topic_id "
                            "WHERE v.format='long' ORDER BY v.id DESC LIMIT 1").fetchone()
    last_cat = last_cat["category"] if last_cat else None
    for slot in conn.execute("SELECT * FROM calendar WHERE status='open' AND format='long' AND slot_at<=? "
                             "ORDER BY slot_at", (window_end,)).fetchall():
        topic = conn.execute("SELECT * FROM topics WHERE status='candidate' AND category IS NOT ? "
                             "ORDER BY final_score DESC LIMIT 1", (last_cat,)).fetchone() or \
            conn.execute("SELECT * FROM topics WHERE status='candidate' ORDER BY final_score DESC LIMIT 1").fetchone()
        if topic is None:
            break
        cur = conn.execute("INSERT INTO videos(topic_id, format, publish_slot) VALUES (?, 'long', ?)",
                           (topic["id"], slot["slot_at"]))
        conn.execute("UPDATE calendar SET video_id=?, status='assigned' WHERE id=?", (cur.lastrowid, slot["id"]))
        conn.execute("UPDATE topics SET status='in_production' WHERE id=?", (topic["id"],))
        last_cat = topic["category"]
        assigned += 1
    conn.execute("UPDATE calendar SET status='skipped' WHERE status='open' AND slot_at < ?", (db.utcnow(),))
    return {"assigned": assigned}


def backlog(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM topics WHERE status='candidate'").fetchone()[0]
