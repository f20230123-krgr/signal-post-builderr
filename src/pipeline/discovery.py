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

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from src.orchestrator.budget import BudgetGovernor
from src.storage.cache import ResponseCache
from src.pipeline.extract import structured_facts, text_fallback_facts
from src.pipeline.verify import NAME_MATCH_THRESHOLD, ConfirmedFact, name_similarity

logger = logging.getLogger(__name__)

DUCKDUCKGO_URL = "https://api.duckduckgo.com/"
EXA_URL = "https://api.exa.ai/search"
PARALLEL_URL = "https://api.parallel.ai/v1/search"

# Price of one search call, recorded against the batch budget's $10 limit
# and any run-wide spend cap. Exa: $7 per 1,000 /search requests (up to 10
# results; we request 5), from exa.ai/pricing, 2026-09. Parallel is used on
# its free tier. Without this the spend limit had nothing to enforce: every
# search was recorded as costing $0.
PROVIDER_COST_PER_SEARCH_USD = {"exa": 0.007, "parallel": 0.0}
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
    # Found running a genuinely unseen 100-company batch (companies with no
    # website on file in the universe -- the hardest discovery case): a
    # B2B contact/lead-gen company-database page, confirmed live to be
    # about the right company but not its own site (see
    # test_verify_discovered_site_rejects_prospeo_lead_gen_aggregator).
    "prospeo.io",
    # Found running a second, general (not website-filtered) unseen
    # 100-company batch: northdata.com is a European company-research
    # aggregator ("North Data Smart Research"); merinfo.no is a Norwegian
    # business-lookup directory (title pattern "<company> - <org number> -
    # <city> - Regnskap, roller og mer") -- both confirmed live, same
    # pattern as the other directory sites above.
    "northdata.com", "merinfo.no",
    # Found re-running that same batch (live discovery results are not
    # deterministic run-to-run -- a later run surfaced 14 more real
    # aggregator/lead-gen hits the first run happened not to return), every
    # one confirmed live before adding:
    #   - byndle.no: "Byndle Opplysning" -- Norway's online phone directory
    #     and business lookup ("Norges telefonkatalog... gratis
    #     nummeropplysning og bedriftssøk"), same category as 1881.no.
    #   - nyeselskaper.no / allebedrifter.no: company directories, both
    #     citing "Totalt 1 174 552" (1.17M) Norwegian companies indexed.
    #   - 1850.no: "1850 NUMMEROPPLYSNINGEN AS" -- phone/business directory,
    #     same category as 1881.no/byndle.no.
    #   - flowfirma.no: directory of ~390,000 Norwegian companies, sourced
    #     live from Brønnøysundregistrene.
    #   - haandverkerportalen.no: "Norges håndverkerregister", 34,100+
    #     businesses across 357 municipalities.
    #   - mittanbud.no: "Mittanbud Marketplaces AS" -- a multi-vendor
    #     marketplace (10,000+ verified businesses), not any one company's
    #     own site.
    #   - eiendomssjekk.no: property/company data aggregator pulling from
    #     five public sources (Kartverket, SSB, Enova, Brønnøysund,
    #     Boligannonser).
    #   - fagfolkguiden.no: craftsman/professional directory, 212+
    #     advertisers across 317+ areas.
    #   - firmview.no: "Nordic Company Financial Data" aggregator, same
    #     category as northdata.com.
    #   - vexter.no / agama.no: real B2B sales-intelligence SaaS companies
    #     (own legitimate marketing sites), but the discovered URLs were
    #     their PRODUCT'S auto-generated profile pages for OTHER companies
    #     (e.g. vexter.no/selskap/<other-company>/<org-number>) built from
    #     their own scraped database of ~1.1M Norwegian companies -- same
    #     failure shape as prospeo.io, just a Norwegian-market platform.
    #   - elinnweb.no / elinn.no: "Elinn" -- an industry software platform
    #     for electrical contractors ("Bransjesystem for elektrikere");
    #     the discovered URL was a numbered customer-profile subpage
    #     (elinnweb.no/1437/about), not the contractor's own domain -- same
    #     SaaS-profile-page shape as vexter.no/agama.no.
    #   - thehub.io: well-known international startup/investor directory.
    #   - stipendportalen.no: a third-party scholarship-listing portal
    #     (returned for a foundation) -- domain bears no relation to the
    #     foundation's own name, consistent with every other aggregator
    #     here; live fetch was blocked (403) so this one rests on strong
    #     circumstantial signal rather than confirmed page content, same
    #     caveat proff.no already carries.
    "byndle.no", "nyeselskaper.no", "allebedrifter.no", "1850.no",
    "flowfirma.no", "haandverkerportalen.no", "mittanbud.no",
    "eiendomssjekk.no", "fagfolkguiden.no", "firmview.no",
    "vexter.no", "agama.no", "elinnweb.no", "elinn.no",
    "thehub.io", "stipendportalen.no",
    # Norway's Financial Supervisory Authority registry-detail pages -- a
    # government register lookup, same class as virksomhet.brreg.no and
    # sgregister.dibk.no above. Confirmed live: the page returned as one
    # company's "official website" was a registry entry for an entirely
    # different company ("ERGO FORSIKRING A/S NUF").
    "finanstilsynet.no",
    # Found in the fourth re-run of the same batch (post the generic
    # directory-path fix): arbeidsplassen.nav.no is NAV's (Norway's
    # official labor/welfare directorate) public job-listing portal --
    # confirmed live, the discovered URL was a specific job POSTING page
    # for a restaurant, not the restaurant's own site. A government job
    # board, same failure shape as an ATS platform reached without an
    # actual link from the official site.
    "arbeidsplassen.nav.no",
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


# Norwegian law requires a registered business to display its own org
# number on its site (typically as "Org.nr: 999 999 999" or "Organisasjons-
# nummer: 999999999", often near a footer/contact block). Real-world
# regression, found running a general unseen 100-company batch: Exa
# returned https://adnor.no ("Adnor Advokat AS", org 996399869) for
# "ADVOKAT VIBEKE MELAND" (a different, separately registered legal
# entity, org 989750291) -- confirmed live via Brreg. The name-similarity
# gate can't catch this (a law firm's site plausibly mentions an associated
# lawyer's name), and the URL itself carries no org number to catch via
# _url_embeds_a_different_org_number -- but the page's own legally-required
# disclosure does. Deliberately anchored to the labeled disclosure phrasing
# (not a bare 9-digit scan of the whole page) to avoid false-rejecting on
# an unrelated 9-digit sequence (phone/invoice/reference numbers).
_LABELED_ORG_NUMBER_RE = re.compile(
    r"org(?:anisasjons)?\.?\s*nr\.?\s*:?\s*(\d[\d\s]{9,12}\d)", re.IGNORECASE
)


# A real company page declares itself, plus occasionally a parent company or
# a publisher -- a handful at most. A page declaring many distinct
# organizations is listing other businesses (a mall, a marketplace, an
# industry portal). Set deliberately above the legitimate parent/subsidiary
# case so a normal corporate page is never caught.
MAX_DISTINCT_ORGANIZATIONS_ON_AN_OWN_SITE = 4


def _page_lists_many_organizations(facts: list) -> bool:
    names = {f.value.strip().lower() for f in facts if f.field_name == "organization_name" and f.value}
    return len(names) > MAX_DISTINCT_ORGANIZATIONS_ON_AN_OWN_SITE


# Two-letter TLDs that Norwegian companies genuinely use as generic domains
# rather than as a foreign country signal -- .as especially (it reads as the
# Norwegian company suffix; anti.as for ANTICOACH AS is a real confirmed
# case), plus the usual tech/regional generics.
_NON_FOREIGN_TWO_LETTER_TLDS = {"no", "as", "io", "ai", "co", "me", "tv", "cc", "eu", "nu", "to", "gg"}


def _is_foreign_country_tld(url: str) -> bool:
    """True if the URL sits on a foreign country-code domain (.in, .se, .dk,
    .fi, .de, ...).

    Real-world regression this exists for: lasventures.in ("LAS Ventures",
    an Indian firm) was accepted as the site of Norwegian "LAS VENTURES AS"
    -- the names match EXACTLY, so neither name-similarity nor the
    short-candidate exact-match rule can catch it, and the foreign domain is
    the only remaining signal.

    Deliberately NOT a blanket "reject anything that isn't .no": 13.5% of
    real registered Norwegian company websites use .com/.net/.org/.io and
    friends. Measured cost of this narrower rule is ~0.3% of registered
    sites, it applies only to search-discovered candidates (a
    registry-provided site never reaches this function), and org-number
    proof waives it entirely."""
    from urllib.parse import urlsplit

    host = urlsplit(url).netloc.lower().split(":", 1)[0]
    tld = host.rsplit(".", 1)[-1] if "." in host else ""
    return len(tld) == 2 and tld not in _NON_FOREIGN_TWO_LETTER_TLDS


def _page_confirms_own_org_number(text: str, org_number: Optional[str]) -> bool:
    """True if the page publishes THIS entity's own 9-digit org number.

    The strongest identity confirmation available, and the one signal that
    doesn't care what the company calls itself: Norwegian businesses are
    legally required to publish their org number on their own site, while
    small companies very often trade under a brand name with no textual
    relationship to the registered legal name ("Snekkersentralen" for
    "HANDVERKER JAN-ERIK MOXNESS, HJEM AS"). Name-similarity alone rejects
    every one of those real sites; this accepts them on hard evidence.

    Matched with or without the conventional "123 456 789" spacing."""
    if not org_number:
        return False
    flattened = re.sub(r"<[^>]+>", " ", text)
    digits_only = re.sub(r"[\s ]", "", flattened)
    return org_number in digits_only


def _page_text_embeds_a_different_org_number(text: str, org_number: Optional[str]) -> bool:
    """Real-world regression, found re-running the exact adnor.no case this
    check was built for: the live page renders the label and number as
    "<strong>Org.nr:</strong> 996 399 869" -- the closing tag sits between
    "nr" and the digits, which a whitespace-only regex (\\s*) never
    matches, so the check silently missed it on real HTML even though a
    synthetic hand-written test (no markup in between) passed. Flattening
    every tag to a space first -- same fix shape as never trusting a
    JSON-LD field to already be plain text -- makes this robust to
    wherever the markup happens to land."""
    flattened = re.sub(r"<[^>]+>", " ", text)
    if not org_number:
        return False
    for match in _LABELED_ORG_NUMBER_RE.finditer(flattened):
        candidate = re.sub(r"\s", "", match.group(1))
        if len(candidate) == 9 and candidate != org_number:
            return True
    return False


# Domain-agnostic defense-in-depth: every confirmed aggregator/directory
# found across three separate unseen 100-company batches this session --
# whether or not it was already in AGGREGATOR_DOMAIN_BLACKLIST -- shares
# one structural trait: a generic "listing/profile" word as its OWN URL
# path segment (nyeselskaper.no/companies/..., 1850.no/bedrifter/...,
# vexter.no/selskap/..., firmview.no/company/..., agama.no/bedrift/...,
# ipqwery.com/.../owner/profile/..., pappers.no/company/...,
# mesterregister.mesterbrev.no/mestere/...). A real company's own homepage
# essentially never structures its own URL this way (contrast: a genuine
# "/about", "/om-oss", or a housing co-op's own slug on its manager's
# platform, none of which match). This catches the *next* aggregator not
# yet individually identified, instead of only the ones already blacklisted
# by domain -- a domain list alone has proven to be a permanently moving
# target (a fresh unseen batch has surfaced new ones every single time).
_DIRECTORY_PATH_SEGMENTS = {
    "company", "companies", "bedrift", "bedrifter", "selskap", "selskaper",
    "firma", "foretak", "virksomhet", "enhet", "enheter", "mester", "mestere",
    "owner", "profile", "directory", "search", "sok", "katalog",
}


def _url_path_looks_like_a_directory_listing(url: str) -> bool:
    from urllib.parse import urlsplit

    segments = [s for s in urlsplit(url).path.lower().split("/") if s]
    return any(segment in _DIRECTORY_PATH_SEGMENTS for segment in segments)


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


class ProviderHealth:
    """Which discovery providers have permanently failed during this run.

    Not retrying an individual 402 isn't enough on its own: the chain still
    made one doomed call per tier, per company. Across a 100-company batch
    with three discovery tiers that is ~270 guaranteed-useless calls, each
    burning a unit of request budget and real wall-clock, and starving the
    tiers that still work. Once a provider answers with a permanent failure
    (exhausted credits, revoked key), it is out for the rest of the run.

    Scoped to one run and passed in explicitly rather than held as module
    state, so it can't leak between runs or between tests.

    Also tracks search spend across the WHOLE run (every chunk), and enforces
    `spend_cap_usd` if given: the per-batch $10 limit in BudgetGovernor resets
    every 100 companies, so on its own it can't stop a 1,000-company run from
    spending far more than an account holds. When the next paid search would
    cross the cap, that provider is disabled like any other permanent failure
    and the run continues on the remaining tiers. Spend is reserved under a
    lock BEFORE the call, so concurrent companies can never overshoot the cap."""

    def __init__(self, spend_cap_usd: Optional[float] = None) -> None:
        self._disabled: dict[str, str] = {}
        self._spend_cap_usd = spend_cap_usd
        self._spent_usd = 0.0
        self._lock = threading.Lock()

    def reserve_spend(self, provider: str, cost_usd: float) -> bool:
        """Reserve `cost_usd` for one paid search. False (and the provider is
        disabled for the rest of the run) if it would cross the run's cap."""
        if cost_usd <= 0:
            return True
        with self._lock:
            if self._spend_cap_usd is not None and self._spent_usd + cost_usd > self._spend_cap_usd + 1e-9:
                over_cap = True
            else:
                over_cap = False
                self._spent_usd += cost_usd
        if over_cap:
            self.disable(provider, f"run spend cap of ${self._spend_cap_usd:.2f} reached")
            return False
        return True

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return round(self._spent_usd, 4)

    def is_available(self, provider: str) -> bool:
        return provider not in self._disabled

    def disable(self, provider: str, reason: str) -> None:
        if provider not in self._disabled:
            logger.warning(
                "discovery provider %r disabled for the rest of this run: %s. "
                "Remaining tiers will be used instead -- coverage for companies "
                "with no registered website will be reduced until this is resolved.",
                provider,
                reason,
            )
            self._disabled[provider] = reason

    @property
    def disabled(self) -> dict[str, str]:
        """provider -> reason, for run reporting."""
        return dict(self._disabled)


def _post_json_with_retries(
    client: httpx.Client,
    url: str,
    json_body: dict,
    headers: dict,
    sleep: Callable[[float], None],
) -> tuple[Optional[dict], Optional[int]]:
    """Returns (data, permanent_failure_status).

    The status is returned, not just swallowed, so the caller can disable a
    provider that has permanently failed (exhausted credits / revoked key)
    instead of rediscovering the same 402 on every subsequent company.

    Shared POST-with-retry helper for the Exa/Parallel tiers: retries on
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
                return response.json(), None
            except ValueError:
                pass

        if response is not None and response.status_code in _PERMANENT_FAILURE_STATUSES:
            return None, response.status_code

        if attempt < MAX_FETCH_RETRIES - 1:
            sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return None, None


def _search_cache_key(url: str, body: dict) -> str:
    """Cache key for a search call: provider endpoint + exact request body.
    Deliberately never includes the API key -- the cache is a plain SQLite
    file on disk, and a secret must not end up in it."""
    return f"search::{url}::{json.dumps(body, sort_keys=True)}"


def _search(
    provider: str,
    url: str,
    body: dict,
    api_key: str,
    client: httpx.Client,
    budget: BudgetGovernor,
    sleep: Callable[[float], None],
    provider_health: Optional[ProviderHealth],
    cache: Optional[ResponseCache],
    date_bucket: Optional[str],
) -> Optional[dict]:
    """One search call, shared by the Exa and Parallel tiers.

    Order matters: a same-day cached response is served first -- it's free
    against the budget (same "cache hits are free" rule as page fetches) and
    spends no credits. Search responses used to be the one thing never
    cached, so every local re-run re-bought the same results; that's a large
    part of how the Exa credits ran out twice. Only successful responses are
    cached, so a failure is always retried on the next run.

    Then: skip a provider already disabled this run, respect the request
    budget and the batch's $ limit, reserve the spend against the run-wide cap,
    make the call, and disable the provider on a permanent failure."""
    bucket = date_bucket or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = _search_cache_key(url, body)
    if cache is not None:
        cached = cache.get(key, bucket)
        if cached is not None:
            try:
                return json.loads(cached)
            except ValueError:
                pass

    if provider_health is not None and not provider_health.is_available(provider):
        return None
    if not budget.can_spend_request():
        return None
    cost = PROVIDER_COST_PER_SEARCH_USD.get(provider, 0.0)
    if cost and not budget.can_spend_usd(cost):
        return None
    if provider_health is not None and not provider_health.reserve_spend(provider, cost):
        return None

    data, permanent_failure = _post_json_with_retries(
        client, url, body, {"x-api-key": api_key, "Content-Type": "application/json"}, sleep
    )
    budget.record_request()
    budget.record_spend(cost)
    if permanent_failure is not None and provider_health is not None:
        provider_health.disable(provider, f"HTTP {permanent_failure} (exhausted credits or invalid key)")
    if data is not None and cache is not None:
        cache.put(key, bucket, json.dumps(data))
    return data


def _discover_via_exa(
    legal_name: str,
    client: httpx.Client,
    budget: BudgetGovernor,
    sleep: Callable[[float], None],
    api_key: str,
    provider_health: Optional[ProviderHealth] = None,
    cache: Optional[ResponseCache] = None,
    date_bucket: Optional[str] = None,
) -> Optional[str]:
    query = f"{_strip_legal_suffix(legal_name)} official website Norway"
    data = _search(
        "exa", EXA_URL, {"query": query, "numResults": 5, "type": "auto"},
        api_key, client, budget, sleep, provider_health, cache, date_bucket,
    )
    if data is None:
        return None
    return _first_webpage_url(data.get("results") or [])


def _discover_via_parallel(
    legal_name: str,
    client: httpx.Client,
    budget: BudgetGovernor,
    sleep: Callable[[float], None],
    api_key: str,
    provider_health: Optional[ProviderHealth] = None,
    cache: Optional[ResponseCache] = None,
    date_bucket: Optional[str] = None,
) -> Optional[str]:
    query = f"{_strip_legal_suffix(legal_name)} official website Norway"
    body = {
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
    }
    data = _search("parallel", PARALLEL_URL, body, api_key, client, budget, sleep, provider_health, cache, date_bucket)
    if data is None:
        return None
    return _first_webpage_url(data.get("results") or [])


def check_provider_keys(
    client: httpx.Client,
    exa_api_key: Optional[str],
    parallel_api_key: Optional[str],
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, str], int]:
    """Send one tiny search per configured key before a run starts.

    Returns (dead_providers, requests_used), where dead_providers maps a
    provider to the reason it can't be used for this run. Only a permanent
    failure (401/402/403: exhausted credits, revoked or wrong key) counts as
    dead -- a 5xx or network blip at startup says nothing about the key, and
    treating it as dead would stop a run or skip a working provider for no
    reason. Unconfigured providers are skipped with no request."""
    probes = [
        (
            "exa",
            exa_api_key,
            EXA_URL,
            lambda key: ({"query": "Equinor official website Norway", "numResults": 1, "type": "auto"},
                         {"x-api-key": key, "Content-Type": "application/json"}),
        ),
        (
            "parallel",
            parallel_api_key,
            PARALLEL_URL,
            lambda key: ({"objective": "Find the official website of the Norwegian company 'Equinor ASA'",
                          "search_queries": ["Equinor official website Norway"],
                          "mode": "fast",
                          "advanced_settings": {"max_results": 1}},
                         {"x-api-key": key, "Content-Type": "application/json"}),
        ),
    ]

    dead: dict[str, str] = {}
    requests_used = 0
    for provider, key, url, build in probes:
        if not key:
            continue
        body, headers = build(key)
        _, permanent_failure = _post_json_with_retries(client, url, body, headers, sleep)
        requests_used += 1
        if permanent_failure is not None:
            dead[provider] = f"HTTP {permanent_failure} (exhausted credits or invalid key)"
    return dead, requests_used


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
    provider_health: Optional[ProviderHealth] = None,
    cache: Optional[ResponseCache] = None,
    date_bucket: Optional[str] = None,
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
        candidate = _discover_via_exa(legal_name, client, budget, sleep, exa_api_key, provider_health, cache, date_bucket)
        if candidate:
            return candidate

    if parallel_api_key:
        candidate = _discover_via_parallel(
            legal_name, client, budget, sleep, parallel_api_key, provider_health, cache, date_bucket
        )
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
    alternate_names: Optional[list[str]] = None,
) -> Optional[ConfirmedFact]:
    """`alternate_names` are other names this same entity is registered
    under (its own former names, from the live registry record): a company
    renamed recently often still runs its site under the old name, so the
    page is matched against those too. The caller is responsible for
    confirming no other entity currently holds such a name (see
    registry_extras.name_is_held_by_another_entity) before passing it.

    Independently fetch `url` and confirm it's actually `legal_name`'s
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
    if _url_path_looks_like_a_directory_listing(url):
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
        except httpx.TooManyRedirects:
            response = None  # a loop is deterministic: never retried
            break
        except (httpx.HTTPError, UnicodeError):
            response = None
            if attempt < MAX_FETCH_RETRIES - 1:
                sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    budget.record_request()

    if response is None or response.status_code != 200:
        return None
    if _page_text_embeds_a_different_org_number(response.text, org_number):
        return None

    retrieved_at = now()
    facts = structured_facts(response.text, url, retrieved_at)
    name_candidates = [f.value for f in facts if f.field_name == "organization_name"]
    if not name_candidates:
        text_facts = text_fallback_facts(response.text, url, retrieved_at)
        name_candidates = [f.value for f in text_facts]

    # A page declaring many distinct organizations is a venue, marketplace or
    # portal listing OTHER businesses -- not any one of them's own site. Real
    # confirmed false positive this generalizes: lillemarkens.no, a shopping
    # mall's own site (30 tenant shops), accepted as the official website of
    # one tenant. Waived when the page proves this entity's own org number,
    # which is stronger evidence than the shape of the page.
    org_number_confirmed = _page_confirms_own_org_number(response.text, org_number)

    if not org_number_confirmed and _page_lists_many_organizations(facts):
        return None

    # A foreign country-code domain, with nothing on the page proving this
    # Norwegian entity owns it, is a cross-border name collision far more
    # often than a genuine foreign-hosted Norwegian company.
    if not org_number_confirmed and _is_foreign_country_tld(url):
        return None

    # Positive org-number confirmation short-circuits name matching entirely:
    # it's hard evidence of ownership, where a name match is only ever an
    # inference. Without this, every real company trading under a brand name
    # unrelated to its registered legal name is rejected.
    confirmed_by_org_number = org_number_confirmed

    identity_names = [legal_name] + [n for n in (alternate_names or []) if n]
    best_score = max(
        (name_similarity(name, c) for name in identity_names for c in name_candidates), default=0.0
    )
    if not confirmed_by_org_number and best_score < NAME_MATCH_THRESHOLD:
        return None
    if confirmed_by_org_number:
        best_score = 100.0

    return ConfirmedFact(
        field_name="official_site",
        value=url,
        source_url=url,
        match_confidence=float(best_score),
        retrieved_at=retrieved_at,
        extraction_method="discovery",
        source_class="external",
    )
