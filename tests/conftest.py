"""Shared test fixtures/factories. No live network calls anywhere in tests
(docs/testing-strategy.md) -- every fixture here is either a literal, an
in-memory mock, or loaded from fixtures/ on disk."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

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


def available_claim(
    value: str,
    source: str = "https://example.com/",
    retrieved_at: Optional[datetime] = None,
    reporting_period: Optional[str] = None,
    content_hash: Optional[str] = None,
    source_class: Optional[str] = None,
    extraction_method: Optional[str] = None,
    match_confidence: Optional[float] = None,
    linked_from: Optional[str] = None,
) -> Claim:
    return Claim(
        value=value,
        state=EvidenceState.AVAILABLE,
        source=source,
        retrieved_at=retrieved_at or datetime(2026, 1, 1, tzinfo=timezone.utc),
        reporting_period=reporting_period,
        content_hash=content_hash,
        source_class=source_class,
        extraction_method=extraction_method,
        match_confidence=match_confidence,
        linked_from=linked_from,
    )


def unavailable_claim(state: EvidenceState = EvidenceState.NOT_AVAILABLE) -> Claim:
    return Claim(value=None, state=state, source=None, retrieved_at=None, reporting_period=None)


def make_profile(
    org_number: str = "923609016",
    run_timestamp: Optional[datetime] = None,
    legal_name: Optional[Claim] = None,
    public_brand: Optional[Claim] = None,
    official_site: Optional[Claim] = None,
    annual_latest: Optional[Claim] = None,
    annual_history: Optional[list[Claim]] = None,
    leaders: Optional[list[Claim]] = None,
    workplaces: Optional[list[Claim]] = None,
    company_profiles: Optional[list[Claim]] = None,
    hiring_signals: Optional[list[Claim]] = None,
    dated_activity: Optional[list[Claim]] = None,
    is_first_run: bool = True,
    previous_run_timestamp: Optional[datetime] = None,
    material_changes: Optional[list[str]] = None,
) -> CompanyProfile:
    ts = run_timestamp or datetime(2026, 1, 1, tzinfo=timezone.utc)
    legal_name = legal_name or unavailable_claim()
    official_site = official_site or unavailable_claim()
    annual_latest = annual_latest or unavailable_claim()

    return CompanyProfile(
        org_number=org_number,
        run_timestamp=ts,
        legal_identity=LegalIdentity(legal_name=legal_name, public_brand=public_brand or unavailable_claim()),
        annual_accounts=AnnualAccounts(latest=annual_latest, history=annual_history or []),
        leadership=Leadership(leaders=leaders or [], workplaces=workplaces or []),
        online_presence=OnlinePresence(official_site=official_site, company_profiles=company_profiles or []),
        activity=Activity(hiring_signals=hiring_signals or [], dated_activity=dated_activity or []),
        evidence_log=[],
        refresh_metadata=RefreshMetadata(
            previous_run_timestamp=previous_run_timestamp,
            material_changes=material_changes or [],
            is_first_run=is_first_run,
        ),
    )
