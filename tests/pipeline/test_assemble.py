"""Tests for src/pipeline/assemble.py -- see docs/component-specs.md."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import src.pipeline.assemble as assemble_module
from src.models.profile import EvidenceState
from src.pipeline.assemble import assemble
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import ConfirmedFact
from tests.conftest import available_claim, make_profile

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _entity(state=EvidenceState.AVAILABLE, legal_name="EQUINOR ASA", site="https://www.equinor.com"):
    return ResolvedEntity(
        org_number="923609016",
        legal_name=legal_name if state == EvidenceState.AVAILABLE else None,
        registered_address="Forusbeen 50, 4035 STAVANGER" if state == EvidenceState.AVAILABLE else None,
        official_site_candidate=site if state == EvidenceState.AVAILABLE else None,
        resolution_state=state,
        source="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=NOW,
        content_hash="entity-hash" if state == EvidenceState.AVAILABLE else None,
    )


def _fact(field_name, value, confidence=95.0, reporting_period=None, content_hash="fact-hash", extraction_method="structured", source_class="company_owned"):
    return ConfirmedFact(
        field_name=field_name,
        value=value,
        source_url="https://www.equinor.com/about",
        match_confidence=confidence,
        retrieved_at=NOW,
        content_hash=content_hash,
        extraction_method=extraction_method,
        source_class=source_class,
        reporting_period=reporting_period,
    )


def test_full_data_populates_all_sections():
    entity = _entity()
    facts = [
        _fact("organization_name", "Equinor ASA"),
        _fact("leader", "Anders Opedal (President and CEO)"),
        _fact("company_profile", "https://www.linkedin.com/company/equinor/"),
        _fact("hiring_signal", "Kahoot! AS is hiring: Senior Backend Engineer"),
        _fact("dated_activity", "2026-08-01: Senior Reservoir Engineer, Stavanger"),
        _fact("workplace", "EQUINOR ASA AVD CCB SOTRA (OYGARDEN, 39 ansatte)"),
        _fact("annual_accounts_latest", "Revenue: 67,956,000,000 USD; Net result: 5,731,000,000 USD", reporting_period="FY2025"),
        _fact("annual_accounts_history", "Revenue: 60,000,000,000 USD", reporting_period="FY2024"),
    ]

    profile = assemble(entity, facts, previous_snapshot=None)

    assert profile.org_number == "923609016"
    assert profile.legal_identity.legal_name.value == "EQUINOR ASA"
    assert profile.legal_identity.legal_name.state == EvidenceState.AVAILABLE
    assert profile.legal_identity.public_brand.value == "Equinor ASA"
    assert profile.online_presence.official_site.value == "https://www.equinor.com"
    assert len(profile.leadership.leaders) == 1
    assert len(profile.online_presence.company_profiles) == 1
    assert len(profile.activity.hiring_signals) == 1
    assert len(profile.activity.dated_activity) == 1
    assert len(profile.leadership.workplaces) == 1
    assert profile.annual_accounts.latest.state == EvidenceState.AVAILABLE
    assert profile.annual_accounts.latest.reporting_period == "FY2025"
    assert len(profile.annual_accounts.history) == 1
    assert profile.annual_accounts.history[0].reporting_period == "FY2024"

    # source policy: content_hash/source_class/extraction_method threaded through
    assert profile.legal_identity.legal_name.content_hash == "entity-hash"
    assert profile.legal_identity.legal_name.source_class == "official_registry"
    assert profile.legal_identity.legal_name.extraction_method == "registry"
    assert profile.legal_identity.public_brand.content_hash == "fact-hash"
    assert profile.legal_identity.public_brand.source_class == "company_owned"
    assert profile.legal_identity.legal_name.match_confidence == 100.0
    assert profile.legal_identity.public_brand.match_confidence == 95.0

    assert profile.refresh_metadata.is_first_run is True
    assert profile.refresh_metadata.previous_run_timestamp is None
    assert profile.refresh_metadata.material_changes == []

    # every claim used anywhere in the profile also appears in evidence_log
    assert len(profile.evidence_log) >= 7


def test_sparse_data_marks_missing_sections_not_available():
    entity = _entity(state=EvidenceState.NOT_AVAILABLE)

    profile = assemble(entity, confirmed_facts=[], previous_snapshot=None)

    assert profile.legal_identity.legal_name.state == EvidenceState.NOT_AVAILABLE
    assert profile.legal_identity.legal_name.value is None
    assert profile.legal_identity.public_brand.state == EvidenceState.NOT_AVAILABLE
    assert profile.online_presence.official_site.state == EvidenceState.NOT_AVAILABLE
    assert profile.annual_accounts.latest.state == EvidenceState.NOT_AVAILABLE
    assert profile.leadership.leaders == []
    assert profile.online_presence.company_profiles == []
    assert profile.activity.hiring_signals == []


def test_resolved_entity_with_no_website_on_file_is_not_available_not_available_with_null_value():
    """Real-world regression, found running a 1,000-company batch: most
    Norwegian companies have no registered website at all. Resolution
    SUCCEEDS (resolution_state=AVAILABLE) but official_site_candidate is
    None. The claim must be NOT_AVAILABLE, never "available" with a null
    value -- that would silently claim to have verified something absent."""
    entity = _entity(site=None)
    entity.resolution_state = EvidenceState.AVAILABLE  # resolution itself succeeded

    profile = assemble(entity, confirmed_facts=[], previous_snapshot=None)

    assert profile.online_presence.official_site.state == EvidenceState.NOT_AVAILABLE
    assert profile.online_presence.official_site.value is None
    assert profile.legal_identity.legal_name.state == EvidenceState.AVAILABLE
    assert profile.legal_identity.legal_name.value == "EQUINOR ASA"


def test_ambiguous_resolution_state_is_carried_through():
    entity = _entity(state=EvidenceState.AMBIGUOUS)
    profile = assemble(entity, confirmed_facts=[], previous_snapshot=None)

    assert profile.legal_identity.legal_name.state == EvidenceState.AMBIGUOUS
    assert profile.online_presence.official_site.state == EvidenceState.AMBIGUOUS


def test_refresh_against_previous_snapshot_detects_material_change():
    entity = _entity(legal_name="NEW LEGAL NAME ASA")
    previous = make_profile(
        org_number="923609016",
        run_timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
        legal_name=available_claim("OLD LEGAL NAME ASA"),
        is_first_run=True,
    )

    profile = assemble(entity, confirmed_facts=[], previous_snapshot=previous)

    assert profile.refresh_metadata.is_first_run is False
    assert profile.refresh_metadata.previous_run_timestamp == previous.run_timestamp
    assert any("legal_name" in c for c in profile.refresh_metadata.material_changes)


def test_refresh_against_unchanged_previous_snapshot_has_no_material_changes():
    entity = _entity(legal_name="EQUINOR ASA", site="https://www.equinor.com")
    previous = make_profile(
        org_number="923609016",
        run_timestamp=datetime(2025, 6, 1, tzinfo=timezone.utc),
        legal_name=available_claim("EQUINOR ASA"),
        official_site=available_claim("https://www.equinor.com"),
        is_first_run=True,
    )

    profile = assemble(entity, confirmed_facts=[], previous_snapshot=previous)

    assert profile.refresh_metadata.is_first_run is False
    assert profile.refresh_metadata.material_changes == []


def test_schema_violation_raises_loudly(monkeypatch):
    def broken_claim(*args, **kwargs):
        from src.models.profile import Claim

        return Claim(value="should not be set", state=EvidenceState.NOT_AVAILABLE)

    monkeypatch.setattr(assemble_module, "_legal_name_claim", broken_claim)

    with pytest.raises(ValidationError):
        assemble(_entity(), confirmed_facts=[], previous_snapshot=None)
