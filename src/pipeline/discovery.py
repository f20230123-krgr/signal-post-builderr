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
#
# Real-world regression, found auditing a real 1,000-company batch: every
# domain below appeared in the "official_website" field of multiple wholly
# unrelated companies (a business directory/accounts-data aggregator lists
# many companies and echoes each one's exact legal name verbatim, which
# trivially clears verify_discovered_site's name-match threshold) -- ~40% of
# all discovered sites in that batch landed on one of these instead of the
# company's actual site. `virksomhet.brreg.no` and `sgregister.dibk.no` are
# government registry/approval-lookup pages (same failure mode: a page ABOUT
# the company, not the company's own site). Deliberately NOT included:
# large Norwegian companies/federations (obos.no, usbl.no, vibbo.no,
# storebrand.no) that can legitimately be the registered site of many
# entities they manage -- that case is handled by verify()'s `context_name`
# check, not a blacklist, since blacklisting them would incorrectly reject
# genuine claims.
AGGREGATOR_DOMAIN_BLACKLIST = [
    "proff.no", "purehelp.no", "gulesider.no", "180.no",
    "facebook.com", "linkedin.com", "wikipedia.org",
    "rosa.no", "regnskapstall.no", "regnskapsbasen.no", "firmadatabasen.no",
    "opencompanydatabase.com", "biztrac.no", "io.no", "21st.ai",
    "virksomhet.brreg.no", "sgregister.dibk.no", "generate.no", "1881.no",
]


_ORG_NUMBER_RE = re.compile(r"(?<!\d)(\d{9})(?!\d)")


def _url_embeds_a_different_org_number(url: str, org_number: Optional[str]) -> bool:
    """True if `url` contains a 9-digit (Norwegian org-number-shaped)
    sequence that does NOT match `org_number`. Independent review finding,
    confirmed against a real 1,000-company batch: a candidate that fuzzy-
    matches the target company's name can still belong to a DIFFERENT
    company -- short/generic Norwegian holding-company names ("GRE HOLDING
    AS" vs "GREVE HOLDING AS") score 92-93 under every RapidFuzz metric
    tested (partial_ratio/token_sort_ratio/token_set_ratio/WRatio), all
    above NAME_MATCH_THRESHOLD. The candidate URL itself embedding the OTHER
    company's real org number is a far more reliable, domain-agnostic
    signal than any name-similarity metric or threshold tweak -- and unlike
    a domain blacklist, it generalizes to directory sites not seen yet. A
    URL with no embedded 9-digit sequence at all (the common case -- most
    homepages are just a bare domain) is unaffected."""
    if not org_number:
        return False
    return any(match != org_number for match in _ORG_NUMBER_RE.findall(url))


def _looks_like_webpage(url: str) -> bool:
    path = url.split("?", 1)[0].split("#", 1)[0].lower()
    return not path.endswith(_NON_WEBPAGE_EXTENSIONS)


def _is_blacklisted_domain(url: str) -> bool:
    """True if `url`'s host is (or is a subdomain of) a blacklisted
    directory/aggregator/government-lookup domain. Suffix-matched, not a
    plain substring check, so e.g. "io.no" doesn't also match "studio.no",
    while "stage.purehelp.no" still correctly matches "purehelp.no"."""
    from urllib.parse import urlsplit

    host = urlsplit(url).netloc.lower().split(":", 1)[0]
    return any(host == domain or host.endswith("." + domain) for domain in AGGREGATOR_DOMAIN_BLACKLIST)


def _first_webpage_url(results: list[dict]) -> Optional[str]:
    for result in results:
        url = result.get("url")
        if url and _looks_like_webpage(url) and not _is_blacklisted_domain(url):
            return url
    return None


def _strip_legal_suffix(legal_name: str) -> str:
    return _LEGAL_SUFFIX_RE.sub("", legal_name.strip()).strip()


def _normalize_site(value: str) -> str:
    value = value.strip()
    if re.match(r"^https?://", value, re.IGNORECASE):
        return value
    return f"https://{value}"


# Real-world regression, confirmed live against the real Exa API mid-
# session (quota exhausted -> 402 on every call): a 401/402/403 is a
# PERMANENT failure for the rest of the billing/auth period, not a
# transient one -- unlike a 5xx or network blip, retrying it can never
# succeed. Retrying it anyway wastes real wall-clock time (backoff sleep)
# and a full unit of request budget per doomed attempt; with three
# discovery tiers each hitting this provider first, that waste multiplied
# 3x per company across a real 1,000-company batch -- a large factor in
# why that run took ~78 minutes instead of the usual ~22, and why fewer
# companies got real coverage (wasted budget that should have reached
# Parallel or crawling instead).
_PERMANENT_FAILURE_STATUSES = {401, 402, 403}


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
    same symptoms), gives up cleanly after MAX_FETCH_RETRIES. Never retries
    a 401/402/403 -- see _PERMANENT_FAILURE_STATUSES."""
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

        if response is not None and response.status_code in _PERMANENT_FAILURE_STATUSES:
            return None

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


_ROLE_TITLE_RE = re.compile(r"\s*\([^)]*\)\s*$")


def strip_leader_role_title(leader_value: str) -> str:
    """"Anders Opedal (Daglig leder)" -> "Anders Opedal" -- registry_extras.py
    formats a leader ConfirmedFact.value as "name (title)"; only the name is
    useful as a search seed for the "leader/founder bridge" (see
    src/orchestrator/runner.py, which owns the retry-with-verification loop
    this feeds -- discover_candidate_site itself stays a single-query
    function; a candidate that's found but then fails independent
    verification must still trigger the leader-name retry, which only the
    caller that also runs verify_discovered_site can know about)."""
    return _ROLE_TITLE_RE.sub("", leader_value).strip()


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
    as evidence -- see module docstring.

    `exa_api_key`/`parallel_api_key` are used exactly as given -- None means
    that provider is skipped, full stop. This function deliberately does NOT
    fall back to os.environ itself: real-world regression, an earlier
    version treated an explicit None the same as "not provided" and read
    EXA_API_KEY/PARALLEL_API_KEY from the environment instead, which is
    invisible in a dev session where a just-`setx`-set var isn't yet
    inherited by the running process, but silently overrides an explicit
    "don't use this provider" once it naturally propagates to a fresh
    session. Reading the environment is the caller's job (see
    src/orchestrator/runner.py) -- same pattern as `client`/`cache` already
    being resolved by the caller, not this function.

    Real-world regression: an earlier version of the "leader/founder bridge"
    lived entirely inside this function, retrying with a leader name only
    when the legal name found NOTHING at all. Measured zero improvement on a
    real, unseen 100-company batch -- because when the legal-name search
    finds SOME candidate that then fails verify_discovered_site's identity
    check (wrong company, blacklisted domain, etc.), this function had
    already returned that candidate, so the leader-name retry never got a
    chance to run at all. The retry now lives in runner.py, which is the
    only place that sees BOTH this function's result AND whether
    verify_discovered_site actually accepted it -- see
    strip_leader_role_title's docstring."""
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
    org_number: Optional[str] = None,
) -> Optional[ConfirmedFact]:
    """Independently fetch `url` and confirm it's actually `legal_name`'s
    site (same NAME_MATCH_THRESHOLD as verify.py) before ever treating the
    discovery hit as real. None on any failure or name mismatch.

    Rejects a blacklisted directory/aggregator/government-lookup domain
    outright, without spending a request -- defense in depth against any
    future call site that bypasses `_first_webpage_url`'s own filtering
    (see AGGREGATOR_DOMAIN_BLACKLIST's docstring for why this matters: those
    pages echo the exact legal name and would otherwise pass the name-match
    check below every time).

    Also rejects a URL embedding a DIFFERENT company's org number, when
    `org_number` (this entity's own) is given -- see
    `_url_embeds_a_different_org_number`'s docstring for the confirmed
    real-world case this closes, independent of name-similarity entirely."""
    if _is_blacklisted_domain(url):
        return None
    if _url_embeds_a_different_org_number(url, org_number):
        return None

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
