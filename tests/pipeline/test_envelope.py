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


def test_identical_claims_from_the_same_page_share_one_evidence_entry():
    """Two claims with the same value, source and content share one evidence
    entry (the same page, the same span) rather than repeating it."""
    same = dict(
        source="https://www.equinor.com/about", content_hash="page-hash",
        source_class="company_owned", extraction_method="text", match_confidence=95.0,
    )
    profile = make_profile(
        org_number="923609016",
        hiring_signals=[available_claim("Backend Engineer", **same)],
        dated_activity=[available_claim("Backend Engineer", **same)],
    )

    envelope = to_envelope(profile, run_id="run-1")

    assert len(envelope["evidence"]) == 1


def test_different_claims_from_the_same_page_each_carry_their_own_evidence_span():
    """Evidence-span validity: a claim's span must be the text that supports
    THAT claim. Sharing one entry between two different claims gave the second
    claim the first one's span."""
    same = dict(source="https://www.equinor.com/about", content_hash="page-hash", source_class="company_owned")
    profile = make_profile(
        org_number="923609016",
        hiring_signals=[available_claim("Hiring: Backend Engineer", **same)],
        dated_activity=[available_claim("2026-08-01: new office opened", **same)],
    )

    envelope = to_envelope(profile, run_id="run-1")

    hiring = next(c for c in envelope["claims"] if c["field"] == "hiring_signal")
    dated = next(c for c in envelope["claims"] if c["field"] == "dated_activity")
    assert hiring["evidence_ids"] != dated["evidence_ids"]
    spans = {e["id"]: e["claim_span"] for e in envelope["evidence"]}
    assert spans[hiring["evidence_ids"][0]] == "Hiring: Backend Engineer"
    assert spans[dated["evidence_ids"][0]] == "2026-08-01: new office opened"


def test_the_reporting_period_and_effective_date_are_exported_on_claim_and_evidence():
    """Builderr's rule: "keep the source, retrieval date and relevant reporting
    period for every claim." The period used to live only in our internal model
    and never reached the submitted envelope."""
    profile = make_profile(
        annual_latest=available_claim(
            "Revenue: 914,000,000 NOK; Net result: 853,000,000 NOK",
            source="https://data.brreg.no/regnskapsregisteret/regnskap/938702675",
            reporting_period="FY2025", effective_date="2025-12-31",
        ),
    )

    envelope = to_envelope(profile, run_id="run-1")

    claim = next(c for c in envelope["claims"] if c["field"] == "annual_accounts_latest")
    assert claim["reporting_period"] == "FY2025"
    assert claim["effective_at"] == "2025-12-31"
    evidence = next(e for e in envelope["evidence"] if e["id"] == claim["evidence_ids"][0])
    assert evidence["reporting_period"] == "FY2025"
    assert evidence["effective_at"] == "2025-12-31"


def test_a_claim_with_no_period_exports_explicit_nulls():
    envelope = to_envelope(make_profile(legal_name=available_claim("EQUINOR ASA")), run_id="run-1")

    claim = next(c for c in envelope["claims"] if c["field"] == "legal_name")
    assert claim["reporting_period"] is None and claim["effective_at"] is None


def test_the_evidence_span_is_the_verbatim_excerpt_when_the_claim_has_one():
    """The value we publish is a formatted string ("Anders Opedal (CEO)") that
    does not appear verbatim in the registry JSON; the span must be text a
    checker can find in the captured source."""
    excerpt = '"fornavn":"Anders","etternavn":"Opedal"'
    profile = make_profile(
        leaders=[available_claim("Anders Opedal (Daglig leder)", source="https://data.brreg.no/x", evidence_span=excerpt)]
    )

    envelope = to_envelope(profile, run_id="run-1")

    claim = next(c for c in envelope["claims"] if c["field"] == "leader")
    evidence = next(e for e in envelope["evidence"] if e["id"] == claim["evidence_ids"][0])
    assert evidence["claim_span"] == excerpt


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


def test_envelope_embeds_one_dated_summary_not_a_question_list():
    """The submitted artifact is envelopes.jsonl, so the synthesis has to be in
    it. Official feedback asked for "a shorter dated summary instead of a
    list", so the envelope carries one narrative, not the Q&A list."""
    profile = make_profile(
        org_number="923609016",
        legal_name=available_claim("EQUINOR ASA"),
        official_site=available_claim("https://www.equinor.com"),
    )

    envelope = to_envelope(profile, run_id="run-1")

    assert "answers" not in envelope
    summary = envelope["summary"]
    assert summary["as_of"] == profile.run_timestamp.date().isoformat()
    assert summary["text"].startswith(f"As of {summary['as_of']}: EQUINOR ASA")
    assert "equinor.com" in summary["text"]


def test_each_summary_sentence_points_at_the_evidence_it_rests_on():
    profile = make_profile(
        org_number="923609016",
        legal_name=available_claim("EQUINOR ASA", source="https://data.brreg.no/enhet/923609016"),
        official_site=available_claim("https://www.equinor.com", source="https://www.equinor.com/"),
    )

    envelope = to_envelope(profile, run_id="run-1")

    evidence_by_id = {e["id"]: e for e in envelope["evidence"]}
    cited = {i for s in envelope["summary"]["sentences"] for i in s["evidence_ids"]}
    assert cited and cited <= set(evidence_by_id)
    identity = envelope["summary"]["sentences"][0]
    assert any(evidence_by_id[i]["source_url"] == "https://data.brreg.no/enhet/923609016" for i in identity["evidence_ids"])
