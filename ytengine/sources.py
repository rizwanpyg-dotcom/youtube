"""Source fetching: polite HTTP with cache, Wikipedia, reference discovery, FRED data.

Publisher identity: archive.org copies count as the original publisher, so a
Wayback snapshot of cnn.com is "cnn.com", not an extra independent source.
"""
from __future__ import annotations

import csv
import hashlib
import html
import io
import re
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config
from .log import get as get_log

log = get_log("sources")

USER_AGENT = "ytengine/0.1 (documentary research bot; respects robots.txt)"

# Credibility tiers: 1 primary/official, 2 major press/academic, 3 encyclopedic, 4 other.
TIER1 = ("fred.stlouisfed.org", "stlouisfed.org", "federalreservehistory.org", "newyorkfed.org", "minneapolisfed.org",
         "richmondfed.org", "philadelphiafed.org", "clevelandfed.org", "frbsf.org", "dallasfed.org",
         "sec.gov", "federalreserve.gov", "bls.gov", "census.gov", "worldbank.org",
         "imf.org", "bis.org", "ecb.europa.eu", "bankofengland.co.uk", "gao.gov", "congress.gov", "nber.org",
         "loc.gov", "archives.gov")
TIER2 = ("nytimes.com", "wsj.com", "ft.com", "bloomberg.com", "reuters.com", "apnews.com", "bbc.co.uk",
         "bbc.com", "theguardian.com", "economist.com", "cnn.com", "latimes.com", "washingtonpost.com",
         "wired.com", "cnet.com", "fortune.com", "forbes.com", "businessinsider.com", "npr.org",
         "marketwatch.com", "barrons.com", "sfgate.com", "nymag.com", "fastcompany.com", "salon.com",
         "britannica.com", "time.com", "theatlantic.com", "newyorker.com", "jstor.org", "nasdaq.com")
TIER3 = ("wikipedia.org",)


@dataclass
class Document:
    url: str
    publisher: str
    tier: int
    title: str
    text: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def original_url(url: str) -> str:
    m = re.match(r"https?://web\.archive\.org/web/\d+[a-z_]*/(.+)", url)
    return m.group(1) if m else url


def publisher(url: str) -> str:
    host = urlparse(original_url(url)).netloc.lower()
    host = host.split(":")[0]
    parts = host.split(".")
    # money.cnn.com -> cnn.com ; en.wikipedia.org -> wikipedia.org ; bbc.co.uk stays
    if len(parts) >= 3 and parts[-2] in ("co", "com", "ac", "gov", "org") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def tier(url: str) -> int:
    pub = publisher(url)
    host = urlparse(original_url(url)).netloc.lower()
    if any(host.endswith(d) for d in TIER1) or host.endswith(".gov"):
        return 1
    if any(pub == d or host.endswith(d) for d in TIER2) or host.endswith(".edu"):
        return 2
    if any(pub == d for d in TIER3):
        return 3
    return 4


# ------------------------------------------------------------------ HTTP
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT
_session.mount("https://", requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=16))
_last_hit: dict[str, float] = {}
_pace_lock = threading.Lock()


def _pace(host: str, interval: float = 1.0) -> None:
    """At most one request per `interval` seconds per host, safe across threads."""
    with _pace_lock:
        slot = max(time.time(), _last_hit.get(host, 0) + interval)
        _last_hit[host] = slot
    delay = slot - time.time()
    if delay > 0:
        time.sleep(delay)


@lru_cache(maxsize=256)
def _robots(base: str) -> urllib.robotparser.RobotFileParser | None:
    rp = urllib.robotparser.RobotFileParser()
    try:
        r = _session.get(base + "/robots.txt", timeout=10)
        if r.status_code >= 400:
            return None
        rp.parse(r.text.splitlines())
        return rp
    except requests.RequestException:
        return None


def allowed(url: str) -> bool:
    p = urlparse(url)
    rp = _robots(f"{p.scheme}://{p.netloc}")
    return True if rp is None else rp.can_fetch(USER_AGENT, url)


def fetch(url: str, *, timeout: int = 20, use_cache: bool = True, check_robots: bool = True,
          attempts: int = 3) -> str:
    cache_dir = config.get().paths.cache / "http"
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".txt")
    if use_cache and key.exists():
        return key.read_text(encoding="utf-8")
    if check_robots and not allowed(url):
        raise PermissionError(f"robots.txt disallows {url}")
    _pace(urlparse(url).netloc)
    for attempt in range(attempts):
        try:
            r = _session.get(url, timeout=timeout)
            if r.status_code in (429, 503) and attempt < attempts - 1:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            # requests assumes ISO-8859-1 when no charset is declared; detect instead.
            if "charset" not in r.headers.get("content-type", "").lower():
                r.encoding = r.apparent_encoding
            key.write_text(r.text, encoding="utf-8")
            return r.text
        except requests.RequestException:
            if attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("unreachable")


def html_to_text(raw: str) -> tuple[str, str]:
    """Return (title, main text). Prefers <p> content, which skips navigation and toolbars."""
    title_m = re.search(r"(?is)<title[^>]*>(.*?)</title>", raw)
    title = html.unescape(re.sub(r"\s+", " ", title_m.group(1))).strip() if title_m else ""
    body = re.sub(r"(?is)<(script|style|noscript|nav|footer|header|aside)\b.*?</\1>", " ", raw)
    paras = re.findall(r"(?is)<p\b[^>]*>(.*?)</p>", body)
    text = "\n".join(html.unescape(re.sub(r"<[^>]+>", " ", p)) for p in paras)
    if len(text) < 1200:
        text = html.unescape(re.sub(r"<[^>]+>", " ", body))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return title, text


def fetch_document(url: str, **kw) -> Document:
    title, text = html_to_text(fetch(url, **kw))
    return Document(url=url, publisher=publisher(url), tier=tier(url), title=title, text=text)


def wayback_url(url: str) -> str:
    """Internet Archive copy (redirects to the snapshot nearest 2020). Same publisher as the original."""
    return f"https://web.archive.org/web/2020/{original_url(url)}"


def fetch_document_or_archive(url: str, min_chars: int = 1500) -> Document:
    """Original first; if blocked (403), dead, paywalled or too short, try the Internet Archive copy once."""
    try:
        d = fetch_document(url, timeout=10, attempts=1)
        if len(d.text) >= min_chars:
            return d
    except Exception:
        pass
    if "web.archive.org" in url:
        raise LookupError(f"no usable copy of {url}")
    d = fetch_document(wayback_url(url), timeout=20, attempts=1, check_robots=False)
    d.publisher, d.tier = publisher(url), tier(url)
    return d


# ------------------------------------------------------------------ polite API access
_API_MIN_INTERVAL = 1.0


def api_get(url: str, params: dict | None = None, timeout: int = 20) -> requests.Response:
    """GET for JSON APIs (Wikipedia, Wikimedia): per-host pacing and 429/503 Retry-After handling."""
    host = urlparse(url).netloc
    for attempt in range(4):
        _pace(host, _API_MIN_INTERVAL)
        r = _session.get(url, params=params, timeout=timeout)
        if r.status_code in (429, 503) and attempt < 3:
            ra = r.headers.get("Retry-After", "")
            time.sleep(min(60.0, float(ra)) if ra.isdigit() else 5.0 * (attempt + 1))
            continue
        return r
    return r


# ------------------------------------------------------------------ Wikipedia
WIKI_API = "https://en.wikipedia.org/w/api.php"


def wikipedia(title: str) -> Document:
    r = api_get(WIKI_API, params={"action": "query", "prop": "extracts|info", "explaintext": 1,
                                  "inprop": "url", "redirects": 1, "titles": title, "format": "json"})
    r.raise_for_status()
    page = next(iter(r.json()["query"]["pages"].values()))
    if "missing" in page:
        raise LookupError(f"Wikipedia page not found: {title}")
    return Document(url=page.get("fullurl", f"https://en.wikipedia.org/wiki/{title}"),
                    publisher="wikipedia.org", tier=3, title=page["title"], text=page["extract"])


_SKIP_LINK = ("wikipedia.org", "wikidata", "wikimedia", "doi.org", "worldcat", "issn.org", "isbn",
              "books.google", "archive.today", "jstor.org/action", "ghostarchive", "youtube.com")


def wikipedia_references(title: str, limit: int = 500) -> list[str]:
    r = api_get(WIKI_API, params={"action": "query", "prop": "extlinks", "titles": title,
                                  "ellimit": limit, "redirects": 1, "format": "json"})
    r.raise_for_status()
    page = next(iter(r.json()["query"]["pages"].values()))
    links = [l["*"] for l in page.get("extlinks", [])]
    return [l for l in links if l.startswith("http") and not any(s in l for s in _SKIP_LINK)]


def rank_references(urls: list[str]) -> list[str]:
    """Prefer higher tiers, originals over archives, one URL per original."""
    seen, out = set(), []
    for u in sorted(urls, key=lambda u: (tier(u), "web.archive.org" in u)):
        o = original_url(u)
        if o in seen:
            continue
        seen.add(o)
        out.append(u)
    return out


# ------------------------------------------------------------------ FRED (no API key needed)
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}&coed={end}"


def fred_series(series: str, start: str, end: str) -> list[tuple[str, float]]:
    url = FRED_CSV.format(series=series, start=start, end=end)
    text = fetch(url, check_robots=False)
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        val = row.get(series, "").strip()
        if val and val != ".":
            rows.append((row.get("observation_date") or row.get("DATE"), float(val)))
    if not rows:
        raise LookupError(f"FRED series {series} empty for {start}..{end}")
    return rows


def fred_url(series: str) -> str:
    return f"https://fred.stlouisfed.org/series/{series}"


def cache_path(name: str) -> Path:
    p = config.get().paths.cache / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p
