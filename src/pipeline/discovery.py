"""
Candidate website discovery for companies with no official site on file in
the registry (~89% of a random sample of the company universe -- see the
2026-09 evaluator feedback exchange).

Source policy: "search/discovery providers for candidate generation only,
never as evidence itself." Two strictly separate steps, matching that rule:

  1. `discover_candidate_site` -- asks a search provider for ONE candidate
     URL. Never trusted on its own.
  2. `verify_discovered_site` -- independently fetches that candidate and
     requires the exact same RapidFuzz name-match threshold verify.py uses
     for any other claim before accepting it. A hit that turns out to be the
     wrong company (or nothing at all) is simply discarded -- same "reject,
     don't guess" discipline as every other stage, regardless of which
     provider produced the candidate.

Provider chain, tried in order, each skipped (no request, no budget spent)
when its API key isn't configured:

  1. Exa (https://api.exa.ai/search) -- EXA_API_KEY. Best free tier checked
     (20,000 requests/month, no card required) as of 2026-09.
  2. Parallel (https://api.parallel.ai/v1/search) -- PARALLEL_API_KEY.
     Also generous (~5,000/month, no card), and supports native source
     inclusion/exclusion -- not yet used here, but a natural place to plug
     in an aggregator blacklist (proff.no, purehelp.no, etc.) if needed.
  3. DuckDuckGo's Instant Answer API -- always available, free, no key, but
     only returns a "Website" field when the query matches a Wikipedia
     infobox, i.e. only for companies notable enough to have a Wikipedia
     page. Measured 0/15 hit rate on real non-notable Norwegian companies
     -- kept as a last-resort, zero-cost fallback, not a real solution.

Both API keys are read from the environment (EXA_API_KEY / PARALLEL_API_KEY)
unless explicitly overridden -- per the real evaluation-harness brief,
"server-side secrets supplied through documented environment variables
only." Never hardcode a key or commit one to the repo.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from src.orchestrator.budget import BudgetGovernor
from src.pipeline.extract import structured_facts, text_fallback_facts
from src.pipeline.verify import NAME_MATCH_THRESHOLD, ConfirmedFact, name_similarity

DUCKDUCKGO_URL = "https://api.duckduckgo.com/"
EXA_URL = "https://api.exa.ai/search"
PARALLEL_URL = "https://api.parallel.ai/v1/search"
MAX_FETCH_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.5

_WEBSITE_VALUE_RE = re.compile(r"\[?([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\]?")

# Norwegian (and a couple of general) legal-entity suffixes, stripped before
# querying. Real-world regression: DuckDuckGo's Instant Answer infobox match
# is keyed to Wikipedia article titles ("Equinor"), not the legal name with
# its entity suffix ("Equinor ASA") -- querying with the suffix intact
# returns nothing at all, even when a real Wikipedia infobox exists.
_LEGAL_SUFFIXES = ["ASA", "AS", "ANS", "DA", "BA", "SA", "ENK", "NUF"]
_LEGAL_SUFFIX_RE = re.compile(r"\s+(" + "|".join(_LEGAL_SUFFIXES) + r")\.?$", re.IGNORECASE)

# Real-world regression: a real Parallel query for a real company's official
# site returned a PDF annual report as the top result, not the homepage --
# verify_discovered_site correctly rejected it (extruct/trafilatura can't
# get an org name from a PDF), but that wastes a verify-fetch on something
# that could never work. Skip these file types before ever returning a
# candidate; try the next result instead.
_NON_WEBPAGE_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".zip", ".mp4", ".mp3",
)

# Aggregators/directories/social platforms a candidate URL should never
# resolve to -- same principle already applied to direct crawling (proff.no
# and purehelp.no are excluded there for robots.txt/bot-blocking reasons;
# see LIMITATIONS.md). A "candidate official site" that's actually one of
# these isn't the company's own site at all.
AGGREGATOR_DOMAIN_BLACKLIST = [
    "proff.no", "purehelp.no", "gulesider.no", "180.no",
    "facebook.com", "linkedin.com", "wikipedia.org",
]


def _looks_like_webpage(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return not path.endswith(_NON_WEBPAGE_EXTENSIONS)


def _first_webpage_url(results: list[dict]) -> Optional[str]:
    for result in results:
        url = result.get("url")
        if url and _looks_like_webpage(url):
            return url
    return None


def _strip_legal_suffix(legal_name: str) -> str:
    return _LEGAL_SUFFIX_RE.sub("", legal_name.strip()).strip()


def _normalize_site(value: str) -> str:
    value = value.strip()
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    return f"https://{value}"


def _post_json_with_retries(
    client: httpx.Client,
    url: str,
    json_body: dict,
    headers: dict,
    sleep: Callable[[float], None],
) -> Optional[dict]:
    """Shared POST-with-retry helper for the Exa/Parallel tiers: retries on
    transport failure or an empty/undecodable body (both observed as
    transient in production -- see the DuckDuckGo tier's own history of the
    same symptoms), gives up cleanly after MAX_FETCH_RETRIES."""
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            response = client.post(url, json=json_body, headers=headers, timeout=10.0)
        except httpx.HTTPError:
            response = None

        if response is not None and 200 <= response.status_code < 300:
            try:
                return response.json()
            except ValueError:
                pass

        if attempt < MAX_FETCH_RETRIES - 1:
            sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return None


def _discover_via_exa(
    legal_name: str,
    client: httpx.Client,
    budget: BudgetGovernor,
    sleep: Callable[[float], None],
    api_key: str,
) -> Optional[str]:
    if not budget.can_spend_request():
        return None
    query = f"{_strip_legal_suffix(legal_name)} official website Norway"
    data = _post_json_with_retries(
        client,
        EXA_URL,
        {"query": query, "numResults": 5, "type": "auto"},
        {"x-api-key": api_key, "Content-Type": "application/json"},
        sleep,
    )
    budget.record_request()
    if data is None:
        return None
    return _first_webpage_url(data.get("results") or [])


def _discover_via_parallel(
    legal_name: str,
    client: httpx.Client,
    budget: BudgetGovernor,
    sleep: Callable[[float], None],
    api_key: str,
) -> Optional[str]:
    if not budget.can_spend_request():
        return None
    query = f"{_strip_legal_suffix(legal_name)} official website Norway"
    data = _post_json_with_retries(
        client,
        PARALLEL_URL,
        {
            "objective": f"Find the official website of the Norwegian company '{legal_name}'",
            "search_queries": [query],
            "mode": "fast",
            "advanced_settings": {
                "max_results": 5,
                # Real-world regression: exclude_domains does NOT live directly
                # under advanced_settings (a real API call 422s there with
                # "Extra inputs are not permitted") -- it's nested one level
                # deeper, under source_policy. Confirmed against the live API,
                # not just the docs.
                "source_policy": {"exclude_domains": list(AGGREGATOR_DOMAIN_BLACKLIST)},
            },
        },
        {"x-api-key": api_key, "Content-Type": "application/json"},
        sleep,
    )
    budget.record_request()
    if data is None:
        return None
    return _first_webpage_url(data.get("results") or [])


def _discover_via_duckduckgo(
    legal_name: str,
    client: httpx.Client,
    budget: BudgetGovernor,
    sleep: Callable[[float], None],
) -> Optional[str]:
    if not budget.can_spend_request():
        return None

    query = _strip_legal_suffix(legal_name)
    data = None
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            response = client.get(
                DUCKDUCKGO_URL,
                params={"q": query, "format": "json"},
                headers={"Accept": "application/json"},
                timeout=10.0,
            )
        except httpx.HTTPError:
            response = None

        # Real-world regression: DuckDuckGo sometimes returns 202 Accepted
        # (not 200) with a perfectly valid body, and separately, sometimes a
        # 200 with a completely empty body for a query that works moments
        # later -- both are worth a retry before concluding there's no
        # result, not an immediate give-up.
        if response is not None and 200 <= response.status_code < 300:
            try:
                data = response.json()
                break
            except ValueError:
                data = None

        if attempt < MAX_FETCH_RETRIES - 1:
            sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    budget.record_request()

    if data is None:
        return None

    infobox = data.get("Infobox") or {}
    for item in infobox.get("content") or []:
        if item.get("label") == "Website" and item.get("value"):
            match = _WEBSITE_VALUE_RE.search(item["value"])
            if match:
                return _normalize_site(match.group(1))
    return None


def discover_candidate_site(
    legal_name: str,
    client: httpx.Client,
    budget: BudgetGovernor,
    sleep: Callable[[float], None] = time.sleep,
    exa_api_key: Optional[str] = None,
    parallel_api_key: Optional[str] = None,
) -> Optional[str]:
    """One best-effort candidate URL for `legal_name`, tried across the
    provider chain in module-docstring order, or None. Never itself treated
    as evidence -- see module docstring. `exa_api_key`/`parallel_api_key`
    default to EXA_API_KEY/PARALLEL_API_KEY from the environment; pass an
    explicit value (including None) to override for testing."""
    exa_api_key = exa_api_key if exa_api_key is not None else os.environ.get("EXA_API_KEY")
    parallel_api_key = parallel_api_key if parallel_api_key is not None else os.environ.get("PARALLEL_API_KEY")

    if exa_api_key:
        candidate = _discover_via_exa(legal_name, client, budget, sleep, exa_api_key)
        if candidate:
            return candidate

    if parallel_api_key:
        candidate = _discover_via_parallel(legal_name, client, budget, sleep, parallel_api_key)
        if candidate:
            return candidate

    return _discover_via_duckduckgo(legal_name, client, budget, sleep)


def verify_discovered_site(
    url: str,
    legal_name: str,
    client: httpx.Client,
    budget: BudgetGovernor,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Optional[ConfirmedFact]:
    """Independently fetch `url` and confirm it's actually `legal_name`'s
    site (same NAME_MATCH_THRESHOLD as verify.py) before ever treating the
    discovery hit as real. None on any failure or name mismatch."""
    if not budget.can_spend_request():
        return None

    response = None
    for attempt in range(MAX_FETCH_RETRIES):
        try:
            response = client.get(url, timeout=10.0, follow_redirects=True)
            break
        # Real-world regression (1,000-company batch with live Exa/Parallel
        # discovery): a discovered candidate can redirect in a loop.
        # httpx.TooManyRedirects is an httpx.HTTPError (which
        # httpx.TransportError is ALSO a subclass of -- catching the
        # broader HTTPError here covers both in one clause) -- the same
        # exception-type gap already fixed once for crawl.py's
        # malformed-redirect case, just not carried over here.
        except (httpx.HTTPError, UnicodeError):
            response = None
            if attempt < MAX_FETCH_RETRIES - 1:
                sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    budget.record_request()

    if response is None or response.status_code != 200:
        return None

    retrieved_at = now()
    facts = structured_facts(response.text, url, retrieved_at)
    name_candidates = [f.value for f in facts if f.field_name == "organization_name"]
    if not name_candidates:
        text_facts = text_fallback_facts(response.text, url, retrieved_at)
        name_candidates = [f.value for f in text_facts]

    best_score = max((name_similarity(legal_name, c) for c in name_candidates), default=0.0)
    if best_score < NAME_MATCH_THRESHOLD:
        return None

    return ConfirmedFact(
        field_name="official_site",
        value=url,
        source_url=url,
        match_confidence=float(best_score),
        retrieved_at=retrieved_at,
        extraction_method="discovery",
        source_class="external",
    )
