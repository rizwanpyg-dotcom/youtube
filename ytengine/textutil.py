"""Deterministic text helpers: normalisation, evidence matching, similarity, sentences."""
from __future__ import annotations

import difflib
import re
import unicodedata

_STOP = set("""a an the of and or in on at to for from by with as is was were be been are this that
these those it its into over under after before during about than then there their they them he she
his her we our you your not no but if so how why what when who which""".split())


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = (text.replace("’", "'").replace("‘", "'").replace("“", '"')
            .replace("”", '"').replace("–", "-").replace("—", "-").replace(" ", " "))
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def slugify(text: str, max_len: int = 60) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:max_len].rstrip("-")


def keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", normalize(text)) if w not in _STOP and len(w) > 2}


def fingerprint(text: str) -> str:
    return " ".join(sorted(keywords(text)))


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def word_trigrams(text: str) -> set[tuple[str, ...]]:
    w = re.findall(r"[a-z0-9']+", normalize(text))
    return {tuple(w[i:i + 3]) for i in range(len(w) - 2)}


def script_similarity(a: str, b: str) -> float:
    return jaccard(word_trigrams(a), word_trigrams(b))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+(?:'[a-z]+)?", normalize(text))


def evidence_match(quote: str, source_text: str, threshold: float = 0.95) -> float:
    """Share of the quote's words found, in order, in the best-matching span of the source.

    1.0 means the quote's words appear contiguously in the source (punctuation and case
    ignored). Matching is on whole words, so changing a single word ("which worked" ->
    "It worked") costs a full word, which is what rejects paraphrases presented as quotes.
    """
    q, s = _tokens(quote), _tokens(source_text)
    if not q or not s:
        return 0.0
    n = len(q)
    first = q[0]
    for i in range(len(s) - n + 1):
        if s[i] == first and s[i:i + n] == q:
            return 1.0
    # Anchor on the rarest quote words, compare same-length word windows around each hit.
    counts = {w: s.count(w) for w in set(q)}
    anchors = sorted((w for w in set(q) if counts[w]), key=lambda w: counts[w])[:3]
    best = 0.0
    for anchor in anchors:
        a_off = q.index(anchor)
        for pos in (i for i, w in enumerate(s) if w == anchor):
            lo = max(0, pos - a_off - 2)
            window = s[lo: lo + n + 4]
            m = difflib.SequenceMatcher(None, q, window, autojunk=False)
            best = max(best, sum(b.size for b in m.get_matching_blocks()) / n)
            if best >= threshold:
                return best
    return best


# Lookbehinds sit just after the punctuation, so each abbreviation includes its dot.
_ABBREV = r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bDr\.)(?<!\bSt\.)(?<!\bvs\.)(?<!\bU\.S\.)(?<!\bInc\.)(?<!\bCo\.)"


def sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text).strip()
    parts = re.split(r"(?<=[.!?])" + _ABBREV + r"\s+(?=[A-Z0-9\"'])", text)
    return [p.strip() for p in parts if p.strip()]


def words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9']+", text))
