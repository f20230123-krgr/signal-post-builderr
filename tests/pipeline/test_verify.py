"""
Tests for src/pipeline/verify.py -- see docs/component-specs.md.

This stage enforces the 95%-precision hard gate. These tests are the closest
thing we have to a direct, fast check on that gate -- keep them honest.
"""
from datetime import datetime, timezone

import src.pipeline.verify as verify_module
from src.models.profile import EvidenceState
from src.pipeline.extract import RawFact
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import NAME_MATCH_THRESHOLD, verify

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _entity(legal_name="EQUINOR ASA", official_site="https://www.equinor.com"):
    return ResolvedEntity(
        org_number="923609016",
        legal_name=legal_name,
        registered_address="Forusbeen 50, 4035 STAVANGER",
        official_site_candidate=official_site,
        resolution_state=EvidenceState.AVAILABLE,
        source="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=NOW,
    )


def _fact(field_name, value, source_url="https://www.equinor.com/about", extraction_method="structured", content_hash="abc123", linked_from=None):
    return RawFact(
        field_name=field_name,
        value=value,
        source_url=source_url,
        extraction_method=extraction_method,
        extracted_at=NOW,
        content_hash=content_hash,
        linked_from=linked_from,
    )


def test_exact_name_match_is_accepted():
    fact = _fact("organization_name", "Equinor ASA")
    confirmed = verify(fact, _entity())

    assert confirmed is not None
    assert confirmed.field_name == "organization_name"
    assert confirmed.match_confidence >= NAME_MATCH_THRESHOLD
    assert confirmed.retrieved_at == NOW
    assert confirmed.content_hash == "abc123"
    assert confirmed.extraction_method == "structured"
    assert confirmed.source_class == "company_owned"


def test_similar_but_different_company_name_is_rejected():
    # Same domain, but the extracted name text doesn't actually match --
    # scores well below threshold (verified against real RapidFuzz output).
    fact = _fact("organization_name", "Equinox Holding")
    confirmed = verify(fact, _entity())

    assert confirmed is None


def test_fact_from_a_different_domain_than_the_official_site_is_rejected():
    # A look-alike/imposter site could use the exact same company name --
    # provenance (source domain) must gate independently of name similarity.
    fact = _fact("organization_name", "Equinor ASA", source_url="https://www.equinor-totally-legit.example/")
    confirmed = verify(fact, _entity())

    assert confirmed is None


def test_non_name_bearing_fact_from_official_site_is_accepted_on_provenance():
    fact = _fact("registered_address", "Forusbeen 50, 4035 Stavanger")
    confirmed = verify(fact, _entity())

    assert confirmed is not None
    assert confirmed.value == "Forusbeen 50, 4035 Stavanger"


def test_no_legal_name_on_entity_means_nothing_can_be_verified():
    fact = _fact("organization_name", "Equinor ASA")
    confirmed = verify(fact, _entity(legal_name=None))

    assert confirmed is None


def test_fact_from_ats_domain_is_accepted_when_linked_from_official_site():
    """Evaluator feedback: "Social profiles and external hiring systems ...
    should be accepted only when linked from the company's official site.
    Preserve that link chain as evidence." """
    fact = _fact(
        "hiring_signal",
        "Backend Engineer (posted 2026-06-20)",
        source_url="https://boards.greenhouse.io/equinor",
        linked_from="https://www.equinor.com/careers",
    )
    confirmed = verify(fact, _entity())

    assert confirmed is not None
    assert confirmed.source_class == "external"
    assert confirmed.linked_from == "https://www.equinor.com/careers"


def test_fact_from_ats_domain_is_rejected_without_a_linked_from_chain():
    """No linked_from at all -- an off-domain fact must still be rejected on
    provenance, exactly as before this feature existed."""
    fact = _fact("hiring_signal", "Backend Engineer", source_url="https://boards.greenhouse.io/equinor")
    confirmed = verify(fact, _entity())

    assert confirmed is None


def test_fact_from_ats_domain_is_rejected_when_linked_from_a_different_companys_site():
    fact = _fact(
        "hiring_signal",
        "Backend Engineer",
        source_url="https://boards.greenhouse.io/equinor",
        linked_from="https://some-other-company.example/careers",
    )
    confirmed = verify(fact, _entity())

    assert confirmed is None


def test_threshold_boundary_case_is_explicit(monkeypatch):
    monkeypatch.setattr(verify_module, "name_similarity", lambda a, b: float(NAME_MATCH_THRESHOLD))
    accepted = verify(_fact("organization_name", "anything"), _entity())
    assert accepted is not None
    assert accepted.match_confidence == NAME_MATCH_THRESHOLD

    monkeypatch.setattr(verify_module, "name_similarity", lambda a, b: NAME_MATCH_THRESHOLD - 0.01)
    rejected = verify(_fact("organization_name", "anything"), _entity())
    assert rejected is None
