"""Crawlability detection, source credibility, fetching and text extraction.

The crawlability agent runs *before* any page is fetched. Its policy, in order:
  1. Scheme/URL sanity (http/https only, no private hosts).
  2. Terms-of-use policy list: platforms whose terms prohibit automated extraction
     (social networks, some academic aggregators, paywalled data vendors) are refused outright.
  3. robots.txt for the domain, evaluated for our own user-agent token and for "*".
     - 404 / no robots.txt  -> allowed (standard convention)
     - 401 / 403            -> treated as "disallow all"
     - timeout / 5xx        -> refused (conservative: unknown permission is not permission)
  4. After fetch, page-level opt-outs are honoured too: `X-Robots-Tag` / `<meta name="robots">`
     containing noai / noimageai / none causes the content to be discarded.
Every decision, with its reason, is stored on the `sources` row and shown in the UI.
"""
from __future__ import annotations

import asyncio
import io
import ipaddress
import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

from ..config import settings

log = logging.getLogger(__name__)

# Domains whose terms of service prohibit automated collection, or that require login.
TOS_BLOCKLIST = {
    "facebook.com": "Terms of service prohibit automated data collection",
    "instagram.com": "Terms of service prohibit automated data collection",
    "linkedin.com": "Terms of service prohibit scraping (login-walled)",
    "x.com": "Terms of service prohibit crawling without consent",
    "twitter.com": "Terms of service prohibit crawling without consent",
    "tiktok.com": "Terms of service prohibit automated data collection",
    "youtube.com": "Video platform; terms prohibit automated access",
    "researchgate.net": "Terms of service prohibit automated downloading",
    "statista.com": "Paywalled data vendor; terms prohibit extraction",
    "scribd.com": "Terms of service prohibit scraping",
    "quora.com": "Terms of service prohibit scraping",
    "reddit.com": "API terms require authorised access",
    "glassdoor.com": "Terms of service prohibit scraping",
}

TIER1_PATTERNS = (
    r"\.gov(\.[a-z]{2})?$", r"\.gob\.[a-z]{2}$", r"\.gouv\.[a-z]{2}$", r"\.go\.[a-z]{2}$", r"\.gv\.[a-z]{2}$",
    r"\.nic\.in$", r"\.govt\.nz$", r"\.admin\.ch$", r"\.bund\.de$", r"\.europa\.eu$",
    r"(^|\.)who\.int$", r"(^|\.)paho\.org$", r"(^|\.)worldbank\.org$", r"(^|\.)un\.org$", r"(^|\.)unicef\.org$",
    r"(^|\.)healthdata\.org$", r"(^|\.)ncdrisc\.org$", r"(^|\.)ncbi\.nlm\.nih\.gov$", r"(^|\.)nih\.gov$",
    r"(^|\.)thelancet\.com$", r"(^|\.)bmj\.com$", r"(^|\.)nature\.com$", r"(^|\.)sciencedirect\.com$",
    r"(^|\.)springer\.com$", r"(^|\.)wiley\.com$", r"(^|\.)plos\.org$", r"(^|\.)ahajournals\.org$",
    r"(^|\.)oup\.com$", r"(^|\.)frontiersin\.org$", r"(^|\.)biomedcentral\.com$", r"(^|\.)jamanetwork\.com$",
    r"\.edu(\.[a-z]{2})?$", r"\.ac\.[a-z]{2}$", r"(^|\.)ourworldindata\.org$", r"(^|\.)oecd\.org$",
)
TIER2_PATTERNS = (
    r"(^|\.)cardio4cities\.org$", r"(^|\.)novartisfoundation\.org$", r"(^|\.)worldheartfederation\.org$",
    r"(^|\.)resolvetosavelives\.org$", r"(^|\.)mdpi\.com$", r"(^|\.)cureus\.com$", r"\.org(\.[a-z]{2})?$",
    r"(^|\.)reuters\.com$", r"(^|\.)bbc\.(com|co\.uk)$", r"(^|\.)theguardian\.com$", r"(^|\.)apnews\.com$",
    r"(^|\.)wikipedia\.org$",
)


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def credibility(url: str) -> tuple[int, str]:
    d = domain_of(url)
    if "wikipedia.org" in d:
        return 3, "Encyclopaedia: useful for context and leads, not as primary evidence"
    if any(re.search(p, d) for p in TIER1_PATTERNS):
        return 1, "Official, multilateral or peer-reviewed source"
    if any(re.search(p, d) for p in TIER2_PATTERNS):
        return 2, "Established organisation or reputable media"
    return 3, "Other / unverified publisher"


@dataclass
class CrawlDecision:
    allowed: bool
    reason: str


class CrawlabilityAgent:
    """Decides, per URL, whether automated extraction is permitted. Caches robots.txt per host."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client
        self._robots: dict[str, tuple[urllib.robotparser.RobotFileParser | None, str]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=10, follow_redirects=True,
                                             headers={"User-Agent": settings.user_agent})
        return self._client

    @staticmethod
    def _host_is_private(host: str) -> bool:
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            return host in ("localhost",) or host.endswith(".local") or host.endswith(".internal")

    async def _robots_for(self, scheme: str, host: str) -> tuple[urllib.robotparser.RobotFileParser | None, str]:
        key = f"{scheme}://{host}"
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if key in self._robots:
                return self._robots[key]
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = await self._http().get(f"{key}/robots.txt")
                if r.status_code in (401, 403):
                    rp.disallow_all = True
                    result = (rp, f"robots.txt returned HTTP {r.status_code} (treated as disallow-all)")
                elif r.status_code >= 500:
                    result = (None, f"robots.txt unavailable (HTTP {r.status_code}); permission unknown")
                elif r.status_code >= 400:
                    rp.allow_all = True
                    result = (rp, "No robots.txt (HTTP %d): no restrictions declared" % r.status_code)
                else:
                    rp.parse(r.text.splitlines())
                    result = (rp, "robots.txt parsed")
            except Exception as exc:  # noqa: BLE001
                result = (None, f"robots.txt could not be retrieved ({type(exc).__name__}); permission unknown")
            self._robots[key] = result
            return result

    async def check(self, url: str) -> CrawlDecision:
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return CrawlDecision(False, "Unsupported URL scheme")
        if self._host_is_private(p.hostname):
            return CrawlDecision(False, "Private or local address")
        d = domain_of(url)
        for blocked, why in TOS_BLOCKLIST.items():
            if d == blocked or d.endswith("." + blocked):
                return CrawlDecision(False, f"Terms-of-use policy: {why}")
        rp, note = await self._robots_for(p.scheme, p.hostname)
        if rp is None:
            return CrawlDecision(False, note)
        ok_ours = rp.can_fetch(settings.robots_agent_token, url)
        ok_any = rp.can_fetch("*", url)
        if ok_ours and ok_any:
            delay = rp.crawl_delay(settings.robots_agent_token) or rp.crawl_delay("*")
            extra = f"; crawl-delay {delay}s honoured" if delay else ""
            return CrawlDecision(True, f"{note}: path allowed for our agent{extra}")
        who = "our user-agent" if not ok_ours else "all user-agents (*)"
        return CrawlDecision(False, f"{note}: path disallowed for {who}")


# --- fetching ----------------------------------------------------------------------------------
@dataclass
class FetchResult:
    ok: bool
    final_url: str = ""
    status: int | None = None
    content_type: str = ""
    title: str = ""
    published: str = ""
    text: str = ""
    error: str = ""


OPT_OUT_TOKENS = ("noai", "noimageai", "none")


class FetcherProtocol(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...


class Fetcher:
    MAX_BYTES = 6_000_000

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=settings.fetch_timeout_s, follow_redirects=True,
                                        headers={"User-Agent": settings.user_agent,
                                                 "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5"})
        self._last_hit: dict[str, float] = {}

    async def fetch(self, url: str) -> FetchResult:
        host = domain_of(url)
        wait = 1.0 - (time.monotonic() - self._last_hit.get(host, 0))  # politeness: 1 req/s per host
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_hit[host] = time.monotonic()
        try:
            r = await self.client.get(url)
        except Exception as exc:  # noqa: BLE001
            return FetchResult(False, error=f"{type(exc).__name__}: {exc}"[:300])
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        res = FetchResult(False, final_url=str(r.url), status=r.status_code, content_type=ctype)
        if r.status_code >= 400:
            res.error = f"HTTP {r.status_code}"
            return res
        xrt = r.headers.get("x-robots-tag", "").lower()
        if any(t in xrt for t in OPT_OUT_TOKENS):
            res.error = f"Page opts out of automated use (X-Robots-Tag: {xrt})"
            return res
        body = r.content[: self.MAX_BYTES]
        try:
            if "pdf" in ctype or url.lower().endswith(".pdf"):
                res.text, res.title = extract_pdf(body)
            else:
                html = body.decode(r.encoding or "utf-8", errors="replace")
                meta_robots = re.search(r'<meta[^>]+name=["\']robots["\'][^>]*content=["\']([^"\']+)', html, re.I)
                if meta_robots and any(t in meta_robots.group(1).lower() for t in OPT_OUT_TOKENS):
                    res.error = f"Page opts out of automated use (meta robots: {meta_robots.group(1)})"
                    return res
                res.text, res.title, res.published = extract_html(html, str(r.url))
        except Exception as exc:  # noqa: BLE001
            res.error = f"Extraction failed: {type(exc).__name__}"
            return res
        res.ok = len(res.text) >= 200
        if not res.ok:
            res.error = "No substantive text extracted"
        return res


def extract_html(html: str, url: str) -> tuple[str, str, str]:
    import trafilatura

    text = trafilatura.extract(html, url=url, include_tables=True, include_comments=False, favor_recall=True) or ""
    title, published = "", ""
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta is not None:
            title = meta.title or ""
            published = (meta.date or "")[:10]
    except Exception:  # noqa: BLE001
        pass
    return text, title, published


def extract_pdf(data: bytes, max_pages: int = 40) -> tuple[str, str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages[:max_pages]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    title = ""
    try:
        title = (reader.metadata.title or "") if reader.metadata else ""
    except Exception:  # noqa: BLE001
        pass
    text = re.sub(r"[ \t]+", " ", "\n".join(parts))
    return re.sub(r"\n{3,}", "\n\n", text).strip(), title


def relevant_excerpt(text: str, city: str, max_chars: int) -> str:
    """Pick the parts of a long document most likely to be about this city and CVD topics.

    Keeps paragraphs in document order; scores by city mentions and topic keywords.
    """
    if len(text) <= max_chars:
        return text
    paras = [p for p in re.split(r"\n\s*\n|\n(?=[A-Z0-9•\-])", text) if p.strip()]
    kw = ("hypertens", "blood pressure", "diabet", "cholesterol", "lipid", "cardio", "stroke", "heart",
          "non-communicable", "ncd", "primary care", "programme", "program", "policy", "strategy", "mortality",
          "prevalence", "health", "secretary", "minister", "mayor", "commissioner", "department")
    city_l = city.lower()
    scored = []
    for i, p in enumerate(paras):
        pl = p.lower()
        s = 3 * pl.count(city_l) + sum(pl.count(k) for k in kw)
        scored.append((s, i, p))
    budget, keep = max_chars, set()
    for s, i, p in sorted(scored, key=lambda x: (-x[0], x[1])):
        if s == 0 and keep:
            break
        if len(p) + 2 > budget:
            continue
        keep.add(i)
        budget -= len(p) + 2
    return "\n\n".join(p for s, i, p in scored if i in keep)


def normalise(s: str) -> str:
    s = s.lower().replace("’", "'").replace("–", "-").replace("—", "-")
    s = re.sub(r"[^\w%.,'\- ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def locate_quote(quote: str, text: str) -> tuple[bool, int]:
    """Deterministic check that a quote really occurs in the source text.

    Exact (normalised) match first; otherwise require >=85% of the quote's word 4-grams to occur
    in the source (tolerates PDF line-break and hyphenation noise, not paraphrase).
    Returns (found, approx_char_position_in_normalised_text).
    """
    q, t = normalise(quote), normalise(text)
    if len(q) < 12:
        return False, -1
    pos = t.find(q)
    if pos != -1:
        return True, pos
    qw, tw = q.split(), t.split()
    if len(qw) < 5:
        return False, -1
    grams = [" ".join(qw[i:i + 4]) for i in range(len(qw) - 3)]
    hits = [t.find(g) for g in grams]
    found = [h for h in hits if h != -1]
    if len(found) / len(grams) >= 0.85:
        return True, min(found)
    return False, -1


def numbers_in(s: str) -> set[str]:
    out = set()
    for m in re.findall(r"\d[\d,\.]*", s):
        m = m.rstrip(".,")
        if not m:
            continue
        out.add(m.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", m) else m.replace(",", "."))
    return out


def passage_around(text: str, quote: str, window: int = 1500) -> str:
    """Return the source passage surrounding a quote (for the independent fact checker)."""
    found, pos = locate_quote(quote, text)
    t = normalise(text)
    if not found:
        return ""
    start = max(0, pos - window)
    return t[start: pos + len(normalise(quote)) + window]


def meta_json(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v not in (None, "")}
