"""
Additional official-registry data for an already-resolved entity: leadership
roles, filed annual accounts, and registered sub-units (workplaces).

Contract: docs/component-specs.md -> "src/pipeline/registry_extras.py"

Why this is a separate stage from resolve.py: resolve.py's job is strictly
"org number -> verified entity" (one request, one authoritative record).
These are three *additional*, independently-failing calls against the same
Brønnøysundregisteret host -- roller (roles), regnskapsregisteret (accounts),
and underenheter (sub-units) -- each gated by the budget governor like any
other outbound request.

Why these bypass verify.py: every fact here is fetched by org_number against
the same official government registry that already identified the entity --
there's no page to misattribute, so there's no identity-matching risk the
95%-precision gate needs to guard against. They're returned as already-
confirmed ConfirmedFacts (match_confidence=100.0), not RawFacts needing
fuzzy verification.

Privacy note: the roller endpoint includes each person's date of birth
(fodselsdato) -- technically public Norwegian registry data, but nothing in
docs/data-schema.md calls for it and this profile is a company-research tool,
not a personal-data tool. Deliberately not published.

Scope note: leadership role groups are filtered to DAGL (CEO) and STYR
(board) -- REVI (auditor) and VARA (deputy/alternate board members) are
excluded as not matching data-schema.md's "leaders: e.g. CEO, board chair".
"""
from __future__ import annotations

import hashlib
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from src.orchestrator.budget import BudgetGovernor
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import ConfirmedFact
from src.models.profile import EvidenceState

ROLES_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
SUBUNITS_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}"

MAX_RETRIES = 2
RETRY_BACKOFF_SECONDS = 0.5

INCLUDED_ROLE_GROUPS = {"DAGL", "STYR"}
EXCLUDED_ROLE_TYPES = {"REVI", "VARA"}


def _get(client: httpx.Client, url: str, sleep: Callable[[float], None]) -> Optional[httpx.Response]:
    for attempt in range(MAX_RETRIES):
        try:
            response = client.get(url, timeout=10.0)
            if response.status_code >= 500:
                if attempt < MAX_RETRIES - 1:
                    sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            return response
        except httpx.TransportError:
            if attempt < MAX_RETRIES - 1:
                sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
    return None


def _person_name(person: dict) -> Optional[str]:
    navn = person.get("navn") or {}
    parts = [navn.get("fornavn"), navn.get("mellomnavn"), navn.get("etternavn")]
    full = " ".join(p for p in parts if p)
    return full or None


def _role_holder_name(role: dict) -> Optional[str]:
    if "person" in role:
        return _person_name(role["person"])
    if "enhet" in role:
        names = role["enhet"].get("navn")
        return names[0] if isinstance(names, list) and names else None
    return None


def _content_hash(response: httpx.Response) -> str:
    return hashlib.sha256(response.content).hexdigest()


def _leadership_facts(
    entity: ResolvedEntity, client: httpx.Client, sleep: Callable[[float], None], now: datetime
) -> list[ConfirmedFact]:
    url = ROLES_URL.format(org=entity.org_number)
    response = _get(client, url, sleep)
    if response is None or response.status_code != 200:
        return []

    body = response.json()
    content_hash = _content_hash(response)
    facts = []
    for group in body.get("rollegrupper", []):
        if group.get("type", {}).get("kode") not in INCLUDED_ROLE_GROUPS:
            continue
        for role in group.get("roller", []):
            if role.get("avregistrert"):
                continue
            role_type = role.get("type", {}).get("kode")
            if role_type in EXCLUDED_ROLE_TYPES:
                continue
            if role.get("person", {}).get("erDoed"):
                continue
            name = _role_holder_name(role)
            if not name:
                continue
            title = role.get("type", {}).get("beskrivelse")
            value = f"{name} ({title})" if title else name
            facts.append(
                ConfirmedFact(
                    "leader", value, url, 100.0, now,
                    content_hash=content_hash, extraction_method="registry", source_class="official_registry",
                )
            )
    return facts


def _format_accounts_value(entry: dict) -> Optional[str]:
    currency = entry.get("valuta") or ""
    result = entry.get("resultatregnskapResultat") or {}
    revenue = (result.get("driftsresultat") or {}).get("driftsinntekter", {}).get("sumDriftsinntekter")
    net_result = result.get("aarsresultat")

    parts = []
    if revenue is not None:
        parts.append(f"Revenue: {revenue:,.0f} {currency}".strip())
    if net_result is not None:
        parts.append(f"Net result: {net_result:,.0f} {currency}".strip())
    return "; ".join(parts) if parts else None


def _reporting_period(entry: dict) -> Optional[str]:
    til_dato = (entry.get("regnskapsperiode") or {}).get("tilDato")
    if not til_dato:
        return None
    return f"FY{til_dato.split('-')[0]}"


def _accounts_facts(
    entity: ResolvedEntity, client: httpx.Client, sleep: Callable[[float], None], now: datetime
) -> list[ConfirmedFact]:
    url = ACCOUNTS_URL.format(org=entity.org_number)
    response = _get(client, url, sleep)
    if response is None or response.status_code != 200:
        return []

    entries = response.json()
    if not isinstance(entries, list) or not entries:
        return []

    content_hash = _content_hash(response)
    entries = sorted(entries, key=lambda e: (e.get("regnskapsperiode") or {}).get("tilDato") or "", reverse=True)

    facts = []
    for i, entry in enumerate(entries):
        value = _format_accounts_value(entry)
        if not value:
            continue
        field_name = "annual_accounts_latest" if i == 0 else "annual_accounts_history"
        facts.append(
            ConfirmedFact(
                field_name, value, url, 100.0, now,
                reporting_period=_reporting_period(entry),
                content_hash=content_hash, extraction_method="registry", source_class="official_registry",
            )
        )
    return facts


def _workplace_value(entry: dict) -> Optional[str]:
    name = entry.get("navn")
    if not name:
        return None
    location = entry.get("beliggenhetsadresse") or entry.get("postadresse") or {}
    kommune = location.get("kommune")
    employees = entry.get("antallAnsatte")
    detail_parts = [p for p in [kommune, f"{employees} ansatte" if employees is not None else None] if p]
    return f"{name} ({', '.join(detail_parts)})" if detail_parts else name


def _workplace_facts(
    entity: ResolvedEntity, client: httpx.Client, sleep: Callable[[float], None], now: datetime
) -> list[ConfirmedFact]:
    url = SUBUNITS_URL.format(org=entity.org_number)
    response = _get(client, url, sleep)
    if response is None or response.status_code != 200:
        return []

    body = response.json()
    content_hash = _content_hash(response)
    entries = (body.get("_embedded") or {}).get("underenheter", [])

    facts = []
    for entry in entries:
        value = _workplace_value(entry)
        if not value:
            continue
        source_url = (entry.get("_links") or {}).get("self", {}).get("href", url)
        facts.append(
            ConfirmedFact(
                "workplace", value, source_url, 100.0, now,
                content_hash=content_hash, extraction_method="registry", source_class="official_registry",
            )
        )
    return facts


def fetch_registry_extras(
    entity: ResolvedEntity,
    budget: BudgetGovernor,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[ConfirmedFact]:
    """
    Fetch leadership roles, filed annual accounts, and registered sub-units
    for `entity` from Brreg's own endpoints, gated by `budget` like any other
    outbound request. Returns [] if the entity wasn't resolved, if the
    budget is exhausted before a given call, or if that endpoint has no data
    for this company (e.g. a newly-formed company with no filed accounts yet)
    -- never fabricated, never raises on a missing/failed endpoint.
    """
    if entity.resolution_state != EvidenceState.AVAILABLE:
        return []

    owns_client = client is None
    client = client or httpx.Client()
    retrieved_at = now()

    fetchers = (_leadership_facts, _accounts_facts, _workplace_facts)
    facts: list[ConfirmedFact] = []
    try:
        for fetcher in fetchers:
            if not budget.can_spend_request():
                break
            new_facts = fetcher(entity, client, sleep, retrieved_at)
            budget.record_request()
            facts.extend(new_facts)
        return facts
    finally:
        if owns_client:
            client.close()
