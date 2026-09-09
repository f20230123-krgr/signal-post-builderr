"""
Tests for src/pipeline/envelope.py.

Contract: docs/component-specs.md -> "src/pipeline/envelope.py"

The Signalpost reference agent's OUTPUT_CONTRACT.md defines the actual
submission shape: one JSON envelope per company with a flat `claims` list
(each referencing evidence by id), a separate deduplicated `evidence` list,
plus `run`/`changes`/`errors`/`operations`. Our internal CompanyProfile is a
nested, Pydantic-validated model (good for correctness enforcement); this
module is the export layer that converts it into that exact submission shape.
"""
from datetime import datetime, timezone

from src.models.profile import EvidenceState
from src.pipeline.envelope import to_envelope
from tests.conftest import available_claim, make_profile, unavailable_claim


def test_available_claim_produces_one_claim_and_one_evidence_entry():
    profile = make_profile(
        org_number="923609016",
        legal_name=available_claim(
            "EQUINOR ASA",
            source="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
            content_hash="hash-a",
            source_class="official_registry",
            extraction_method="registry",
            match_confidence=100.0,
        ),
    )

    envelope = to_envelope(profile, run_id="run-1")

    assert envelope["organisation_number"] == "923609016"
    legal_name_claim = next(c for c in envelope["claims"] if c["field"] == "legal_name")
    assert legal_name_claim["value"] == "EQUINOR ASA"
    assert legal_name_claim["availability"] == "available"
    assert legal_name_claim["confidence"] == 1.0
    assert len(legal_name_claim["evidence_ids"]) == 1

    ev_id = legal_name_claim["evidence_ids"][0]
    evidence = next(e for e in envelope["evidence"] if e["id"] == ev_id)
    assert evidence["source_url"] == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    assert evidence["source_class"] == "official_registry"
    assert evidence["content_sha256"] == "hash-a"
    assert evidence["claim_span"] == "EQUINOR ASA"


def test_unavailable_claim_has_no_evidence_and_null_value():
    profile = make_profile(org_number="923609016", legal_name=unavailable_claim(EvidenceState.NOT_AVAILABLE))

    envelope = to_envelope(profile, run_id="run-1")

    legal_name_claim = next(c for c in envelope["claims"] if c["field"] == "legal_name")
    assert legal_name_claim["value"] is None
    assert legal_name_claim["availability"] == "not_available"
    assert legal_name_claim["evidence_ids"] == []


def test_claims_from_the_same_page_share_one_deduplicated_evidence_entry():
    same_source_kwargs = dict(
        source="https://www.equinor.com/about",
        content_hash="page-hash",
        source_class="company_owned",
        extraction_method="text",
        match_confidence=95.0,
    )
    profile = make_profile(
        org_number="923609016",
        hiring_signals=[available_claim("Hiring: Backend Engineer", **same_source_kwargs)],
        dated_activity=[available_claim("2026-08-01: new office opened", **same_source_kwargs)],
    )

    envelope = to_envelope(profile, run_id="run-1")

    assert len(envelope["evidence"]) == 1
    hiring_claim = next(c for c in envelope["claims"] if c["field"] == "hiring_signal")
    dated_claim = next(c for c in envelope["claims"] if c["field"] == "dated_activity")
    assert hiring_claim["evidence_ids"] == dated_claim["evidence_ids"]


def test_list_fields_produce_one_claim_entry_each():
    profile = make_profile(
        org_number="923609016",
        leaders=[available_claim("Anders Opedal (Daglig leder)"), available_claim("Jarle Kjell Roth (Styrets leder)")],
    )

    envelope = to_envelope(profile, run_id="run-1")

    leader_claims = [c for c in envelope["claims"] if c["field"] == "leader"]
    assert len(leader_claims) == 2


def test_material_changes_pass_through_as_changes():
    profile = make_profile(org_number="923609016", is_first_run=False, material_changes=["legal_name: 'A' -> 'B'"])

    envelope = to_envelope(profile, run_id="run-1")

    assert envelope["changes"] == ["legal_name: 'A' -> 'B'"]


def test_failed_and_blocked_claims_are_surfaced_as_errors():
    profile = make_profile(
        org_number="923609016",
        legal_name=unavailable_claim(EvidenceState.FAILED),
        official_site=unavailable_claim(EvidenceState.BLOCKED),
    )

    envelope = to_envelope(profile, run_id="run-1")

    error_fields = {(e["field"], e["state"]) for e in envelope["errors"]}
    assert ("legal_name", "failed") in error_fields
    assert ("official_website", "blocked") in error_fields


def test_run_metadata_and_operations_are_included():
    profile = make_profile(org_number="923609016", run_timestamp=datetime(2026, 8, 24, 6, 0, 0, tzinfo=timezone.utc))

    envelope = to_envelope(
        profile,
        run_id="2026-08-24-a",
        operations={"requests": 4, "runtime_ms": 8120, "third_party_cost_usd": 0.0},
    )

    assert envelope["run"]["run_id"] == "2026-08-24-a"
    assert envelope["run"]["terminal_status"] == "completed"
    assert envelope["run"]["started_at"] == "2026-08-24T06:00:00Z"
    assert envelope["run"]["completed_at"] == "2026-08-24T06:00:00Z"
    assert envelope["operations"] == {"requests": 4, "runtime_ms": 8120, "third_party_cost_usd": 0.0}


def test_linked_from_is_preserved_in_the_evidence_entry():
    """Evaluator feedback: a claim sourced from an official ATS platform
    (only accepted when linked from the entity's own official site -- see
    verify.py) must preserve that link chain in the submitted evidence."""
    profile = make_profile(
        org_number="923609016",
        hiring_signals=[
            available_claim(
                "Backend Engineer (posted 2026-06-20)",
                source="https://boards.greenhouse.io/equinor",
                source_class="external",
                linked_from="https://www.equinor.com/careers",
            )
        ],
    )

    envelope = to_envelope(profile, run_id="run-1")

    hiring_claim = next(c for c in envelope["claims"] if c["field"] == "hiring_signal")
    evidence = next(e for e in envelope["evidence"] if e["id"] == hiring_claim["evidence_ids"][0])
    assert evidence["linked_from"] == "https://www.equinor.com/careers"


def test_operations_defaults_when_not_provided():
    profile = make_profile(org_number="923609016")
    envelope = to_envelope(profile, run_id="run-1")
    assert envelope["operations"] == {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0}
