"""
Stage 5: Assemble.

Contract: docs/component-specs.md -> "src/pipeline/assemble.py"

Responsibility: build the final validated CompanyProfile from confirmed facts
plus the prior snapshot (if any). Every section must be filled, using an
explicit EvidenceState for any section with no confirmed facts -- never omit
a section. Pydantic validation must not be bypassed or swallowed.

annual_accounts.latest/.history, leadership.workplaces, and leadership.leaders
can additionally be populated from src/pipeline/registry_extras.py (Brreg's
own roller/regnskapsregisteret/underenheter endpoints -- see that module's
docstring), fed in via the same `confirmed_facts` list as crawled facts, under
field_name "annual_accounts_latest"/"annual_accounts_history"/"workplace"/
"leader". If a caller doesn't wire that stage in, these sections are honestly
NOT_AVAILABLE/empty rather than fabricated, per the "no fabricated financial
data" hard gate -- never a hard requirement, always a coverage bonus.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Optional

from src.models.profile import (
    Activity,
    AnnualAccounts,
    Claim,
    CompanyProfile,
    EvidenceState,
    LegalIdentity,
    Leadership,
    OnlinePresence,
    RefreshMetadata,
)
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import ConfirmedFact
from src.storage.snapshots import diff_material_changes


def _unavailable_claim(state: EvidenceState) -> Claim:
    return Claim(value=None, state=state, source=None, retrieved_at=None, reporting_period=None)


def _registry_claim(value: Optional[str], entity: ResolvedEntity) -> Claim:
    """A Claim sourced directly from the registry lookup (resolve stage),
    used for legal_name/official_site -- the registry is authoritative for
    these, not the crawled page.

    Two distinct reasons a Claim here can lack a value, kept separate on
    purpose (regression found running a 1,000-company batch: most Norwegian
    companies have no registered website at all, and the two cases were
    conflated, producing `state=available, value=null` -- claiming to have
    verified something that was never there):
      1. Resolution itself failed/was ambiguous/etc -- state mirrors that.
      2. Resolution succeeded, but this specific field has no value on file
         -- state is NOT_AVAILABLE for this field, never "available".
    """
    if entity.resolution_state != EvidenceState.AVAILABLE:
        return Claim(value=None, state=entity.resolution_state, source=entity.source, retrieved_at=None)

    if value is None:
        return Claim(
            value=None,
            state=EvidenceState.NOT_AVAILABLE,
            source=entity.source,
            retrieved_at=entity.retrieved_at,
        )

    return Claim(
        value=value,
        state=EvidenceState.AVAILABLE,
        source=entity.source,
        retrieved_at=entity.retrieved_at,
        content_hash=entity.content_hash,
        source_class="official_registry",
        extraction_method="registry",
        match_confidence=100.0,
    )


def _legal_name_claim(entity: ResolvedEntity) -> Claim:
    return _registry_claim(entity.legal_name, entity)


def _official_site_claim(entity: ResolvedEntity, confirmed_facts: list[ConfirmedFact]) -> Claim:
    """Registry value first (authoritative when present). Falls back to a
    confirmed "official_site" fact -- from extract.py's canonical-link/
    JSON-LD detection, or src/pipeline/discovery.py's search-based discovery
    for companies with no website on file -- when the registry has none.

    Real gap found during implementation: this used to ignore confirmed
    facts entirely, silently discarding anything extract.py/discovery.py
    found even though it had already passed verify()'s precision gate.
    """
    registry_claim = _registry_claim(entity.official_site_candidate, entity)
    if registry_claim.state == EvidenceState.AVAILABLE:
        return registry_claim

    discovered = _first_matching(confirmed_facts, "official_site")
    if discovered:
        return _confirmed_claim(discovered)

    return registry_claim


def _confirmed_claim(fact: ConfirmedFact) -> Claim:
    return Claim(
        value=fact.value,
        state=EvidenceState.AVAILABLE,
        source=fact.source_url,
        retrieved_at=fact.retrieved_at,
        reporting_period=fact.reporting_period,
        content_hash=fact.content_hash,
        source_class=fact.source_class,
        extraction_method=fact.extraction_method,
        match_confidence=fact.match_confidence,
        linked_from=fact.linked_from,
    )


def _first_matching(facts: list[ConfirmedFact], field_name: str) -> Optional[ConfirmedFact]:
    return next((f for f in facts if f.field_name == field_name), None)


def _all_matching(facts: list[ConfirmedFact], field_name: str) -> list[ConfirmedFact]:
    return [f for f in facts if f.field_name == field_name]


def assemble(
    entity: ResolvedEntity,
    confirmed_facts: list[ConfirmedFact],
    previous_snapshot: Optional[CompanyProfile],
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CompanyProfile:
    """
    Build and validate a CompanyProfile. Raises pydantic.ValidationError on
    any schema violation -- callers must not catch-and-drop this; a schema
    violation is a bug to fix, not a profile to silently omit from the batch.
    """
    run_timestamp = now()

    legal_name_claim = _legal_name_claim(entity)
    official_site_claim = _official_site_claim(entity, confirmed_facts)

    brand_fact = _first_matching(confirmed_facts, "organization_name")
    public_brand_claim = _confirmed_claim(brand_fact) if brand_fact else _unavailable_claim(EvidenceState.NOT_AVAILABLE)

    leader_claims = [_confirmed_claim(f) for f in _all_matching(confirmed_facts, "leader")]
    company_profile_claims = [_confirmed_claim(f) for f in _all_matching(confirmed_facts, "company_profile")]
    hiring_claims = [_confirmed_claim(f) for f in _all_matching(confirmed_facts, "hiring_signal")]
    dated_activity_claims = [_confirmed_claim(f) for f in _all_matching(confirmed_facts, "dated_activity")]
    workplace_claims = [_confirmed_claim(f) for f in _all_matching(confirmed_facts, "workplace")]
    annual_history_claims = [_confirmed_claim(f) for f in _all_matching(confirmed_facts, "annual_accounts_history")]

    # src/pipeline/registry_extras.py wires these from Brreg's own accounts
    # endpoint when available; honestly NOT_AVAILABLE otherwise, never
    # fabricated (see module docstring).
    latest_accounts_fact = _first_matching(confirmed_facts, "annual_accounts_latest")
    annual_latest_claim = (
        _confirmed_claim(latest_accounts_fact) if latest_accounts_fact else _unavailable_claim(EvidenceState.NOT_AVAILABLE)
    )

    legal_identity = LegalIdentity(legal_name=legal_name_claim, public_brand=public_brand_claim)
    annual_accounts = AnnualAccounts(latest=annual_latest_claim, history=annual_history_claims)
    leadership = Leadership(leaders=leader_claims, workplaces=workplace_claims)
    online_presence = OnlinePresence(official_site=official_site_claim, company_profiles=company_profile_claims)
    activity = Activity(hiring_signals=hiring_claims, dated_activity=dated_activity_claims)

    evidence_log = (
        [legal_name_claim, public_brand_claim, annual_latest_claim, official_site_claim]
        + annual_history_claims
        + leader_claims
        + workplace_claims
        + company_profile_claims
        + hiring_claims
        + dated_activity_claims
    )

    if previous_snapshot is None:
        refresh_metadata = RefreshMetadata(previous_run_timestamp=None, material_changes=[], is_first_run=True)
    else:
        # Diff against a same-shaped draft first (diff_material_changes only
        # looks at section values, refresh_metadata is irrelevant to it).
        draft = CompanyProfile(
            org_number=entity.org_number,
            run_timestamp=run_timestamp,
            legal_identity=legal_identity,
            annual_accounts=annual_accounts,
            leadership=leadership,
            online_presence=online_presence,
            activity=activity,
            evidence_log=evidence_log,
            refresh_metadata=RefreshMetadata(previous_run_timestamp=None, material_changes=[], is_first_run=True),
        )
        material_changes = diff_material_changes(previous_snapshot, draft)
        refresh_metadata = RefreshMetadata(
            previous_run_timestamp=previous_snapshot.run_timestamp,
            material_changes=material_changes,
            is_first_run=False,
        )

    return CompanyProfile(
        org_number=entity.org_number,
        run_timestamp=run_timestamp,
        legal_identity=legal_identity,
        annual_accounts=annual_accounts,
        leadership=leadership,
        online_presence=online_presence,
        activity=activity,
        evidence_log=evidence_log,
        refresh_metadata=refresh_metadata,
    )
