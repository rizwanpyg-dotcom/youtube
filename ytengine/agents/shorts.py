"""Shorts: a 35-58 s vertical cut built from a finished long-form video's verified claims.

A Short is a trailer for its long-form video (one striking fact, then "full story on the channel"),
not a templated clone. It reuses the parent's verified claims, so no new facts are introduced,
and it passes the same script/audio/video QC gates (plus a hard 60 s limit).
"""
from __future__ import annotations

import json
import sqlite3

from .. import db, llm, textutil
from ..log import get as get_log
from . import scriptwriter

log = get_log("shorts")

SHORT_WORDS = (85, 125)  # ~40-55 s at the narrator's measured ~136 wpm including pauses

SHORT_SCHEMA = {
    "type": "object", "required": ["title_working", "acts"],
    "properties": {"title_working": {"type": "string"},
                   "acts": {"type": "array", "minItems": 1, "items": scriptwriter.SCRIPT_SCHEMA["properties"]["acts"]["items"]}},
}


def create_children(conn: sqlite3.Connection, per_week: int) -> int:
    """Create a Short for each finished long-form video that has none, within the weekly limit."""
    week_count = conn.execute("SELECT COUNT(*) FROM videos WHERE format='short' AND created_at >= "
                              "strftime('%Y-%m-%dT%H:%M:%SZ','now','-7 days')").fetchone()[0]
    made = 0
    parents = conn.execute(
        "SELECT p.* FROM videos p WHERE p.format='long' AND p.stage IN ('package_ready','scheduled','published') "
        "AND NOT EXISTS (SELECT 1 FROM videos c WHERE c.parent_video_id=p.id) ORDER BY p.id").fetchall()
    for p in parents:
        if week_count + made >= per_week:
            break
        slot = conn.execute("SELECT * FROM calendar WHERE format='short' AND status='open' AND slot_at > ? "
                            "ORDER BY slot_at LIMIT 1", (p["publish_slot"] or db.utcnow(),)).fetchone()
        cur = conn.execute("INSERT INTO videos(topic_id, format, parent_video_id, publish_slot) VALUES (?,?,?,?)",
                           (p["topic_id"], "short", p["id"], slot["slot_at"] if slot else None))
        if slot:
            conn.execute("UPDATE calendar SET video_id=?, status='assigned' WHERE id=?", (cur.lastrowid, slot["id"]))
        made += 1
        log.info("short %d created from long-form %d", cur.lastrowid, p["id"])
    return made


def inherit_research(conn: sqlite3.Connection, video_id: int) -> dict:
    """Copy the parent's verified claims (and their evidence links) to the Short."""
    v = db.video(conn, video_id)
    parent = v["parent_video_id"]
    conn.execute("DELETE FROM claims WHERE video_id=?", (video_id,))
    n = 0
    for c in conn.execute("SELECT * FROM claims WHERE video_id=? AND status='verified'", (parent,)).fetchall():
        cur = conn.execute("INSERT INTO claims(video_id, ref, text, claim_type, status, confidence, notes) "
                           "VALUES (?,?,?,?,?,?,?)", (video_id, c["ref"], c["text"], c["claim_type"], c["status"],
                                                      c["confidence"], f"inherited from video {parent}"))
        conn.execute("INSERT INTO claim_sources(claim_id, source_id, stance, evidence_quote, quote_verified) "
                     "SELECT ?, source_id, stance, evidence_quote, quote_verified FROM claim_sources WHERE claim_id=?",
                     (cur.lastrowid, c["id"]))
        n += 1
    db.set_stage(conn, video_id, "fact_checked")
    return {"inherited_claims": n, "parent": parent}


def write(conn: sqlite3.Connection, video_id: int, feedback: str | None = None) -> int:
    v = db.video(conn, video_id)
    parent_script = json.loads(scriptwriter.latest(conn, v["parent_video_id"])["outline_json"])
    claims = scriptwriter._claims_block(conn, video_id)
    script, provider = llm.complete_json(conn, "short_script", f"""Write a YouTube Short (vertical, 35-55 seconds,
{SHORT_WORDS[0]}-{SHORT_WORDS[1]} words) that teases this documentary: "{parent_script['title_working']}".
Open with the single most striking verified number or fact in the first sentence. 2-4 paragraphs, one scene each.
End with: "The full story is on the channel."
{scriptwriter._style_rules()}
Each paragraph is one on-screen scene (6-20 seconds). Scene types (shown vertically, keep simple):
 card:  {{"style": "number|statement|quote|title", "eyebrow": str, "big": str, "small": str}}
 chart: {{"series": FRED id, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "title": str, "subtitle": str,
          "highlights": [{{"date": "YYYY-MM-DD", "label": str}}], "source": str}}
On-screen text may only contain numbers from the paragraph's claims.
{"Reviewer feedback to address: " + feedback if feedback else ""}
Return {{"title_working": str, "acts": [{{"name": str, "paragraphs": [{{"text": str, "claims": [refs],
"scene": {{"type": "card|chart", "spec": {{...}}}}}}]}}]}}

Claims:
{claims}""", SHORT_SCHEMA, temperature=0.7)
    body = scriptwriter.render_body(script)
    wc = textutil.words(scriptwriter.narration_text(script))
    version = (conn.execute("SELECT MAX(version) FROM scripts WHERE video_id=?", (video_id,)).fetchone()[0] or 0) + 1
    conn.execute("UPDATE scripts SET status='superseded' WHERE video_id=? AND status IN ('draft','qc_failed')",
                 (video_id,))
    cur = conn.execute(
        "INSERT INTO scripts(video_id, version, outline_json, body, word_count, est_duration_s, llm_provider) "
        "VALUES (?,?,?,?,?,?,?)", (video_id, version, json.dumps(script), body, wc, wc / 136 * 60, provider))
    conn.execute("UPDATE videos SET working_title=? WHERE id=?", (script["title_working"], video_id))
    db.set_stage(conn, video_id, "scripted")
    log.info("short script v%d: %d words via %s", version, wc, provider)
    return cur.lastrowid
