"""Agent 4: outline + narration built only from verified claims.

Output structure (stored in scripts.outline_json):
  {"title_working": str,
   "acts": [{"name": str,
             "paragraphs": [{"text": str, "claims": ["C1", ...],
                             "scene": {"type": "chart|card|image", "spec": {...}}}]}]}
"""
from __future__ import annotations

import json
import sqlite3

from .. import config, db, llm, textutil
from ..log import get as get_log

log = get_log("scriptwriter")

WPM = 150  # measured later from real narration; used only for estimates

SCRIPT_SCHEMA = {
    "type": "object", "required": ["title_working", "acts"],
    "properties": {
        "title_working": {"type": "string"},
        "acts": {"type": "array", "minItems": 3, "items": {
            "type": "object", "required": ["name", "paragraphs"],
            "properties": {"name": {"type": "string"}, "paragraphs": {"type": "array", "minItems": 1, "items": {
                "type": "object", "required": ["text", "claims", "scene"],
                "properties": {"text": {"type": "string"},
                               "claims": {"type": "array", "items": {"type": "string"}},
                               "scene": {"type": "object", "required": ["type", "spec"],
                                         "properties": {"type": {"type": "string",
                                                                 "enum": ["chart", "card", "image"]}}}}}}}}}},
}

OUTLINE_SCHEMA = {
    "type": "object", "required": ["hook", "acts", "closing_idea"],
    "properties": {"hook": {"type": "string"}, "closing_idea": {"type": "string"},
                   "acts": {"type": "array", "minItems": 3, "items": {"type": "object"}}},
}


def target_words(conn: sqlite3.Connection, video_id: int) -> tuple[int, int]:
    v = db.video(conn, video_id)
    topic = conn.execute("SELECT evidence_json FROM topics WHERE id=?", (v["topic_id"],)).fetchone()
    if v["format"] == "short":
        from .shorts import SHORT_WORDS
        return SHORT_WORDS
    override = json.loads(topic["evidence_json"]).get("target_words")
    if override:
        return int(override[0]), int(override[1])
    cfg = config.get()
    lo, hi = cfg.get("schedule.long_form.target_minutes", [9, 13])
    return int(lo * WPM), int(hi * WPM)


def _claims_block(conn, video_id) -> str:
    rows = conn.execute("SELECT ref, text, status, claim_type FROM claims WHERE video_id=? "
                        "AND status IN ('verified','conflicting') ORDER BY id", (video_id,)).fetchall()
    lines = []
    for r in rows:
        tag = "VERIFIED" if r["status"] == "verified" else "CONFLICTING - only say sources disagree"
        lines.append(f"{r['ref']} [{tag}; {r['claim_type']}]: {r['text']}")
    return "\n".join(lines)


def _style_rules() -> str:
    banned = _banned_phrases()
    return f"""Style rules:
- Calm, precise documentary narrator; explain causes, not just events; no hype words.
- Open with a concrete scene or number within the first 2 sentences.
- Sentences under 30 words. Write every number as digits exactly as in the claim.
- Every sentence with a number, date, name-specific fact or quotation must cite claims in that paragraph's "claims".
- Use ONLY the claims listed. No other facts, statistics, quotes or dates. No invented dialogue.
- Direct quotations must name who said them (e.g. "X said ...").
- Never give investment or financial advice.
- Never use these phrases: {", ".join(banned)}"""


def _banned_phrases() -> list[str]:
    path = config.get().paths.root / config.get().get("quality.banned_phrases_file", "config/banned_phrases.txt")
    if not path.exists():
        return []
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip() and not l.startswith("#")]


def write(conn: sqlite3.Connection, video_id: int, feedback: str | None = None) -> int:
    v = db.video(conn, video_id)
    lo, hi = target_words(conn, video_id)
    claims = _claims_block(conn, video_id)
    outline, _ = llm.complete_json(conn, "outline", f"""Topic: {v['topic_title']}
Angle: {v['angle'] or ''}
Target length: {lo}-{hi} words of narration.
Plan a documentary outline: a hook, 3-6 acts (each with a purpose and the claim refs it uses), a closing idea.
Return {{"hook": str, "acts": [{{"name": str, "purpose": str, "claims": [refs]}}], "closing_idea": str}}

Claims:
{claims}""", OUTLINE_SCHEMA, temperature=0.6)

    script, provider = llm.complete_json(conn, "script", f"""Write the narration for this documentary.
Topic: {v['topic_title']}
Outline: {json.dumps(outline)}
Target length: {lo}-{hi} words.
{_style_rules()}
Each paragraph is one on-screen scene (6-25 seconds). Scene types:
 chart: {{"series": FRED id, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "title": str, "subtitle": str,
          "highlights": [{{"date": "YYYY-MM-DD", "label": str}}], "source": str}}
 card:  {{"style": "number|statement|quote|title", "eyebrow": str, "big": str, "small": str}}
 image: {{"commons_file": "File:...", "caption": str}}
On-screen text may only contain numbers from the paragraph's claims.
{"Reviewer feedback to address: " + feedback if feedback else ""}
Return {{"title_working": str, "acts": [{{"name": str, "paragraphs": [{{"text": str, "claims": [refs],
"scene": {{"type": ..., "spec": {{...}}}}}}]}}]}}

Claims:
{claims}""", SCRIPT_SCHEMA, temperature=float(config.get().get("llm.temperature_script", 0.8)))

    body = render_body(script)
    wc = textutil.words(narration_text(script))
    version = (conn.execute("SELECT MAX(version) FROM scripts WHERE video_id=?", (video_id,)).fetchone()[0] or 0) + 1
    conn.execute("UPDATE scripts SET status='superseded' WHERE video_id=? AND status IN ('draft','qc_failed')",
                 (video_id,))
    cur = conn.execute(
        "INSERT INTO scripts(video_id, version, outline_json, body, word_count, est_duration_s, llm_provider) "
        "VALUES (?,?,?,?,?,?,?)", (video_id, version, json.dumps(script), body, wc, wc / WPM * 60, provider))
    conn.execute("UPDATE videos SET working_title=? WHERE id=?", (script["title_working"], video_id))
    db.set_stage(conn, video_id, "scripted")
    log.info("script v%d: %d words (~%.1f min) via %s", version, wc, wc / WPM, provider)
    return cur.lastrowid


def paragraphs(script: dict) -> list[dict]:
    out = []
    for ai, act in enumerate(script["acts"]):
        for pi, p in enumerate(act["paragraphs"]):
            out.append({**p, "act": act["name"], "act_index": ai, "first_in_act": pi == 0})
    return out


def narration_text(script: dict) -> str:
    return "\n\n".join(p["text"] for p in paragraphs(script))


def render_body(script: dict) -> str:
    lines = []
    for act in script["acts"]:
        lines.append(f"## {act['name']}")
        for p in act["paragraphs"]:
            refs = " ".join(f"[{c}]" for c in p["claims"])
            lines.append(f"[SCENE: {p['scene']['type']}] {p['text']} {refs}".strip())
    return "\n\n".join(lines)


def latest(conn: sqlite3.Connection, video_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM scripts WHERE video_id=? AND status!='superseded' "
                       "ORDER BY version DESC LIMIT 1", (video_id,)).fetchone()
    if row is None:
        raise LookupError(f"no script for video {video_id}")
    return row
