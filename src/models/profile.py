"""
Pydantic schema for the Signalpost company profile.

Contract source of truth: docs/data-schema.md
Do not add fields here without updating that doc first — code should conform
to docs, not the other way around (see CLAUDE.md working agreement).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, model_validator


class EvidenceState(str, Enum):
    AVAILABLE = "available"
    NOT_AVAILABLE = "not_available"
    BLOCKED = "blocked"
    NOT_APPLICABLE = "not_applicable"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


class Claim(BaseModel):
    """One sourced fact. Every field the pipeline emits is wrapped in this.

    content_hash/source_class/extraction_method were added after reviewing
    signalpost-sources.md's publication rules ("Every claim records source
    URL..., retrieval time,..., content hash and extraction method") and the
    reference agent's OUTPUT_CONTRACT.md. They are populated wherever the
    pipeline can (see resolve.py/extract.py/verify.py/registry_extras.py) but
    deliberately left optional/unenforced here rather than added as a hard
    validator requirement -- doing the latter would force every existing
    ResolvedEntity/RawFact/ConfirmedFact test construction across the suite
    to supply them, for a schema-strictness gain that isn't itself a hard
    gate. Revisit if a stricter contract is confirmed.
    """

    value: Optional[str] = None
    state: EvidenceState
    source: Optional[str] = None
    retrieved_at: Optional[datetime] = None
    reporting_period: Optional[str] = None
    content_hash: Optional[str] = None
    source_class: Optional[str] = None
    extraction_method: Optional[str] = None
    # 0-100 identity-match confidence (RapidFuzz score for crawled facts,
    # 100.0 for registry-sourced facts -- see verify.py/registry_extras.py/
    # resolve.py). Same "populated but not enforced" rationale as above.
    match_confidence: Optional[float] = None
    # Set only when this claim came from an external platform (e.g. an ATS
    # like Greenhouse) reached by following a link found on the entity's own
    # official site -- the URL of that official-site page, preserving the
    # link chain as evidence (evaluator feedback, 2026-09). None otherwise.
    linked_from: Optional[str] = None
    # ISO date (YYYY-MM-DD) the fact itself is effective/true as of -- the end
    # of an accounting period, a role's last-changed date, a registry event's
    # date. Distinct from retrieved_at (when we fetched it) and from the
    # human-readable reporting_period ("FY2025"). Builderr's rule: keep "the
    # source, retrieval date and relevant reporting period for every claim".
    effective_date: Optional[str] = None
    # A short VERBATIM excerpt of the captured source that supports this claim
    # (evidence-span validity is a listed measurement). The published value is
    # often a formatted string that never appears literally in the source
    # (e.g. "Anders Opedal (CEO)" vs {"fornavn":"Anders","etternavn":"Opedal"});
    # this is the text a checker can actually find. None -> envelope falls back
    # to the value.
    evidence_span: Optional[str] = None

    @model_validator(mode="after")
    def _value_only_when_available(self) -> "Claim":
        if self.value is not None and self.state != EvidenceState.AVAILABLE:
            raise ValueError(
                "Claim.value must be null unless state == 'available' "
                f"(got state={self.state!r}, value={self.value!r})"
            )
        if self.state == EvidenceState.AVAILABLE and self.retrieved_at is None:
            raise ValueError("Claim with state='available' must set retrieved_at")
        return self


class LegalIdentity(BaseModel):
    legal_name: Claim
    public_brand: Claim
    # Free official-registry identity facts carried by Builderr's own frozen
    # universe record (see registry_extras.universe_identity_facts): zero
    # extra requests, 100% precision. Optional (not a required Claim) so an
    # entity resolved outside the universe -- or any existing caller that
    # doesn't supply them -- still validates.
    industry: Optional[Claim] = None
    employee_count: Optional[Claim] = None
    legal_form: Optional[Claim] = None
    operating_status: Optional[Claim] = None
    # Founding date from the live registry record (registry_extras.
    # fetch_live_registry_details) -- not carried by the universe manifest.
    founded_date: Optional[Claim] = None


class AnnualAccounts(BaseModel):
    latest: Claim
    history: list[Claim] = []
    # The discrete figures of the newest filings, keyed by claim field (revenue,
    # operating_result, profit_before_tax, annual_result, total_assets,
    # total_equity, total_debt) plus annual_report_pdf, each claim carrying its
    # reporting_period and effective_date. See registry_extras.ACCOUNT_METRICS.
    metrics: dict[str, list[Claim]] = {}


class Leadership(BaseModel):
    leaders: list[Claim] = []
    workplaces: list[Claim] = []


class OnlinePresence(BaseModel):
    official_site: Claim
    company_profiles: list[Claim] = []


class Activity(BaseModel):
    hiring_signals: list[Claim] = []
    dated_activity: list[Claim] = []


class RefreshMetadata(BaseModel):
    previous_run_timestamp: Optional[datetime] = None
    material_changes: list[str] = []
    is_first_run: bool

    @model_validator(mode="after")
    def _first_run_is_consistent(self) -> "RefreshMetadata":
        if self.is_first_run and (
            self.previous_run_timestamp is not None or self.material_changes
        ):
            raise ValueError(
                "is_first_run=True requires previous_run_timestamp=None "
                "and material_changes=[]"
            )
        return self


class CompanyProfile(BaseModel):
    org_number: str
    run_timestamp: datetime

    legal_identity: LegalIdentity
    annual_accounts: AnnualAccounts
    leadership: Leadership
    online_presence: OnlinePresence
    activity: Activity
    evidence_log: list[Claim] = []
    refresh_metadata: RefreshMetadata
