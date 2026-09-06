"""
Stage 2: Crawl.

Contract: docs/component-specs.md -> "src/pipeline/crawl.py"

Responsibility: fetch permitted public pages for a resolved entity, checking
the budget governor before every request, preferring a static fetch over
Playwright (headless browser) unless the page looks like a JS shell.

Source policy allow-list (reviewed against the real signalpost-sources.md):
the only domain implicitly permitted is the entity's own resolved official
site. Same-domain company-owned paths (/about, /careers, etc. -- explicitly
named in the agent playbook and learning harness) are opt-in via
`company_owned_paths`, since they're still the same domain and carry no new
policy risk. Any *other* secondary source must be passed in explicitly via
`allowed_domains`/`secondary_urls` by a caller that has checked it against the
policy -- nothing outside the official site's own domain is fetched by default.
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

# A fetched page whose visible text (tags/scripts/styles stripped) is shorter
# than this is treated as a JS-shell candidate for the Playwright fallback.
# Real prose pages in fixtures/pages/ all clear this by a wide margin; a bare
# SPA root div does not -- see fixtures/pages/edge_cases/js_shell.html.
JS_SHELL_TEXT_THRESHOLD = 60

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class FetchedPage:
    url: str
    raw_html: str
    fetched_at: datetime
    fetch_state: EvidenceState


def _domain(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _visible_text_len(html: str) -> int:
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    stripped = _TAG_RE.sub(" ", stripped)
    return len(_WHITESPACE_RE.sub(" ", stripped).strip())


def _looks_like_js_shell(html: str) -> bool:
    return _visible_text_len(html) < JS_SHELL_TEXT_THRESHOLD


# Real-world regression (found running a 1,000-company batch): a site can
# return a redirect Location header with an empty hostname label (e.g.
# "https://.example.no/...", a server-side misconfiguration) -- httpx's
# redirect-following then raises a bare UnicodeError from the idna codec,
# not an httpx.TransportError. Any malformed external response must degrade
# to FAILED, never propagate past this function and crash the company.
_FETCH_ERRORS = (httpx.TransportError, UnicodeError)


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


def crawl(
    entity: ResolvedEntity,
    budget: BudgetGovernor,
    client: Optional[httpx.Client] = None,
    secondary_urls: Optional[list[str]] = None,
    allowed_domains: Optional[set[str]] = None,
    company_owned_paths: Optional[list[str]] = None,
    render_js: Optional[Callable[[str], str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[FetchedPage]:
    """
    Fetch permitted pages for `entity`, stopping this company's crawl (not the
    whole batch) once its per-company budget allocation is spent.

    `company_owned_paths`, if given, are joined onto the entity's own
    official-site domain (e.g. "/careers" -> "https://site.no/careers") and
    fetched alongside it -- same domain, no new allow-list risk. Opt-in
    (default None) so existing single-URL callers are unaffected; see
    DEFAULT_COMPANY_OWNED_PATHS for the recommended list.

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
    allowed = {_domain(entity.official_site_candidate)} | {d.lower() for d in (allowed_domains or set())}

    company_owned_urls = [urljoin(entity.official_site_candidate, path) for path in (company_owned_paths or [])]
    candidate_urls = [entity.official_site_candidate] + company_owned_urls + list(secondary_urls or [])
    to_fetch = []
    for url in candidate_urls:
        if _domain(url) in allowed:
            to_fetch.append(url)
        else:
            logger.warning("crawl: skipping disallowed URL %s (not in source-policy allow-list)", url)

    pages: list[FetchedPage] = []
    try:
        budget_exhausted = False
        for url in to_fetch:
            if budget_exhausted or not budget.can_spend_request():
                budget_exhausted = True
                pages.append(FetchedPage(url=url, raw_html="", fetched_at=now(), fetch_state=EvidenceState.BLOCKED))
                continue

            response = _fetch_static(client, url, sleep)
            budget.record_request()

            if response is None:
                pages.append(FetchedPage(url=url, raw_html="", fetched_at=now(), fetch_state=EvidenceState.FAILED))
                continue

            status_state = _state_for_status(response.status_code)
            if status_state is not None:
                pages.append(FetchedPage(url=url, raw_html="", fetched_at=now(), fetch_state=status_state))
                continue

            html = response.text
            if _looks_like_js_shell(html) and render_js is not None:
                if budget.can_spend_request():
                    html = render_js(url)
                    budget.record_request()
                else:
                    budget_exhausted = True

            pages.append(FetchedPage(url=url, raw_html=html, fetched_at=now(), fetch_state=EvidenceState.AVAILABLE))
        return pages
    finally:
        if owns_client:
            client.close()
