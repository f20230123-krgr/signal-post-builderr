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
from src.pipeline.net import UnsafeOutboundUrl, new_client
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
#
# "/aktuelt" and "/nyheter" (Norwegian for "current/topical" and "news")
# added after independent review: a Norwegian-only site can have neither
# "/news" nor anything matching an existing keyword under these names --
# unlike "/presse" ("press" is already a substring match), there was no
# coverage at all for this common Norwegian page-naming pattern.
#
# Trimmed from 13 paths to the 4 that earn their requests. Builderr counts every
# request against a 2,000-per-run limit (redirects and retries included), and on
# the 1,000-company corpus (293 sites) the nine dropped guesses produced claims on
# only 5 pages between them (~0.1% of claims) while costing ~9 requests per site.
# The kept four (/kontakt, /om-oss, /about, /contact) produced 25 of the 30
# claim-bearing guessed pages. Real pages under the dropped names are still found
# through the sitemap and the pages' own links.
DEFAULT_COMPANY_OWNED_PATHS = ["/kontakt", "/om-oss", "/about", "/contact"]

# After this many failed fetches in a row on one domain (5xx, timeouts, 429),
# the rest of that site is skipped: a real batch spent 32 requests on a single
# server that answered 503 to every page.
MAX_CONSECUTIVE_DOMAIN_FAILURES = 3

# Keywords a sitemap.xml URL's path must contain to be worth spending budget
# on -- sitemaps commonly list hundreds of blog/product pages we don't want.
SITEMAP_PRIORITY_KEYWORDS = [
    "about", "om-oss", "contact", "kontakt", "team", "leadership", "ledelse",
    "location", "career", "jobs", "job", "news", "press", "investor",
    "aktuelt", "nyheter",
]
# Raised from 15: real batches consistently use well under half the 2,000-
# request/100-company budget (~1,000-1,100 typical), and this cap only ever
# binds for the minority of companies that both have a registered site AND
# a sitemap listing more than 15 priority-keyword pages -- there's room to
# check more of exactly that same already-vetted, same-domain content
# without meaningful budget risk.
MAX_SITEMAP_URLS = 20

# Official ATS (applicant tracking system) platforms companies commonly link
# their own careers page to. Only ever fetched when linked FROM the entity's
# own official domain -- see `ats_domains` on crawl() and module docstring.
#
# "recman.no" and "jobylon.com" added after independent review, verified
# LIVE before adding -- unlike webcruiter.no (checked the same way, found to
# render NO JSON-LD or microdata at all: only basic OpenGraph tags, so
# adding it would spend crawl budget for zero extraction benefit), a real
# recman.no listing (apply.recman.no, for POWER Norge AS) and a real
# jobylon.com listing (emp.jobylon.com, for Hotel Norge by Scandic) were
# each fetched and inspected directly, and both render a genuine
# "@type": "JobPosting" JSON-LD block. Norwegian-market platforms, unlike
# the rest of this list, which is mostly US-centric.
DEFAULT_ATS_DOMAINS = {
    "greenhouse.io", "lever.co", "workable.com", "teamtailor.com", "myworkdayjobs.com",
    "recman.no", "jobylon.com",
}

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
    client: httpx.Client, url: str, sleep: Callable[[float], None], retry: bool = True
) -> Optional[httpx.Response]:
    response = None
    attempts = MAX_FETCH_RETRIES if retry else 1
    for attempt in range(attempts):
        try:
            response = client.get(url, timeout=10.0, follow_redirects=True)
            if response.status_code >= 500 and attempt < attempts - 1:
                sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return response
        except UnsafeOutboundUrl:
            # A URL the outbound policy refuses is refused every time; retrying
            # it would only burn backoff time.
            return None
        except httpx.TooManyRedirects:
            # A redirect loop is deterministic: a retry just walks the same loop
            # again (the cost of one such site was ~190 requests).
            return None
        except _FETCH_ERRORS:
            response = None
            if attempt < attempts - 1:
                sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return response


def _rebased(url: str, rebase: dict[str, str]) -> str:
    """Point `url` at the host its site was seen to live on (see crawl())."""
    parts = urlsplit(url)
    target = rebase.get(f"{parts.scheme}://{parts.netloc}")
    if target is None:
        return url
    return target + url[len(f"{parts.scheme}://{parts.netloc}"):]


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
    # A <loc> entry ending in .xml is always itself another sitemap file
    # (e.g. a sitemap index pointing at "/sitemap/news/sitemap.xml"), never
    # a real content page -- real-world regression, found auditing a real
    # 100-company batch: "news" is a priority keyword, so that nested
    # sitemap URL was crawled and extract()'d as if it were an HTML page.
    # With no HTML block tags to split on, its raw XML (hundreds of
    # concatenated <loc>/<lastmod> pairs) became a single giant garbage
    # dated_activity fact. Must be excluded before the keyword check, not
    # after -- a real page's path essentially never ends in .xml.
    if path.endswith(".xml"):
        return False
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


_FEED_LINK_RE = re.compile(
    r'<link\b[^>]*\btype\s*=\s*["\']application/(?:rss|atom)\+xml["\'][^>]*>', re.IGNORECASE
)
_FEED_HREF_RE = re.compile(r'\bhref\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)

# One feed is the company's news channel; a site declaring many (per-category
# feeds, comment feeds) would otherwise turn into a crawl of its own.
MAX_FEEDS_PER_SITE = 1


def _discover_feed_urls(html: str, page_url: str) -> list[str]:
    """Absolute URLs of RSS/Atom feeds this page explicitly declares."""
    urls: list[str] = []
    for tag in _FEED_LINK_RE.findall(html):
        href = _FEED_HREF_RE.search(tag)
        if not href:
            continue
        urls.append(urljoin(page_url, href.group(1).strip()))
        if len(urls) >= MAX_FEEDS_PER_SITE:
            break
    return urls


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
    client = client or new_client()
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
        # Consecutive failed fetches per domain, and where a site redirected to
        # (example.no -> www.example.no): once the first page shows where the
        # site really lives, later pages are requested there directly instead
        # of paying for the same redirect hop on every page.
        failures: dict[str, int] = {}
        rebase: dict[str, str] = {}

        while queue:
            url, linked_from = queue.pop(0)
            url = _rebased(url, rebase)
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

            domain = _domain(url)
            if cached_html is None and failures.get(domain, 0) >= MAX_CONSECUTIVE_DOMAIN_FAILURES:
                pages.append(FetchedPage(url=url, raw_html="", fetched_at=now(), fetch_state=EvidenceState.FAILED, linked_from=linked_from))
                continue

            if cached_html is not None:
                html = cached_html
                status_state = None
            else:
                # A domain that has already failed once isn't retried again:
                # the first page gets its second chance, the rest don't.
                response = _fetch_static(client, url, sleep, retry=failures.get(domain, 0) == 0)
                budget.record_request()

                if response is None or response.status_code >= 500 or response.status_code == 429:
                    failures[domain] = failures.get(domain, 0) + 1
                else:
                    failures[domain] = 0

                if response is None:
                    pages.append(FetchedPage(url=url, raw_html="", fetched_at=now(), fetch_state=EvidenceState.FAILED, linked_from=linked_from))
                    continue

                if 200 <= response.status_code < 300:
                    sent, landed = urlsplit(url), urlsplit(str(response.url))
                    if (sent.scheme, sent.netloc) != (landed.scheme, landed.netloc) and _domain(str(response.url)) in allowed:
                        rebase[f"{sent.scheme}://{sent.netloc}"] = f"{landed.scheme}://{landed.netloc}"

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

            # A declared RSS/Atom feed is the company's own published
            # activity, on its own domain -- no new allow-list surface. Only
            # followed when the page actually declares one (never guessed at
            # /feed or /rss), and only from the entity's own pages, so a
            # company without a feed costs nothing.
            if linked_from is None:
                for feed_url in _discover_feed_urls(html, url):
                    if _domain(feed_url) in allowed and normalize_url(feed_url) not in seen_normalized:
                        queue.append((feed_url, None))
                        seen_normalized.add(normalize_url(feed_url))

        return pages
    finally:
        if owns_client:
            client.close()
