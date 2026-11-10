"""Unit tests for the safety-critical logic: evidence matching, claim verification, QC number
checks, the task queue, quota caps, secret redaction and TTS text normalisation."""
import json
import sqlite3

import pytest

from ytengine import textutil


# ------------------------------------------------------------------ evidence matching
SRC = ('the presumption that you needed to get big fast, which worked for Amazon.com and virtually '
       'no one else," commented Gartner analyst Whit Andrews')


def test_exact_quote_matches_ignoring_punctuation():
    assert textutil.evidence_match("which worked for Amazon.com, and virtually no-one else", SRC) == 1.0


def test_paraphrased_quote_is_rejected():
    assert textutil.evidence_match("It worked for Amazon.com and virtually no one else", SRC) < 0.95


def test_invented_quote_is_rejected():
    assert textutil.evidence_match("Pets.com spent $11.5 million on a single Super Bowl ad", SRC) < 0.5


def test_sentence_split_keeps_abbreviations():
    assert textutil.sentences("Mr. Smith left. The U.S. grew 3%. Done!") == \
        ["Mr. Smith left.", "The U.S. grew 3%.", "Done!"]


def test_similarity():
    a = "the nasdaq peaked in march and then fell sharply over two years"
    assert textutil.script_similarity(a, a) == 1.0
    assert textutil.script_similarity(a, "completely different words about railways in eighteen forty") == 0.0


# ------------------------------------------------------------------ claim verification
@pytest.fixture()
def corpus():
    from ytengine import sources
    from ytengine.agents.fact_check import Corpus
    c = Corpus()
    c.docs["https://en.wikipedia.org/wiki/X"] = sources.Document(
        "https://en.wikipedia.org/wiki/X", "wikipedia.org", 3, "X",
        "On Friday March 10, 2000, the NASDAQ Composite stock market index peaked at 5,048.62.")
    c.docs["https://www.theguardian.com/y"] = sources.Document(
        "https://www.theguardian.com/y", "theguardian.com", 2, "Y",
        "By October 2002, it had plunged to 1114.11, a total loss of 78% against its peak.")
    c.series["NASDAQCOM"] = [("2000-03-09", 4900.0), ("2000-03-10", 5048.62), ("2002-10-09", 1114.11)]
    return c


def _claim(ref, text, evidence, typ="number"):
    return {"ref": ref, "text": text, "type": typ, "evidence": evidence}


def test_verify_claims_statuses(conn, topic_video, corpus):
    from ytengine.agents import fact_check as fc
    for url, d in corpus.docs.items():
        fc._upsert_source(conn, url, d.title, d.publisher, d.tier, d.sha256, d.text)
    from ytengine import sources
    fc._upsert_source(conn, sources.fred_url("NASDAQCOM"), "FRED", "stlouisfed.org", 1, None, "")
    proposed = [
        _claim("C1", "On March 10, 2000, the Nasdaq peaked at 5,048.62.", [
            {"kind": "text", "url": "https://en.wikipedia.org/wiki/X",
             "quote": "the NASDAQ Composite stock market index peaked at 5,048.62"},
            {"kind": "data_point", "series": "NASDAQCOM", "date": "2000-03-10", "value": 5048.62}]),
        _claim("C2", "The Nasdaq lost 78% by October 2002.", [
            {"kind": "text", "url": "https://www.theguardian.com/y",
             "quote": "By October 2002, it had plunged to 1114.11, a total loss of 78% against its peak."}]),
        _claim("C3", "The Nasdaq lost 87% by October 2002.", [
            {"kind": "text", "url": "https://www.theguardian.com/y",
             "quote": "By October 2002, it had plunged to 1114.11, a total loss of 78% against its peak."}]),
        _claim("C4", "Invented.", [{"kind": "text", "url": "https://www.theguardian.com/y",
                                     "quote": "a sentence that does not exist anywhere in the source"}], "event"),
        _claim("C5", "Peak at 5,100.", [{"kind": "data_point", "series": "NASDAQCOM", "date": "2000-03-10",
                                          "value": 5100}]),
        _claim("C6", "Fell 77.9% from peak to trough.", [{"kind": "data_change", "series": "NASDAQCOM",
                                                          "from": "2000-03-10", "to": "2002-10-09", "pct": -77.9}]),
    ]
    stats = fc.verify_claims(conn, topic_video, proposed, corpus)
    status = {r["ref"]: r["status"] for r in conn.execute("SELECT ref, status FROM claims")}
    assert status["C1"] == "verified"        # tier-1 data + text
    assert status["C2"] == "unverified"      # single publisher, tier 2
    assert status["C3"] == "rejected"        # number not in evidence
    assert status["C4"] == "rejected"        # quote not in source
    assert status["C5"] == "rejected"        # data mismatch
    assert status["C6"] == "verified"        # data change matches
    assert stats["verified"] == 2


def test_weekend_date_uses_previous_observation():
    from ytengine.agents.fact_check import _series_value
    assert _series_value([("2000-03-10", 1.0), ("2000-03-13", 2.0)], "2000-03-12") == ("2000-03-10", 1.0)


# ------------------------------------------------------------------ script QC
def _script(text, claims, scene=None):
    return {"title_working": "t", "acts": [{"name": f"A{i}", "paragraphs": [
        {"text": text if i == 0 else "Plain words here.", "claims": claims if i == 0 else [],
         "scene": scene or {"type": "card", "spec": {"style": "title", "big": "X"}}}]} for i in range(3)]}


def _store(conn, vid, script):
    from ytengine.agents import scriptwriter
    body = scriptwriter.render_body(script)
    conn.execute("INSERT INTO scripts(video_id, version, outline_json, body, word_count, est_duration_s) "
                 "VALUES (?,?,?,?,?,?)", (vid, 1, json.dumps(script), body,
                                          textutil.words(scriptwriter.narration_text(script)), 60))


def _verified_claim(conn, vid):
    conn.execute("INSERT INTO claims(video_id, ref, text, claim_type, status) VALUES (?,?,?,?,?)",
                 (vid, "C1", "The Nasdaq peaked at 5,048.62 in 2000.", "number", "verified"))


def test_script_gate_passes_supported_numbers(conn, topic_video):
    from ytengine.agents import qc
    _verified_claim(conn, topic_video)
    _store(conn, topic_video, _script("In 2000 the Nasdaq peaked at 5,048.62.", ["C1"]))
    rep = qc.script_gate(conn, topic_video)
    assert rep.passed, rep.failures()


def test_script_gate_rejects_unsupported_number(conn, topic_video):
    from ytengine.agents import qc
    _verified_claim(conn, topic_video)
    _store(conn, topic_video, _script("In 2000 the Nasdaq peaked at 6,000.", ["C1"]))
    rep = qc.script_gate(conn, topic_video)
    assert not rep.passed
    assert any("narration_numbers_supported" in f for f in rep.failures())


def test_script_gate_rejects_uncited_fact(conn, topic_video):
    from ytengine.agents import qc
    _store(conn, topic_video, _script("In 2000 the Nasdaq peaked.", []))
    assert not qc.script_gate(conn, topic_video).passed


def test_script_gate_rejects_onscreen_number(conn, topic_video):
    from ytengine.agents import qc
    _verified_claim(conn, topic_video)
    scene = {"type": "card", "spec": {"style": "number", "big": "-90%"}}
    _store(conn, topic_video, _script("In 2000 the Nasdaq peaked at 5,048.62.", ["C1"], scene))
    assert not qc.script_gate(conn, topic_video).passed


def test_script_gate_rejects_advice(conn, topic_video):
    from ytengine.agents import qc
    _store(conn, topic_video, _script("You should buy index funds now.", []))
    rep = qc.script_gate(conn, topic_video)
    assert any("no_financial_advice" in f for f in rep.failures())


# ------------------------------------------------------------------ task queue
def test_enqueue_is_idempotent(conn):
    from ytengine import db
    assert db.enqueue(conn, "video:1:render:v1", "render") is not None
    assert db.enqueue(conn, "video:1:render:v1", "render") is None
    assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_fail_backs_off_then_dies(conn):
    from ytengine import db
    db.enqueue(conn, "k", "x", max_attempts=2)
    t = db.claim(conn)
    assert db.fail(conn, t["id"], "boom") == "pending"
    conn.execute("UPDATE tasks SET next_run_at='2000-01-01T00:00:00Z'")
    t = db.claim(conn)
    assert db.fail(conn, t["id"], "boom again") == "dead"
    assert db.claim(conn) is None


def test_reclaim_stale_lease(conn):
    from ytengine import db
    db.enqueue(conn, "k", "x")
    db.claim(conn)
    conn.execute("UPDATE tasks SET lease_until='2000-01-01T00:00:00Z'")
    assert db.reclaim_stale(conn) == 1
    assert db.claim(conn) is not None


# ------------------------------------------------------------------ quota
def test_quota_hard_cap(conn):
    from ytengine import quota
    quota.consume(conn, "gemini:test", 5, cap=10)
    quota.consume(conn, "gemini:test", 5, cap=10)
    with pytest.raises(quota.QuotaExceeded):
        quota.consume(conn, "gemini:test", 1, cap=10)


def test_expense_requires_approval(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO expenses(month, item, amount_usd) VALUES ('2026-11','vps',5)")


# ------------------------------------------------------------------ redaction
def test_redaction(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSyTESTKEY1234567890abcdefghijklmnop")
    from ytengine.log import redact
    out = redact("call failed key=AIzaSyTESTKEY1234567890abcdefghijklmnop and Bearer abc "
                 "token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
    assert "AIzaSy" not in out and "ghp_" not in out


def test_redaction_auth_key_unregistered():
    """AI Studio auth keys (AQ.) are redacted by pattern even when not a registered secret."""
    from ytengine.log import redact
    out = redact("HTTP 401 with header x-goog-api-key: AQ.Ab8RN6Ixyz0123456789abcdefGHIJ-kl_mn end")
    assert "AQ.Ab8" not in out and "[REDACTED]" in out and out.endswith(" end")


# ------------------------------------------------------------------ TTS normalisation
def test_speakable():
    from ytengine.agents.voice import speakable
    s = speakable('On March 10, 2000, it fell 9.7% to $164 billion in Super Bowl XXXIV. "Quote".')
    assert "March tenth, two thousand" in s
    assert "9.7 percent" in s and "164 billion dollars" in s and "thirty-four" in s and '"' not in s
    assert speakable("In 1995 and 1907") == "In nineteen ninety-five and nineteen oh seven"


def test_captions_chunking():
    from ytengine.render.captions import cues
    out = cues([{"text": "word " * 30, "start": 0.0, "end": 6.0}])
    assert all(len(line) <= 42 for _, _, t in out for line in t.split("\n"))
    assert abs(out[-1][1] - 6.0) < 1e-6


# ------------------------------------------------------------------ LLM model fallback
def test_rank_gemini_models_prefers_stable_newest_flash():
    from ytengine.llm import rank_gemini_models
    ids = ["gemini-2.5-flash", "gemini-3.6-flash", "gemini-3.8-flash-tts", "gemini-3-flash-preview",
           "gemini-3.5-flash-lite", "gemini-embedding-2", "gemini-3.1-pro-preview", "gemini-flash-latest"]
    ranked = rank_gemini_models(ids)
    assert ranked[0] == "gemini-3.6-flash"
    assert "gemini-3.8-flash-tts" not in ranked and "gemini-embedding-2" not in ranked
    assert "gemini-3.1-pro-preview" not in ranked and "gemini-flash-latest" not in ranked
    assert ranked.index("gemini-3-flash-preview") > ranked.index("gemini-2.5-flash")


def test_retired_model_falls_through_to_auto(monkeypatch):
    """A 404 on a configured model is not retried; 'auto' discovers a working model."""
    from ytengine import llm
    monkeypatch.setenv("GEMINI_API_KEY", "AQ.test-key-not-real-0000000000")
    monkeypatch.setenv("YT_LLM", "auto")
    calls = []

    class Resp:
        def __init__(self, code, payload):
            self.status_code, self._p, self.headers, self.text = code, payload, {}, json.dumps(payload)

        def json(self):
            return self._p

    def fake_post(url, **kw):
        model = url.split("/models/")[1].split(":")[0]
        calls.append(model)
        if model == "gemini-9-flash":
            return Resp(200, {"candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}]})
        return Resp(404, {"error": {"code": 404}})

    def fake_get(url, **kw):
        return Resp(200, {"models": [
            {"name": "models/gemini-9-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-9-flash-tts", "supportedGenerationMethods": ["generateContent"]}]})

    monkeypatch.setattr(llm.requests, "post", fake_post)
    monkeypatch.setattr(llm.requests, "get", fake_get)
    monkeypatch.setattr(llm.GeminiAuto, "_cache", None)
    monkeypatch.setattr(llm.time, "sleep", lambda s: None)
    obj, label = llm.complete_json(None, "t", "p", {"type": "object", "required": ["ok"]})
    assert obj == {"ok": True} and label == "gemini:gemini-9-flash"
    configured = [m for m in calls if m != "gemini-9-flash"]
    assert len(configured) == len(set(configured)), "a retired model was retried"


def test_failed_topic_research_retries_within_hours(conn, monkeypatch):
    from ytengine import cycle, db as dbm
    from ytengine.agents import topics as tp
    monkeypatch.setattr(cycle, "_llm_available", lambda: True)
    monkeypatch.setattr(tp, "research", lambda c: (_ for _ in ()).throw(RuntimeError("all providers failed")))
    summary = cycle.run(conn, budget_s=5)
    assert any("topic research" in e for e in summary["errors"])
    hours = cycle._hours_since(dbm.get_setting(conn, "last_topic_research"))
    assert 16 < hours < 18   # gate is >20 h, so research is retried ~3-4 h later, not 20 h


# ------------------------------------------------------------------ source filtering (2026-11-10 cloud run)
def test_catalog_pages_are_not_references():
    from ytengine import sources
    urls = ["https://id.loc.gov/authorities/sh85008786", "https://www.jstor.org/stable/2122576",
            "https://lux.collections.yale.edu/view/concept/5cc",
            "https://www.lib.uchicago.edu/e/scrc/findingaids/view.php?eadid=X",
            "https://www.imf.org/external/np/speeches/1998/032098.htm"]
    assert sources.rank_references(urls) == ["https://www.imf.org/external/np/speeches/1998/032098.htm"]


def test_phrase_keywords_match_by_word(monkeypatch, conn):
    """LLM keywords are phrases; a relevant document must not be discarded for lacking the exact phrase."""
    from ytengine import sources
    from ytengine.agents import fact_check
    body = ("The assignats were paper money issued during the French Revolution, backed by confiscated "
            "church lands. Over-issue produced hyperinflation. ") * 30
    wiki = sources.Document("https://en.wikipedia.org/wiki/Assignat", "wikipedia.org", 3, "Assignat", body)
    ref = sources.Document("https://www.britannica.com/topic/assignat", "britannica.com", 2, "Assignat", body)
    monkeypatch.setattr(sources, "wikipedia", lambda t: wiki)
    monkeypatch.setattr(sources, "wikipedia_references", lambda t: [])
    monkeypatch.setattr(sources, "fetch_document_or_archive", lambda u, **k: ref)
    plan = {"wikipedia": ["Assignat"], "keywords": ["Assignat hyperinflation", "church land confiscation"],
            "extra_urls": ["https://www.britannica.com/topic/assignat"]}
    corpus = fact_check.gather(conn, 0, plan)
    assert "https://www.britannica.com/topic/assignat" in corpus.docs


def test_repair_reopens_bug_rejected_topics(conn):
    from ytengine import cycle, db as dbm
    conn.execute("INSERT INTO topics(slug, fingerprint, title, category, status, rejection_reason, created_at) VALUES "
                 "('a','a','A','bank-panic','rejected','insufficient evidence: x','2026-11-10T06:00:00Z'),"
                 "('b','b','B','bank-panic','rejected','owner rejected','2026-11-10T06:00:00Z')")
    cycle._repair_2026_11_10(conn)
    st = dict(conn.execute("SELECT slug, status FROM topics").fetchall())
    assert st == {"a": "candidate", "b": "rejected"}
    conn.execute("UPDATE topics SET status='rejected', rejection_reason='insufficient evidence: y' WHERE slug='a'")
    cycle._repair_2026_11_10(conn)  # runs once only
    assert conn.execute("SELECT status FROM topics WHERE slug='a'").fetchone()[0] == "rejected"


def test_corroborate_finds_second_publisher_and_respects_numbers():
    from ytengine import sources
    from ytengine.agents import fact_check
    c = fact_check.Corpus()
    c.docs["https://www.nytimes.com/a"] = sources.Document(
        "https://www.nytimes.com/a", "nytimes.com", 2, "x",
        "Unrelated opening line here. The Columbia Trust Company merged with the Irving Bank of New York in "
        "February 1923, forming the Irving Bank-Columbia Trust Company. Other news follows.")
    claim = "In February 1923, Columbia Trust Company merged with Irving Bank of New York."
    hit = fact_check.corroborate(claim, c, {"wikipedia.org"})
    assert hit and hit[0] == "https://www.nytimes.com/a"
    assert fact_check.corroborate(claim.replace("1923", "1924"), c, {"wikipedia.org"}) is None  # number must match
    assert fact_check.corroborate(claim, c, {"nytimes.com"}) is None                          # must be ANOTHER publisher


def test_relevant_excerpt_skips_cover_pages():
    from ytengine.agents import fact_check
    txt = ("Cover page. Table of contents. " * 400) + "Greece revised its 2009 budget deficit to 12.7 percent " \
          "of GDP, and the IMF approved a stand-by arrangement. " + ("Annex tables. " * 400)
    ex = fact_check.relevant_excerpt(txt, fact_check._content_words("Greece deficit IMF stand-by"), 600)
    assert "12.7 percent" in ex and len(ex) <= 700


def test_inline_citation_markers_are_not_spoken_or_counted():
    from ytengine.agents import scriptwriter, fact_check
    script = {"acts": [{"name": "A", "paragraphs": [{
        "text": "In January 1719 the bank was renamed the Banque Royale [C13]. Notes became legal tender (C22, C7).",
        "claims": ["C1"], "scene": {"type": "card", "spec": {"big": "1719 [C13]"}}}]}]}
    p = scriptwriter.paragraphs(script)[0]
    assert "C13" not in p["text"] and "C22" not in p["text"] and "[" not in p["text"]
    assert p["claims"] == ["C1", "C13", "C22", "C7"]
    assert p["scene"]["spec"]["big"] == "1719"
    assert "C13" not in scriptwriter.narration_text(script)
    assert fact_check.numbers_in("ref C13 and B2B") == []


def test_spelled_out_claim_numbers_support_digits():
    from ytengine.agents import qc
    assert 10.0 in qc._claim_nums("Law closed the bank for ten days.")


def test_quoted_terms_and_titles_are_not_speech():
    from ytengine.agents import qc
    claim = 'In 1705 Law wrote a pamphlet titled "Money and Trade Considered: with a Proposal for Supplying the Nation with Money".'
    assert qc._is_term("Beau", "")
    assert qc._is_term("millionaire", "")
    assert qc._is_term("Money and Trade Considered: with a Proposal for Supplying the Nation with Money", claim)
    assert not qc._is_term("we will pay every note in silver on demand", claim)  # a real quotation
