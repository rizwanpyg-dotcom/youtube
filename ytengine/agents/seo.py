"""Agent 8a: SEO metadata. The LLM proposes titles/summary/tags; code assembles the rest.

Description structure (deterministic): summary, chapters, numbered sources (only those
backing claims the script actually uses), credits, and the AI/education disclosure.
"""
from __future__ import annotations

import json
import sqlite3

from .. import db, llm
from ..log import get as get_log
from . import scriptwriter, strategy

log = get_log("seo")

SEO_SCHEMA = {
    "type": "object", "required": ["titles", "summary", "tags", "thumbnail"],
    "properties": {
        "titles": {"type": "array", "minItems": 2, "items": {"type": "object", "required": ["text", "pattern"]}},
        "summary": {"type": "string"},
        "tags": {"type": "array", "minItems": 5, "items": {"type": "string"}},
        "thumbnail": {"type": "object", "required": ["big", "label"]},
    },
}

TITLE_PATTERNS = ["story-question", "number-led", "how-why"]

DISCLOSURE = ("About this video: the narration voice is AI-generated (Kokoro-82M). Every factual claim was "
              "checked against the sources listed above; numbers on charts come directly from the cited data. "
              "This video is for education only and is not financial advice.")


def _ts(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def used_sources(conn: sqlite3.Connection, video_id: int, script: dict) -> list[sqlite3.Row]:
    refs = sorted({r for p in scriptwriter.paragraphs(script) for r in p["claims"]})
    if not refs:
        return []
    return conn.execute(
        f"SELECT DISTINCT s.* FROM sources s JOIN claim_sources cs ON cs.source_id=s.id "
        f"JOIN claims c ON c.id=cs.claim_id WHERE c.video_id=? AND c.ref IN ({','.join('?' * len(refs))}) "
        f"AND cs.stance='supports' ORDER BY s.tier, s.publisher", (video_id, *refs)).fetchall()


def build(conn: sqlite3.Connection, video_id: int, chapters: list[dict]) -> dict:
    v = db.video(conn, video_id)
    row = scriptwriter.latest(conn, video_id)
    script = json.loads(row["outline_json"])
    claims = "\n".join(f"{c['ref']}: {c['text']}" for c in conn.execute(
        "SELECT ref, text FROM claims WHERE video_id=? AND status='verified'", (video_id,)))
    task = "seo_short" if v["format"] == "short" else "seo"
    kind = "YouTube Short (vertical teaser for a longer documentary)" if v["format"] == "short" else "documentary"
    proposal, provider = llm.complete_json(conn, task, f"""Create YouTube metadata for this {kind}.
Working title: {script['title_working']}
Narration (excerpt): {scriptwriter.narration_text(script)[:2500]}
Verified claims (titles/thumbnail may only use numbers from these):
{claims}
Rules: titles <= 70 characters, accurate, no clickbait words (shocking, insane, destroyed, exposed),
no promises the video doesn't keep. Provide 3 titles labelled with a pattern from {TITLE_PATTERNS}.
Summary: 2-3 plain sentences. Tags: 10-15 search phrases. Thumbnail: big text <= 6 characters taken from a
verified number, label <= 3 words, plus the FRED series and date range for a background chart if relevant.
Return {{"titles":[{{"text":...,"pattern":...}}],"summary":...,"tags":[...],
"thumbnail":{{"big":...,"label":...,"series":...,"range":["YYYY-MM-DD","YYYY-MM-DD"]}}}}""",
                                           SEO_SCHEMA, temperature=0.5)
    titles = [t for t in proposal["titles"] if len(t["text"]) <= 70] or proposal["titles"]
    pattern = strategy.choose(conn, "title_pattern", sorted({t["pattern"] for t in titles}))
    title = next(t["text"] for t in titles if t["pattern"] == pattern)

    lines = [proposal["summary"].strip(), ""]
    if len(chapters) >= 3:
        lines.append("Chapters")
        lines += [f"{_ts(c['t'])} {c['title']}" for c in chapters]
        lines.append("")
    srcs = used_sources(conn, video_id, script)
    if srcs:
        lines.append("Sources")
        for i, s in enumerate(srcs, 1):
            lines.append(f"[{i}] {s['title'] or s['publisher']} ({s['publisher']}): {s['url']}")
        lines.append("")
    credits = [a["attribution"] for a in conn.execute(
        "SELECT DISTINCT attribution FROM assets WHERE video_id=? AND attribution IS NOT NULL", (video_id,))]
    lines.append("Credits")
    lines += credits or ["All visuals are original."]
    lines.append("Fonts: Archivo Black and Inter (SIL Open Font License).")
    lines += ["", DISCLOSURE]
    description = "\n".join(lines)
    tags = []
    for t in proposal["tags"]:  # YouTube limit: ~500 chars total
        if len(",".join(tags + [t])) <= 480:
            tags.append(t)
    meta = {"title": title, "title_pattern": pattern, "format": v["format"], "description": description, "tags": tags,
            "chapters": chapters, "category_id": "27", "synthetic_media": False, "made_for_kids": False,
            "thumbnail": proposal["thumbnail"], "alt_titles": [t["text"] for t in titles if t["text"] != title],
            "provider": provider}
    conn.execute(
        "INSERT INTO seo_metadata(video_id, title, description, tags_json, chapters_json, category_id, "
        "synthetic_media, made_for_kids, keywords_json) VALUES (?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(video_id) DO UPDATE SET title=excluded.title, description=excluded.description, "
        "tags_json=excluded.tags_json, chapters_json=excluded.chapters_json, keywords_json=excluded.keywords_json",
        (video_id, title, description, json.dumps(tags), json.dumps(chapters), "27", 0, 0,
         json.dumps(meta["alt_titles"])))
    db.set_setting(conn, f"title_pattern:{video_id}", pattern)  # read by the learning loop
    log.info("title (%s): %s", pattern, title)
    return meta
