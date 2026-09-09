"""
Stage 2: Crawl.

Contract: docs/component-specs.md -> "src/pipeline/crawl.py"

Responsibility: fetch permitted public pages for a resolved entity, checking
the budget governor before every request, preferring a static fetch over
Playwright (headless browser) unless the page looks like a JS shell.

Source policy allow-list (reviewed against the real signalpost-sources.md):
the only domain implicitly permitted is the entity's own resolved official
site. Same-domain company-owned paths (/about, /careers, etc.) and
sitemap.xml-discovered pages are still that same domain, so carry no new
policy risk. `ats_domains` is the one deliberate exception: a small,
explicitly allow-listed set of official ATS platforms (Greenhouse, Lever,
Workable, Teamtailor) that may be fetched ONLY when linked directly from a
page already on the entity's own official domain -- the link itself is the
provenance (see `linked_from` on FetchedPage, and verify.py's acceptance
rule). Any *other* secondary source must be passed in explicitly via
`allowed_domains`/`secondary_urls` by a caller that has checked it against
the policy -- nothing outside these is fetched by default.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional
from urllib.parse import urljoin, urlsplit

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.urls import normalize_url
from src.storage.cache import ResponseCache

logger = logging.getLogger(__name__)

# Capped retry policy -- never retry indefinitely (component-specs.md).
MAX_FETCH_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.5

# Company-owned paths worth trying on the entity's own official-site domain,
# per the agent playbook ("Prioritise /about, /om-oss, /contact, /kontakt,
# /leadership, /ledelse, /locations, /careers, /jobs, /news and /investor")
# and the learning harness's matching list. Opt-in via `company_owned_paths`
# (see crawl() signature) -- not fetched unless a caller asks for them, so
# existing single-URL tests/behavior are unaffected by default.
DEFAULT_COMPANY_OWNED_PATHS = [
    "/about", "/om-oss", "/contact", "/kontakt", "/leadership", "/ledelse",
    "/locations", "/careers", "/jobs", "/news", "/investor",
]

# Keywords a sitemap.xml URL's path must contain to be worth spending budget
# on -- sitemaps commonly list hundreds of blog/product pages we don't want.
SITEMAP_PRIORITY_KEYWORDS = [
    "about", "om-oss", "contact", "kontakt", "team", "leadership", "ledelse",
    "location", "career", "jobs", "job", "news", "press", "investor",
]
MAX_SITEMAP_URLS = 15

# Official ATS (applicant tracking system) platforms companies commonly link
# their own careers page to. Only ever fetched when linked FROM the entity's
# own official domain -- see `ats_domains` on crawl() and module docstring.
DEFAULT_ATS_DOMAINS = {"greenhouse.io", "lever.co", "workable.com", "teamtailor.com", "myworkdayjobs.com"}

# A fetched page whose visible text (tags/scripts/styles stripped) is shorter
# than this is treated as a JS-shell candidate for the Playwright fallback.
# Real prose pages in fixtures/pages/ all clear this by a wide margin; a bare
# SPA root div does not -- see fixtures/pages/edge_cases/js_shell.html.
JS_SHELL_TEXT_THRESHOLD = 60

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_HREF_RE = re.compile(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)


@dataclass
class FetchedPage:
    url: str
    raw_html: str
    fetched_at: datetime
    fetch_state: EvidenceState
    # Set only for pages reached by following an outbound link from a page on
    # the entity's own official domain (see `ats_domains`) -- preserves the
    # link chain as evidence, per evaluator feedback.
    linked_from: Optional[str] = None


def _domain(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _domain_matches_any(domain: str, allowed: set[str]) -> bool:
    """True if `domain` equals one of `allowed`, or is a subdomain of one --
    ATS platforms are commonly linked via a subdomain (e.g.
    "boards.greenhouse.io" for the "greenhouse.io" allow-list entry)."""
    return any(domain == a or domain.endswith("." + a) for a in allowed)


def _visible_text_len(html: str) -> int:
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    stripped = _TAG_RE.sub(" ", stripped)
    return len(_WHITESPACE_RE.sub(" ", stripped).strip())


def _looks_like_js_shell(html: str) -> bool:
    return _visible_text_len(html) < JS_SHELL_TEXT_THRESHOLD


# Real-world regressions (found running 1,000-company batches): a site can
# return a redirect Location header with an empty hostname label (e.g.
# "https://.example.no/...", a server-side misconfiguration) -- httpx's
# redirect-following then raises a bare UnicodeError from the idna codec,
# not an httpx.TransportError. A site can also redirect in a loop, which
# raises httpx.TooManyRedirects -- an httpx.HTTPError, not a
# TransportError (though TransportError is itself an HTTPError subclass, so
# catching the broader type here covers both in one clause). Any malformed
# external response must degrade to FAILED, never propagate past this
# function and crash the company.
_FETCH_ERRORS = (httpx.HTTPError, UnicodeError)


def _fetch_static(
    client: httpx.Client, url: str, sleep: Callable[[float], None]
) -> Optional[httpx.Response]:
    response = None
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            response = client.get(url, timeout=10.0, follow_redirects=True)
            if response.status_code >= 500 and attempt < MAX_FETCH_RETRIES - 1:
                sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return response
        except _FETCH_ERRORS:
            response = None
            if attempt < MAX_FETCH_RETRIES - 1:
                sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return response


def _state_for_status(status_code: int) -> Optional[EvidenceState]:
    """None means "treat as a successful fetch, proceed to content checks"."""
    if 200 <= status_code < 300:
        return None
    if status_code == 404:
        return EvidenceState.NOT_AVAILABLE
    if status_code in (401, 403, 429):
        return EvidenceState.BLOCKED
    return EvidenceState.FAILED


def _fetch_text_cached(
    url: str,
    client: httpx.Client,
    sleep: Callable[[float], None],
    budget: BudgetGovernor,
    cache: Optional[ResponseCache],
    date_bucket: str,
) -> Optional[str]:
    """Best-effort GET for meta-discovery fetches (robots.txt, sitemap.xml)
    that aren't themselves content pages -- 200 body text on success, None on
    anything else (missing, blocked, failed, budget exhausted)."""
    if cache is not None:
        hit = cache.get(url, date_bucket)
        if hit is not None:
            return hit
    if not budget.can_spend_request():
        return None
    response = _fetch_static(client, url, sleep)
    budget.record_request()
    if response is None or response.status_code != 200:
        return None
    if cache is not None:
        cache.put(url, date_bucket, response.text)
    return response.text


def _parse_robots_disallow(robots_txt: str) -> tuple[set[str], list[str]]:
    """Returns (disallowed_paths, sitemap_urls) for the "User-agent: *" block."""
    disallowed: set[str] = set()
    sitemap_urls: list[str] = []
    applies_to_all = False
    for raw_line in robots_txt.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            applies_to_all = value == "*"
        elif key == "disallow" and applies_to_all and value:
            disallowed.add(value)
        elif key == "sitemap" and value:
            sitemap_urls.append(value)
    return disallowed, sitemap_urls


def _is_priority_sitemap_url(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(keyword in path for keyword in SITEMAP_PRIORITY_KEYWORDS)


def _discover_sitemap_urls(
    base_url: str,
    client: httpx.Client,
    sleep: Callable[[float], None],
    budget: BudgetGovernor,
    cache: Optional[ResponseCache],
    date_bucket: str,
) -> list[str]:
    """robots.txt -> Disallow rules + declared Sitemap: location, then parse
    the sitemap for same-domain, priority-keyword, not-disallowed URLs.
    Entirely best-effort: any missing/malformed step just yields no results,
    never raises -- discovery is a bonus, not a required step."""
    domain = _domain(base_url)
    robots_url = urljoin(base_url, "/robots.txt")
    robots_txt = _fetch_text_cached(robots_url, client, sleep, budget, cache, date_bucket)

    disallowed: set[str] = set()
    sitemap_locations: list[str] = []
    if robots_txt:
        disallowed, sitemap_locations = _parse_robots_disallow(robots_txt)

    sitemap_url = sitemap_locations[0] if sitemap_locations else urljoin(base_url, "/sitemap.xml")
    sitemap_xml = _fetch_text_cached(sitemap_url, client, sleep, budget, cache, date_bucket)
    if not sitemap_xml:
        return []

    discovered = []
    for loc in _LOC_RE.findall(sitemap_xml):
        if _domain(loc) != domain:
            continue
        path = urlsplit(loc).path
        if any(path == d or path.startswith(d) for d in disallowed):
            logger.warning("crawl: skipping robots.txt-disallowed sitemap URL %s", loc)
            continue
        if _is_priority_sitemap_url(loc):
            discovered.append(loc)
    return discovered[:MAX_SITEMAP_URLS]


def _extract_outbound_links(html: str, page_url: str) -> list[str]:
    return [urljoin(page_url, href) for href in _HREF_RE.findall(html)]


def crawl(
    entity: ResolvedEntity,
    budget: BudgetGovernor,
    client: Optional[httpx.Client] = None,
    secondary_urls: Optional[list[str]] = None,
    allowed_domains: Optional[set[str]] = None,
    company_owned_paths: Optional[list[str]] = None,
    use_sitemap: bool = False,
    ats_domains: Optional[set[str]] = None,
    cache: Optional[ResponseCache] = None,
    render_js: Optional[Callable[[str], str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    date_bucket: Optional[str] = None,
) -> list[FetchedPage]:
    """
    Fetch permitted pages for `entity`, stopping this company's crawl (not the
    whole batch) once its per-company budget allocation is spent.

    `company_owned_paths`, if given, are joined onto the entity's own
    official-site domain and fetched alongside it -- same domain, no new
    allow-list risk. `use_sitemap`, if True, additionally reads robots.txt
    (respecting Disallow) and sitemap.xml to discover real same-domain pages
    instead of guessing a fixed path list. `ats_domains`, if given, follows
    outbound links found on official-domain pages to those specific external
    domains only (see module docstring). `cache`, if given, makes a repeat
    fetch of the same (url, date_bucket) free against the request budget.
    All opt-in (default None/False) so existing single-URL callers/tests are
    unaffected.

    Must:
      - Check `budget` before every request.
      - Try static fetch first; escalate to Playwright only on detected JS-shell.
      - Respect the source policy allow-list.
    Must not:
      - Fetch a URL outside the allow-list.
      - Retry indefinitely -- cap retries, record BLOCKED/FAILED on give-up.
    """
    if not entity.official_site_candidate:
        return []

    owns_client = client is None
    client = client or httpx.Client()
    date_bucket = date_bucket or now().strftime("%Y-%m-%d")
    allowed = {_domain(entity.official_site_candidate)} | {d.lower() for d in (allowed_domains or set())}
    ats_domains = {d.lower() for d in (ats_domains or set())}

    try:
        company_owned_urls = [urljoin(entity.official_site_candidate, path) for path in (company_owned_paths or [])]
        sitemap_urls = (
            _discover_sitemap_urls(entity.official_site_candidate, client, sleep, budget, cache, date_bucket)
            if use_sitemap
            else []
        )
        candidate_urls = [entity.official_site_candidate] + company_owned_urls + sitemap_urls + list(secondary_urls or [])

        seen_normalized: set[str] = set()
        to_fetch: list[str] = []
        for url in candidate_urls:
            normalized = normalize_url(url)
            if normalized in seen_normalized:
                continue
            if _domain(url) in allowed:
                seen_normalized.add(normalized)
                to_fetch.append(url)
            else:
                logger.warning("crawl: skipping disallowed URL %s (not in source-policy allow-list)", url)

        pages: list[FetchedPage] = []
        budget_exhausted = False
        queue: list[tuple[str, Optional[str]]] = [(url, None) for url in to_fetch]

        while queue:
            url, linked_from = queue.pop(0)
            normalized = normalize_url(url)
            if linked_from is not None:
                if normalized in seen_normalized:
                    continue
                seen_normalized.add(normalized)

            # Cache hits are free against the budget (docs/component-specs.md
            # "cache hits free") -- must be checked BEFORE the exhaustion
            # gate below, not after, or an exhausted budget would block a
            # fetch that costs nothing.
            cached_html = cache.get(url, date_bucket) if cache is not None else None

            if cached_html is None and (budget_exhausted or not budget.can_spend_request()):
                budget_exhausted = True
                pages.append(FetchedPage(url=url, raw_html="", fetched_at=now(), fetch_state=EvidenceState.BLOCKED, linked_from=linked_from))
                continue

            if cached_html is not None:
                html = cached_html
                status_state = None
            else:
                response = _fetch_static(client, url, sleep)
                budget.record_request()

                if response is None:
                    pages.append(FetchedPage(url=url, raw_html="", fetched_at=now(), fetch_state=EvidenceState.FAILED, linked_from=linked_from))
                    continue

                status_state = _state_for_status(response.status_code)
                if status_state is not None:
                    pages.append(FetchedPage(url=url, raw_html="", fetched_at=now(), fetch_state=status_state, linked_from=linked_from))
                    continue

                html = response.text

            if _looks_like_js_shell(html) and render_js is not None:
                if budget.can_spend_request():
                    html = render_js(url)
                    budget.record_request()
                else:
                    budget_exhausted = True

            if cache is not None and cached_html is None:
                cache.put(url, date_bucket, html)

            pages.append(FetchedPage(url=url, raw_html=html, fetched_at=now(), fetch_state=EvidenceState.AVAILABLE, linked_from=linked_from))

            if ats_domains and linked_from is None:
                for link in _extract_outbound_links(html, url):
                    if _domain_matches_any(_domain(link), ats_domains) and normalize_url(link) not in seen_normalized:
                        queue.append((link, url))

        return pages
    finally:
        if owns_client:
            client.close()
