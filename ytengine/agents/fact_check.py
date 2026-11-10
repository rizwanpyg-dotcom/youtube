"""Agent 3: research gathering, claim extraction and deterministic verification.

The LLM only *proposes* claims with evidence. Code then decides:
  1. text evidence must literally appear in the fetched source (evidence_match >= 0.95);
  2. data evidence must match the downloaded dataset (FRED) within tolerance;
  3. every number in the claim must appear in its verified evidence;
  4. a claim is 'verified' with a tier-1 source, or with verified evidence from
     2+ distinct publishers. Contradicting evidence makes it 'conflicting'.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import date

from .. import db, llm, sources, textutil
from ..log import get as get_log

log = get_log("fact_check")

MAX_DOCS = 14
MAX_REFS_TRIED = 45
REF_BUDGET_S = 240      # cap on reference fetching per Wikipedia article
FETCH_WORKERS = 6
DOC_CHARS_IN_PROMPT = 7000


@dataclass
class Corpus:
    docs: dict[str, sources.Document] = field(default_factory=dict)   # url -> doc
    series: dict[str, list[tuple[str, float]]] = field(default_factory=dict)

    def by_url(self, url: str) -> sources.Document | None:
        if url in self.docs:
            return self.docs[url]
        o = sources.original_url(url)
        for u, d in self.docs.items():
            if sources.original_url(u) == o:
                return d
        return None


# ------------------------------------------------------------------ gather
def gather(conn: sqlite3.Connection, video_id: int, plan: dict) -> Corpus:
    """plan = {"wikipedia": [titles], "fred": [{"series","start","end"}], "extra_urls": [...],
               "keywords": [...]}"""
    corpus = Corpus()
    # Keywords may be phrases ("Assignat hyperinflation"); match their individual words.
    kw = textutil.keywords(" ".join(plan.get("keywords", [])))
    for title in plan.get("wikipedia", []):
        doc = sources.wikipedia(title)
        corpus.docs[doc.url] = doc
        refs = [u for u in sources.rank_references(sources.wikipedia_references(title)) if sources.tier(u) <= 2]
        fetched = _fetch_parallel(refs[:MAX_REFS_TRIED])
        for url in refs[:MAX_REFS_TRIED]:  # keep rank order (tier first), not completion order
            d = fetched.get(url)
            if d is None or len(corpus.docs) >= MAX_DOCS:
                continue
            text_l = d.text.lower()
            hits = sum(1 for k in kw if k in text_l)
            if len(d.text) < 1500 or (kw and hits < 2):
                continue
            if any(sources.original_url(u) == sources.original_url(url) for u in corpus.docs):
                continue
            corpus.docs[url] = d
    for url in plan.get("extra_urls", []):
        try:
            d = sources.fetch_document_or_archive(url)
            if (not kw or sum(1 for k in kw if k in d.text.lower()) >= 2) and len(d.text) >= 800:
                # wrong/irrelevant URLs are discarded
                corpus.docs[url] = d
        except Exception as e:
            log.info("extra source unavailable %s: %s", url, e)
    for spec in plan.get("fred", []):
        corpus.series[spec["series"]] = sources.fred_series(spec["series"], spec["start"], spec["end"])

    for url, d in corpus.docs.items():
        _upsert_source(conn, url, d.title, d.publisher, d.tier, d.sha256, d.text[:1500])
    for s in corpus.series:
        _upsert_source(conn, sources.fred_url(s), f"FRED series {s}", "stlouisfed.org", 1, None,
                       f"Federal Reserve Bank of St. Louis, FRED series {s}")
    pubs = sorted({d.publisher for d in corpus.docs.values()})
    (log.warning if len(pubs) < 3 else log.info)(
        "gathered %d documents, %d data series (publishers: %s)", len(corpus.docs), len(corpus.series), pubs)
    return corpus


def _fetch_parallel(urls: list[str]) -> dict[str, sources.Document]:
    """Fetch cited references concurrently (per-host pacing still applies) within REF_BUDGET_S."""
    from concurrent.futures import ThreadPoolExecutor, wait
    out: dict[str, sources.Document] = {}
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as ex:
        futures = {ex.submit(sources.fetch_document_or_archive, u): u for u in urls}
        done, pending = wait(futures, timeout=REF_BUDGET_S)
        for f in pending:
            f.cancel()
        for f in done:
            try:
                out[futures[f]] = f.result()
            except Exception as e:  # dead links and blocks are normal
                log.debug("skip %s: %s", futures[f], e)
    log.info("references: %d tried, %d fetched, %d timed out", len(urls), len(out), len(pending))
    return out


def _upsert_source(conn, url, title, publisher, tier, sha, excerpt) -> int:
    conn.execute(
        "INSERT INTO sources(url, title, publisher, tier, content_sha256, excerpt) VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title, content_sha256=excluded.content_sha256, "
        "accessed_at=strftime('%Y-%m-%dT%H:%M:%SZ','now')",
        (url, title, publisher, tier, sha, excerpt))
    return conn.execute("SELECT id FROM sources WHERE url=?", (url,)).fetchone()["id"]


# ------------------------------------------------------------------ extract (LLM)
CLAIMS_SCHEMA = {
    "type": "object", "required": ["claims"],
    "properties": {"claims": {"type": "array", "minItems": 1, "items": {
        "type": "object", "required": ["ref", "text", "type", "evidence"],
        "properties": {
            "ref": {"type": "string"}, "text": {"type": "string"},
            "type": {"type": "string", "enum": ["number", "date", "quote", "event", "causal", "other"]},
            "evidence": {"type": "array", "minItems": 1, "items": {"type": "object"}},
        }}}},
}


def relevant_excerpt(text: str, focus: set[str], budget: int) -> str:
    """The most on-topic passages of a document (in original order) instead of just its start:
    long reports put the substance far from the cover page."""
    if len(text) <= budget:
        return text
    sents = textutil.sentences(text)
    windows = [" ".join(sents[i:i + 3]) for i in range(0, len(sents), 3)]
    scored = sorted(range(len(windows)),
                    key=lambda i: -len(focus & {_stem(w) for w in textutil.keywords(windows[i])}))
    keep, used = set(), 0
    for i in scored:
        if used + len(windows[i]) > budget:
            continue
        keep.add(i)
        used += len(windows[i]) + 1
    return "\n".join(windows[i] for i in sorted(keep))


def extract_prompt(topic: str, angle: str, corpus: Corpus, keywords: list[str] | None = None) -> str:
    focus = _content_words(" ".join([topic, angle] + list(keywords or [])))
    blocks, pubs = [], {}
    for url, d in corpus.docs.items():
        pubs.setdefault(d.publisher, []).append(url)
        budget = DOC_CHARS_IN_PROMPT * (2 if d.publisher == "wikipedia.org" else 1)
        blocks.append(f"### SOURCE {url}\nPublisher: {d.publisher} (tier {d.tier})\n"
                      f"{relevant_excerpt(d.text, focus, budget)}")
    series = "\n".join(f"- FRED {s}: {v[0][0]}..{v[-1][0]}, {len(v)} observations" for s, v in corpus.series.items())
    return f"""Topic: {topic}
Angle: {angle}
Publishers available: {", ".join(f"{p} ({len(u)} source{'s' if len(u) > 1 else ''})" for p, u in pubs.items())}

Extract 25-40 atomic, checkable factual claims useful for a documentary on this topic.

VERIFICATION RULE (claims that fail it are discarded automatically):
a claim counts only if it is supported by TWO DIFFERENT PUBLISHERS, or by one government / central-bank /
IMF / academic (tier 1) source. So prefer the central facts that several sources state (dates, amounts,
who did what, causes and consequences) over details only one article mentions. For EVERY claim give an
evidence item from each publisher that states it (two or more when possible).

Text evidence: {{"kind":"text","url":<source url exactly as given>,
"quote":<a passage copied VERBATIM from that source, 8-60 words>,"stance":"supports"|"contradicts"}}.
Numeric data evidence from datasets:
{{"kind":"data_point","series":<id>,"date":"YYYY-MM-DD","value":<number>}} or
{{"kind":"data_change","series":<id>,"from":"YYYY-MM-DD","to":"YYYY-MM-DD","pct":<number>}}.
Copy quotes exactly; do not paraphrase inside "quote". Every number in a claim's text must appear in its evidence.
Return {{"claims":[{{"ref":"C1","text":...,"type":"number|date|quote|event|causal|other","evidence":[...]}}]}}

Datasets:
{series}

{chr(10).join(blocks)}"""


# ------------------------------------------------------------------ verify (deterministic)
_NUM = re.compile(r"(?<![A-Za-z])\$?\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> list[float]:
    out = []
    for m in _NUM.finditer(text):
        s = m.group(0).lstrip("$").replace(",", "")
        try:
            out.append(float(s))
        except ValueError:
            pass
    return out


def _series_value(series: list[tuple[str, float]], day: str) -> tuple[str, float] | None:
    """Exact date, or the nearest earlier observation (weekends/holidays)."""
    best = None
    for d, v in series:
        if d <= day:
            best = (d, v)
        else:
            break
    return best


def verify_evidence(ev: dict, corpus: Corpus) -> tuple[bool, str, str | None, str]:
    """Returns (ok, note, source_url, evidence_text_for_storage)."""
    kind = ev.get("kind", "text")
    if kind == "text":
        url, quote = ev.get("url", ""), ev.get("quote", "")
        doc = corpus.by_url(url)
        if doc is None:
            return False, "source not in corpus", None, quote
        score = textutil.evidence_match(quote, doc.text)
        return score >= 0.95, f"match={score:.2f}", doc.url, quote
    if kind in ("data_point", "data_change"):
        s = corpus.series.get(ev.get("series", ""))
        if not s:
            return False, "series not loaded", None, json.dumps(ev)
        url = sources.fred_url(ev["series"])
        if kind == "data_point":
            obs = _series_value(s, ev["date"])
            if not obs:
                return False, "no observation", url, json.dumps(ev)
            ok = abs(obs[1] - float(ev["value"])) <= max(0.005 * abs(obs[1]), 0.01)
            return ok, f"observed {obs[1]} on {obs[0]}", url, f"{ev['series']} on {obs[0]} = {obs[1]}"
        a, b = _series_value(s, ev["from"]), _series_value(s, ev["to"])
        if not a or not b:
            return False, "no observation", url, json.dumps(ev)
        pct = (b[1] - a[1]) / a[1] * 100
        ok = abs(pct - float(ev["pct"])) <= 0.6
        return ok, f"observed {pct:.1f}%", url, f"{ev['series']} {a[0]}={a[1]} -> {b[0]}={b[1]} ({pct:.1f}%)"
    return False, f"unknown evidence kind {kind}", None, json.dumps(ev)


# ------------------------------------------------------------------ automatic second source
CORROBORATE_MIN_WORDS = 4      # claims with fewer content words are too vague to match safely
CORROBORATE_OVERLAP = 0.6      # share of the claim's content words that must appear in the passage


def _stem(w: str) -> str:
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


def _content_words(text: str) -> set[str]:
    return {_stem(w) for w in textutil.keywords(text) if not w.isdigit()}


def _passages(corpus: Corpus) -> list[tuple[str, str, set[str], list[float]]]:
    """(url, passage, content words, numbers) for every 2-sentence window, cached on the corpus."""
    cached = getattr(corpus, "_passage_index", None)
    if cached is None:
        cached = []
        for url, d in corpus.docs.items():
            sents = textutil.sentences(d.text)
            for i in range(len(sents)):
                p = " ".join(sents[i:i + 2])
                if 40 <= len(p) <= 900:
                    nums = numbers_in(p) + [float(x) for x in re.findall(r"\d+", p)]
                    cached.append((url, p, _content_words(p), nums))
        corpus._passage_index = cached
    return cached


def corroborate(claim_text: str, corpus: Corpus, exclude_publishers: set[str]) -> tuple[str, str] | None:
    """Find a passage from ANOTHER publisher that states the same fact: every number in the claim
    appears in it and most of the claim's content words do. Returns (url, passage) or None.
    This is the second source Gemini didn't cite; the passage is real text from that document."""
    words = _content_words(claim_text)
    if len(words) < CORROBORATE_MIN_WORDS:
        return None
    nums = numbers_in(claim_text)
    best = None
    for url, passage, pwords, pnums in _passages(corpus):
        if sources.publisher(url) in exclude_publishers:
            continue
        if any(not any(abs(n - e) < 1e-9 for e in pnums) for n in nums):
            continue
        overlap = len(words & pwords) / len(words)
        if overlap >= CORROBORATE_OVERLAP and (best is None or overlap > best[0]):
            best = (overlap, url, passage)
    return (best[1], best[2]) if best else None


def _numbers_supported(claim_text: str, evidence_texts: list[str], data_values: list[float]) -> list[float]:
    """Return claim numbers NOT found in evidence (empty list = all supported)."""
    ev_nums = []
    for t in evidence_texts:
        ev_nums += numbers_in(t)
        # dates like 2000-03-10 contribute 2000, 3, 10
        ev_nums += [float(x) for x in re.findall(r"\d+", t)]
    missing = []
    for n in numbers_in(claim_text):
        if any(abs(n - e) < 1e-9 for e in ev_nums):
            continue
        if any(abs(abs(n) - abs(v)) <= max(0.01 * abs(v), 0.6) for v in data_values):
            continue  # rounded data value, e.g. 78 vs -77.9 or 5,048 vs 5048.62
        missing.append(n)
    return missing


def verify_claims(conn: sqlite3.Connection, video_id: int, proposed: list[dict], corpus: Corpus) -> dict:
    stats = {"proposed": len(proposed), "verified": 0, "conflicting": 0, "rejected": 0, "unverified": 0}
    conn.execute("DELETE FROM claims WHERE video_id=?", (video_id,))
    for c in proposed:
        support_pubs, tier1, contradict, best_tier = set(), False, False, 9
        good_texts, data_vals, rows, notes = [], [], [], []
        for ev in c["evidence"]:
            ok, note, url, ev_text = verify_evidence(ev, corpus)
            notes.append(note)
            stance = ev.get("stance", "supports")
            if not ok or url is None:
                continue
            src_tier = sources.tier(url)
            if stance == "contradicts":
                contradict = True
            else:
                support_pubs.add(sources.publisher(url))
                tier1 = tier1 or src_tier == 1
                best_tier = min(best_tier, src_tier)
                good_texts.append(ev_text)
                if ev.get("kind") == "data_point":
                    data_vals.append(float(ev["value"]))
                elif ev.get("kind") == "data_change":
                    data_vals.append(float(ev["pct"]))
            rows.append((url, stance, ev_text))

        missing = _numbers_supported(c["text"], good_texts, data_vals)
        corroborated = None
        if (not missing and good_texts and not contradict and not tier1 and len(support_pubs) < 2
                and not (c["type"] == "quote" and best_tier <= 2)):
            corroborated = corroborate(c["text"], corpus, support_pubs)
            if corroborated:
                url2, passage = corroborated
                support_pubs.add(sources.publisher(url2))
                tier1 = tier1 or sources.tier(url2) == 1
                good_texts.append(passage)
                rows.append((url2, "supports", passage))
        if missing:
            status, why = "rejected", f"numbers not in evidence: {missing}"
        elif not good_texts:
            status, why = "rejected", "no evidence verified (" + "; ".join(notes) + ")"
        elif contradict:
            status, why = "conflicting", "sources disagree"
        elif tier1 or len(support_pubs) >= 2:
            status, why = "verified", f"publishers={sorted(support_pubs)}" + (
                " (second source found automatically)" if corroborated else "")
        elif c["type"] == "quote" and best_tier <= 2:
            # A direct quotation is "X said Y, as reported by Z": one verified tier-1/2
            # report suffices, and the script must attribute it (checked by QC).
            status, why = "verified", f"attributed quote via {sorted(support_pubs)}"
        else:
            status, why = "unverified", f"single publisher {sorted(support_pubs)}"
        stats[status] += 1
        confidence = 1.0 if tier1 else min(1.0, 0.4 + 0.3 * len(support_pubs))
        cur = conn.execute(
            "INSERT INTO claims(video_id, ref, text, claim_type, status, confidence, notes) VALUES (?,?,?,?,?,?,?)",
            (video_id, c["ref"], c["text"], c["type"], status, confidence, why))
        claim_id = cur.lastrowid
        for url, stance, ev_text in rows:
            sid = conn.execute("SELECT id FROM sources WHERE url=?", (url,)).fetchone()
            if sid is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO claim_sources(claim_id, source_id, stance, evidence_quote, quote_verified) "
                "VALUES (?,?,?,?,1)", (claim_id, sid["id"], stance, ev_text[:1000]))
        if status != "verified":
            log.info("claim %s %s: %s", c["ref"], status, why)
    return stats


def run(conn: sqlite3.Connection, video_id: int) -> dict:
    v = db.video(conn, video_id)
    topic = conn.execute("SELECT * FROM topics WHERE id=?", (v["topic_id"],)).fetchone()
    plan = json.loads(topic["evidence_json"]).get("research_plan")
    if not plan:
        raise ValueError("topic has no research_plan")
    corpus = gather(conn, video_id, plan)
    db.set_stage(conn, video_id, "researched")
    proposed, provider = llm.complete_json(conn, "claims_extract",
                                           extract_prompt(topic["title"], topic["angle"] or "", corpus, plan.get("keywords")),
                                           CLAIMS_SCHEMA, temperature=0.1)
    stats = verify_claims(conn, video_id, proposed["claims"], corpus)
    stats.update(documents=len(corpus.docs), series=list(corpus.series), provider=provider,
                 publishers=sorted({d.publisher for d in corpus.docs.values()}))
    db.set_stage(conn, video_id, "fact_checked")
    log.info("fact-check: %s", stats)
    return {"stats": stats, "corpus": corpus}
