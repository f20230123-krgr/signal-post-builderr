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
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from src.orchestrator.budget import BudgetGovernor
from src.pipeline.net import new_client
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import ConfirmedFact
from src.models.profile import EvidenceState

ROLES_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller"
ACCOUNTS_URL = "https://data.brreg.no/regnskapsregisteret/regnskap/{org}"
SUBUNITS_URL = "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}"
ENHET_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/{org}"
NAME_SEARCH_URL = "https://data.brreg.no/enhetsregisteret/api/enheter"

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
) -> tuple[list[ConfirmedFact], EvidenceState]:
    """Returns (facts, state) instead of a bare list -- real evaluator
    feedback: "a temporary source error was also recorded as information
    being unavailable." A transport failure or exhausted-retry 5xx must be
    distinguishable from Brreg genuinely having no accounts on file (404, or
    a 200 with an empty list), so the caller (assemble.py) can honestly mark
    a fetch problem as FAILED rather than a confirmed NOT_AVAILABLE, and can
    optionally carry forward the last known good value on refresh."""
    url = ACCOUNTS_URL.format(org=entity.org_number)
    response = _get(client, url, sleep)

    if response is None or response.status_code >= 500:
        return [], EvidenceState.FAILED
    if response.status_code == 404:
        return [], EvidenceState.NOT_AVAILABLE
    if response.status_code != 200:
        return [], EvidenceState.FAILED

    entries = response.json()
    if not isinstance(entries, list) or not entries:
        return [], EvidenceState.NOT_AVAILABLE

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
    return facts, (EvidenceState.AVAILABLE if facts else EvidenceState.NOT_AVAILABLE)


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
    entity: ResolvedEntity,
    client: httpx.Client,
    sleep: Callable[[float], None],
    now: datetime,
    budget: BudgetGovernor,
) -> list[ConfirmedFact]:
    """Follows Brreg's own pagination (`_links.next`, confirmed against the
    live API to be real Spring-style paging) until there are no more pages.
    Real evaluator feedback: reading only the first page missed 116
    workplaces across three companies in the submitted batch. Each
    additional page is its own budget-gated request, same as any other
    outbound call -- stops cleanly (partial results kept) if the budget
    runs out mid-pagination, never fetches indefinitely."""
    url: Optional[str] = SUBUNITS_URL.format(org=entity.org_number)
    facts: list[ConfirmedFact] = []
    # Only a successful response proves the sub-unit list is genuinely empty.
    # A transport failure, a 5xx, or an exhausted budget means we simply
    # don't know -- and the own-address fallback below must not turn "we
    # couldn't ask" into a registry-confirmed workplace claim.
    confirmed_empty = False

    while url is not None:
        if not budget.can_spend_request():
            break
        response = _get(client, url, sleep)
        budget.record_request()
        if response is None or response.status_code != 200:
            break
        confirmed_empty = True

        body = response.json()
        content_hash = _content_hash(response)
        entries = (body.get("_embedded") or {}).get("underenheter", [])

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

        url = (body.get("_links") or {}).get("next", {}).get("href")

    if not facts and confirmed_empty:
        # A company with no registered sub-units still has a workplace: its
        # own registered business location. Real-world gap -- ~25% of a
        # 1,000-company batch reported no workplace at all while the registry
        # held a perfectly good one for each. Costs nothing (the sub-units
        # request already happened and came back empty) and is the same
        # registry-sourced trust tier as every other fact here.
        own_location = _own_registered_workplace_value(entity)
        if own_location:
            facts.append(
                ConfirmedFact(
                    "workplace", own_location, entity.source or SUBUNITS_URL.format(org=entity.org_number),
                    100.0, now, extraction_method="registry", source_class="official_registry",
                )
            )

    return facts


_SUBUNIT_URL_RE = re.compile(r"/underenheter/(\d{9})(?:\D|$)")


def subunit_org_numbers(facts: list[ConfirmedFact]) -> set[str]:
    """Org numbers of the entity's registered sub-units, read from the
    workplace facts' own registry URLs (.../underenheter/<orgnr>). Used to
    attribute NAV job ads, which carry the workplace's org number."""
    return {
        m.group(1)
        for f in facts
        if f.field_name == "workplace"
        for m in [_SUBUNIT_URL_RE.search(f.source_url or "")]
        if m
    }


def _own_registered_workplace_value(entity: ResolvedEntity) -> Optional[str]:
    if not entity.legal_name:
        return None
    address = (entity.registered_address or "").strip()
    return f"{entity.legal_name} ({address})" if address else entity.legal_name


UPDATES_URL = "https://data.brreg.no/enhetsregisteret/api/oppdateringer/enheter?organisasjonsnummer={org}&size=100"

# A company can accumulate 40+ registry update events; republishing all of
# them would bury any real signal in registry churn. The most recent few are
# what actually answer "has anything happened with this company lately".
DEFAULT_MAX_UPDATE_EVENTS = 3


def fetch_registry_update_activity(
    entity: ResolvedEntity,
    budget: BudgetGovernor,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    max_events: int = DEFAULT_MAX_UPDATE_EVENTS,
) -> list[ConfirmedFact]:
    """Dated registry-change events for this company, newest first.

    Brreg's `oppdateringer/enheter` endpoint accepts an
    `organisasjonsnummer` filter (confirmed against the live API), making it
    a per-company, dated, authoritative and free source of "dated public
    activity" -- the one such source that works for the ~89% of companies
    with no website at all, where every other dated_activity path has
    nothing to crawl.

    Same trust tier as this module's other facts: fetched by org number
    against the official registry, so there's no page to misattribute.
    `endringstype` is the registry's own change label ("Endring"); it says
    the record changed, not what changed, so the published value says
    exactly that and no more."""
    if not budget.can_spend_request():
        return []

    real_client = client or new_client()
    try:
        url = UPDATES_URL.format(org=entity.org_number)
        response = _get(real_client, url, sleep)
        budget.record_request()
        if response is None or response.status_code != 200:
            return []

        try:
            body = response.json()
        except ValueError:
            return []

        events = (body.get("_embedded") or {}).get("oppdaterteEnheter", [])
        dated = [e for e in events if e.get("dato")]
        dated.sort(key=lambda e: e["dato"], reverse=True)

        content_hash = _content_hash(response)
        retrieved_at = now()
        facts = []
        for event in dated[:max_events]:
            day = str(event["dato"]).split("T")[0]
            change = event.get("endringstype") or "Endring"
            facts.append(
                ConfirmedFact(
                    "dated_activity",
                    f"Registry record updated ({change}) on {day} - Brønnøysundregistrene",
                    url,
                    100.0,
                    retrieved_at,
                    content_hash=content_hash,
                    extraction_method="registry",
                    source_class="official_registry",
                )
            )
        return facts
    finally:
        if client is None:
            real_client.close()


UNIVERSE_SOURCE = "signalpost-company-universe-2025.jsonl.gz"


def universe_identity_facts(
    org_number: str,
    universe_entry: Optional[dict],
    now: datetime,
) -> list[ConfirmedFact]:
    """Identity facts already carried by Builderr's frozen universe record:
    industry, employee count, legal form, insolvency status.

    Free in every sense that matters here -- the record is already loaded in
    memory by src/pipeline/universe.py (resolve() reads the same object), so
    this spends zero requests, and it's official-registry data fetched by
    org number, so there's no page to misattribute and nothing for the
    95%-precision gate to guard against (same reasoning as this module's
    other facts -- see module docstring).

    A field the record doesn't actually carry is omitted, never guessed."""
    if not universe_entry:
        return []

    # Same fingerprint resolve() records for legal_name from this exact record,
    # so every fact read from the manifest carries identical evidence.
    content_hash = hashlib.sha256(json.dumps(universe_entry, sort_keys=True).encode("utf-8")).hexdigest()

    def fact(field_name: str, value: str) -> ConfirmedFact:
        return ConfirmedFact(
            field_name, value, UNIVERSE_SOURCE, 100.0, now,
            content_hash=content_hash, extraction_method="registry", source_class="official_registry",
        )

    facts: list[ConfirmedFact] = []

    code = (universe_entry.get("industry_code") or "").strip()
    label = (universe_entry.get("industry_label") or "").strip()
    industry = " ".join(p for p in (code, label) if p)
    if industry:
        facts.append(fact("industry", industry))

    employees = universe_entry.get("employees")
    if employees is not None:
        facts.append(fact("employee_count", str(employees)))

    legal_form = (universe_entry.get("legal_form") or "").strip()
    if legal_form:
        facts.append(fact("legal_form", legal_form))

    if universe_entry.get("bankrupt"):
        status = "Bankrupt"
    elif universe_entry.get("liquidating"):
        status = "In liquidation"
    else:
        status = "Active"
    facts.append(fact("operating_status", status))

    return facts


# A company can have been renamed many times; the most recent few are what
# matter for identity and search, and more would just be registry history.
MAX_FORMER_NAMES = 3


@dataclass
class LiveRegistryDetails:
    facts: list[ConfirmedFact] = field(default_factory=list)
    former_names: list[str] = field(default_factory=list)
    # Industry / employee count / legal form / status read from the live
    # record. Kept apart from `facts` (published for every company) because
    # they are only used when the universe manifest isn't supplying the same
    # four claims, so nothing is published twice.
    identity_facts: list[ConfirmedFact] = field(default_factory=list)


def fetch_live_registry_details(
    entity: ResolvedEntity,
    budget: BudgetGovernor,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> LiveRegistryDetails:
    """Founding date and former names from the live registry record.

    Builderr's frozen universe manifest (which resolve() reads, request-free)
    doesn't carry either. Measured on 40 random companies with no registered
    website: founding date present for 40/40, former names for 11/40.

    One request per company, and an extra rather than core data, so it backs
    off once the batch budget is nearly spent (budget.should_degrade()) --
    the requests left must go to core registry data, not to this.

    Returns the facts (founded_date, plus dated_activity for founding and each
    rename) and the former names themselves, newest first, for the
    former-name website search tier in runner.py."""
    details = LiveRegistryDetails()
    if entity.resolution_state != EvidenceState.AVAILABLE:
        return details

    url = ENHET_URL.format(org=entity.org_number)
    reuse_record = isinstance(entity.raw_record, dict) and entity.source == url
    if not reuse_record and (budget.should_degrade() or not budget.can_spend_request()):
        return details

    owns_client = client is None
    client = client or new_client()
    try:
        if reuse_record:
            # resolve() just downloaded this exact record: reuse it.
            body, content_hash = entity.raw_record, entity.content_hash
        else:
            response = _get(client, url, sleep)
            budget.record_request()
            if response is None or response.status_code != 200:
                return details
            try:
                body = response.json()
            except ValueError:
                return details
            content_hash = _content_hash(response)
        retrieved_at = now()

        def fact(field_name: str, value: str) -> ConfirmedFact:
            return ConfirmedFact(
                field_name, value, url, 100.0, retrieved_at,
                content_hash=content_hash, extraction_method="registry", source_class="official_registry",
            )

        founded = body.get("stiftelsesdato")
        if isinstance(founded, str) and founded.strip():
            founded = founded.strip()[:10]
            details.facts.append(fact("founded_date", founded))
            details.facts.append(fact("dated_activity", f"Founded on {founded} - Brønnøysundregistrene"))

        details.identity_facts = _live_identity_facts(body, fact)

        former = [
            h for h in (body.get("historiskeNavn") or [])
            if isinstance(h, dict) and isinstance(h.get("navn"), str) and h["navn"].strip()
        ]
        former.sort(key=lambda h: str(h.get("tilDato") or ""), reverse=True)
        for entry in former[:MAX_FORMER_NAMES]:
            name = entry["navn"].strip()
            details.former_names.append(name)
            renamed_on = str(entry.get("tilDato") or "").split(" ")[0].split("T")[0]
            if renamed_on:
                details.facts.append(
                    fact("dated_activity", f"Renamed from '{name}' on {renamed_on} - Brønnøysundregistrene")
                )
        return details
    finally:
        if owns_client:
            client.close()


def _live_identity_facts(body: dict, fact: Callable[[str, str], ConfirmedFact]) -> list[ConfirmedFact]:
    """The four claims universe_identity_facts() reads from the manifest, read
    from the live registry record instead, in the same published shape. A field
    the record doesn't carry is omitted, never guessed -- in particular a record
    with no bankruptcy/liquidation flags at all says nothing about status."""
    facts: list[ConfirmedFact] = []

    code_block = body.get("naeringskode1")
    if isinstance(code_block, dict):
        industry = " ".join(
            p for p in (str(code_block.get("kode") or "").strip(), str(code_block.get("beskrivelse") or "").strip()) if p
        )
        if industry:
            facts.append(fact("industry", industry))

    employees = body.get("antallAnsatte")
    if isinstance(employees, int) and not isinstance(employees, bool):
        facts.append(fact("employee_count", str(employees)))

    form = body.get("organisasjonsform")
    legal_form = str(form.get("kode") or "").strip() if isinstance(form, dict) else ""
    if legal_form:
        facts.append(fact("legal_form", legal_form))

    flags = ("konkurs", "underAvvikling", "underTvangsavviklingEllerTvangsopplosning")
    if any(flag in body for flag in flags):
        if body.get("konkurs"):
            status = "Bankrupt"
        elif body.get("underAvvikling") or body.get("underTvangsavviklingEllerTvangsopplosning"):
            status = "In liquidation"
        else:
            status = "Active"
        facts.append(fact("operating_status", status))

    return facts


def _normalized_company_name(name: str) -> str:
    return " ".join(name.upper().split())


def name_is_held_by_another_entity(
    name: str,
    org_number: str,
    budget: BudgetGovernor,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """True if a DIFFERENT registered entity currently holds exactly this name.

    Guards the former-name website search: a name this company gave up can
    since have been registered by someone else, and a site under that name
    is then theirs, not ours. Precision-first on purpose -- an exhausted
    budget, a failed request or an unreadable response all answer True
    ("don't risk it"), since the only cost is skipping one extra search."""
    if not budget.can_spend_request():
        return True
    owns_client = client is None
    client = client or new_client()
    try:
        try:
            response = client.get(NAME_SEARCH_URL, params={"navn": name, "size": 20}, timeout=10.0)
        except httpx.HTTPError:
            response = None
        budget.record_request()
        if response is None or response.status_code != 200:
            return True
        try:
            units = (response.json().get("_embedded") or {}).get("enheter", [])
        except ValueError:
            return True
        wanted = _normalized_company_name(name)
        return any(
            _normalized_company_name(str(u.get("navn") or "")) == wanted
            and str(u.get("organisasjonsnummer")) != org_number
            for u in units
        )
    finally:
        if owns_client:
            client.close()


def fetch_leadership_only(
    entity: ResolvedEntity,
    budget: BudgetGovernor,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[ConfirmedFact]:
    """Leadership only, no accounts/workplaces -- used by discovery.py's
    "leader/founder bridge" (agent playbook §2: a verified CEO/board-chair
    name as a secondary search seed when the company name alone finds
    nothing), which needs leader names BEFORE fetch_registry_extras()
    normally runs later in the pipeline. The normal fetch_registry_extras()
    call still re-fetches leadership as part of its own flow -- one small,
    bounded duplicate roles request for the subset of companies with no
    registered site, acceptable given the ample per-company budget
    headroom, and far simpler/safer than restructuring the whole pipeline
    order to share a single fetch."""
    if entity.resolution_state != EvidenceState.AVAILABLE:
        return []
    owns_client = client is None
    client = client or new_client()
    try:
        if not budget.can_spend_request():
            return []
        facts = _leadership_facts(entity, client, sleep, now())
        budget.record_request()
        return facts
    finally:
        if owns_client:
            client.close()


def fetch_registry_extras(
    entity: ResolvedEntity,
    budget: BudgetGovernor,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[list[ConfirmedFact], EvidenceState]:
    """
    Fetch leadership roles, filed annual accounts, and registered sub-units
    for `entity` from Brreg's own endpoints, gated by `budget` like any other
    outbound request. Returns ([], NOT_AVAILABLE) if the entity wasn't
    resolved or the budget ran out before the accounts call was even
    attempted -- never fabricated, never raises on a missing/failed endpoint.

    The second return value is specifically the annual-accounts fetch
    outcome (AVAILABLE/NOT_AVAILABLE/FAILED) -- assemble.py needs this to
    distinguish "Brreg confirms no accounts filed" from "the call failed",
    which a plain fact list can't express since both look like "no facts".
    """
    if entity.resolution_state != EvidenceState.AVAILABLE:
        return [], EvidenceState.NOT_AVAILABLE

    owns_client = client is None
    client = client or new_client()
    retrieved_at = now()

    facts: list[ConfirmedFact] = []
    accounts_state = EvidenceState.NOT_AVAILABLE
    try:
        if not budget.can_spend_request():
            return facts, accounts_state
        facts.extend(_leadership_facts(entity, client, sleep, retrieved_at))
        budget.record_request()

        if not budget.can_spend_request():
            return facts, accounts_state
        accounts_facts, accounts_state = _accounts_facts(entity, client, sleep, retrieved_at)
        budget.record_request()
        facts.extend(accounts_facts)

        facts.extend(_workplace_facts(entity, client, sleep, retrieved_at, budget))
        return facts, accounts_state
    finally:
        if owns_client:
            client.close()
