"""
Hiring signals from NAV's official job-vacancy feed (pam-stilling-feed).

NAV (the Norwegian Labour and Welfare Administration) publishes every job ad
on its national job board as a feed meant for outside developers, with a
public access token published at /api/publicToken. Each full ad carries the
employer's organisation number, so a hiring signal is attributed by exact org
number -- no name-guessing and no page to misattribute. Before this,
hiring_signal was available for ~0% of companies: it only ever came from a
JSON-LD JobPosting on a company's own site or a linked ATS.

Two steps, sized to the request budget:

  1. `build_nav_job_index` -- once per run, reads the feed's list pages for ads
     modified in the last NAV_WINDOW_DAYS and indexes the still-ACTIVE ones by
     normalised employer name. List items carry the employer NAME only, not the
     org number.
  2. `nav_hiring_signal_facts` -- per company, fetches the full ad only for
     ads whose employer name exactly matches the company, and publishes one
     only when the ad's own employer org number equals the company's.

Sizing, measured against the live feed: covering every active ad needs a
~60-day window (~104 list pages); ads modified in the last 14 days are ~70%
of active ads (~31 pages), 30 days ~93% (~64 pages). 14 days with a hard cap
of MAX_NAV_FEED_PAGES keeps this to a few percent of a 2,000-request batch.
The feed is ordered oldest-first, so hitting the cap cuts off the NEWEST ads;
that is logged and reported rather than hidden.

Privacy: ads include contact people's names, emails and phone numbers. Only
the job title and its dates are published.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Callable, Optional

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import ConfirmedFact

logger = logging.getLogger(__name__)

NAV_FEED_BASE = "https://pam-stilling-feed.nav.no"
PUBLIC_TOKEN_URL = NAV_FEED_BASE + "/api/publicToken"
FEED_PATH = "/api/v1/feed"
PUBLIC_AD_URL = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"

NAV_WINDOW_DAYS = 14
MAX_NAV_FEED_PAGES = 40
# A large employer can have dozens of open ads; a handful is enough to show
# the company is hiring, and each one costs a request to confirm.
MAX_ADS_PER_COMPANY = 5

_TOKEN_RE = re.compile(r"eyJ[\w-]+\.[\w-]+\.[\w-]+")


def normalize_employer_name(name: str) -> str:
    return " ".join(str(name).upper().split())


@dataclass
class NavJobIndex:
    token: Optional[str] = None
    ads_by_employer: dict[str, list[dict]] = field(default_factory=dict)
    pages_fetched: int = 0
    requests_used: int = 0
    truncated: bool = False

    @property
    def active_ads(self) -> int:
        return sum(len(ads) for ads in self.ads_by_employer.values())

    def candidates_for(self, legal_name: Optional[str]) -> list[dict]:
        if not legal_name:
            return []
        return self.ads_by_employer.get(normalize_employer_name(legal_name), [])

    def report(self) -> dict:
        return {
            "pages_fetched": self.pages_fetched,
            "requests_used": self.requests_used,
            "active_ads_indexed": self.active_ads,
            "truncated_at_page_cap": self.truncated,
        }


def _get(client: httpx.Client, url: str, headers: dict, sleep: Callable[[float], None]) -> Optional[httpx.Response]:
    for attempt in range(2):
        try:
            response = client.get(url, headers=headers, timeout=30.0)
        except httpx.HTTPError:
            response = None
        if response is not None and response.status_code < 500:
            return response
        if attempt == 0:
            sleep(0.5)
    return None


def build_nav_job_index(
    client: httpx.Client,
    budget: BudgetGovernor,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    window_days: int = NAV_WINDOW_DAYS,
    max_pages: int = MAX_NAV_FEED_PAGES,
    sleep: Callable[[float], None] = time.sleep,
) -> NavJobIndex:
    """Index still-active NAV job ads by normalised employer name.

    Built once per run and shared by every company and chunk. Never raises:
    any failure just ends the index where it got to (an empty index means no
    NAV hiring signals, nothing worse)."""
    index = NavJobIndex()

    if not budget.can_spend_request():
        return index
    token_response = _get(client, PUBLIC_TOKEN_URL, {}, sleep)
    budget.record_request()
    index.requests_used += 1
    if token_response is None or token_response.status_code != 200:
        logger.warning("NAV job feed: could not fetch the public token; no NAV hiring signals this run")
        return index
    match = _TOKEN_RE.search(token_response.text)
    if not match:
        logger.warning("NAV job feed: public token not found in response; no NAV hiring signals this run")
        return index
    index.token = match.group(0)

    auth = {"Authorization": f"Bearer {index.token}", "Accept": "application/json"}
    since = format_datetime(now() - timedelta(days=window_days), usegmt=True)
    path: Optional[str] = FEED_PATH
    headers = {**auth, "If-Modified-Since": since}
    active: dict[str, dict] = {}

    while path:
        if index.pages_fetched >= max_pages:
            index.truncated = True
            logger.warning(
                "NAV job feed: stopped at the %d-page cap before reaching the newest ads; "
                "the most recently posted ads are missing from this run",
                max_pages,
            )
            break
        if not budget.can_spend_request():
            index.truncated = True
            break
        response = _get(client, NAV_FEED_BASE + path, headers, sleep)
        budget.record_request()
        index.requests_used += 1
        headers = auth
        if response is None or response.status_code != 200:
            break
        try:
            page = response.json()
        except ValueError:
            break
        items = page.get("items") or []
        if not items:
            break
        index.pages_fetched += 1
        for item in items:
            entry = item.get("_feed_entry") or {}
            uuid = item.get("id")
            if not uuid:
                continue
            if entry.get("status") == "ACTIVE" and entry.get("businessName") and item.get("url"):
                active[uuid] = {
                    "uuid": uuid,
                    "url": item["url"],
                    "employer": normalize_employer_name(entry["businessName"]),
                }
            else:
                active.pop(uuid, None)
        path = page.get("next_url")

    for ad in active.values():
        index.ads_by_employer.setdefault(ad["employer"], []).append(ad)
    return index


def _day(value: object) -> Optional[str]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    return value[:10]


def nav_hiring_signal_facts(
    entity: ResolvedEntity,
    index: Optional[NavJobIndex],
    client: httpx.Client,
    budget: BudgetGovernor,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep,
    max_ads: int = MAX_ADS_PER_COMPANY,
    subunit_org_numbers: Optional[set[str]] = None,
) -> list[ConfirmedFact]:
    """hiring_signal facts for `entity` from ads whose employer name matched.

    Each candidate costs one request to fetch the full ad, so this backs off
    once the batch budget is nearly spent (budget.should_degrade()) -- the
    remaining requests belong to core registry data. An ad is published only
    when its employer org number is the entity's own or one of the entity's
    registered sub-units, and only if it hasn't already expired.

    Real-world regression: NAV ads usually carry the org number of the
    WORKPLACE (the registered sub-unit / underenhet) rather than the parent
    company. Confirmed live: both active ads for INSIDER FACILITY SOLUTIONS
    AS (834327082) used 917784078 and 994172603, each a registered sub-unit
    of that company, so an exact parent-number match rejected every valid
    ad. Sub-unit numbers come from the registry itself, so accepting them
    keeps attribution exact."""
    if index is None or not index.token or entity.resolution_state != EvidenceState.AVAILABLE:
        return []

    auth = {"Authorization": f"Bearer {index.token}", "Accept": "application/json"}
    today = now().date().isoformat()
    own_org_numbers = {entity.org_number} | set(subunit_org_numbers or ())
    facts: list[ConfirmedFact] = []

    for ad in index.candidates_for(entity.legal_name)[:max_ads]:
        if budget.should_degrade() or not budget.can_spend_request():
            break
        response = _get(client, NAV_FEED_BASE + ad["url"], auth, sleep)
        budget.record_request()
        if response is None or response.status_code != 200:
            continue
        try:
            content = (response.json() or {}).get("ad_content") or {}
        except ValueError:
            continue

        employer = content.get("employer") or {}
        if str(employer.get("orgnr") or "") not in own_org_numbers:
            continue
        title = content.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        expires = _day(content.get("expires"))
        if expires is not None and expires < today:
            continue

        published = _day(content.get("published"))
        details = ", ".join(
            part for part in (f"published {published}" if published else None,
                              f"apply by {expires}" if expires else None) if part
        )
        value = f"{title.strip()} ({details}) - NAV" if details else f"{title.strip()} - NAV"
        facts.append(
            ConfirmedFact(
                "hiring_signal",
                value,
                PUBLIC_AD_URL.format(uuid=ad["uuid"]),
                100.0,
                now(),
                content_hash=hashlib.sha256(response.content).hexdigest(),
                extraction_method="structured",
                source_class="external",
            )
        )
    return facts
