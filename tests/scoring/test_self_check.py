"""Tests for src/scoring/self_check.py -- see docs/component-specs.md."""
import json

from src.scoring.self_check import run_self_check
from tests.conftest import available_claim, make_profile


def _write_label(fixtures_dir, org_number, expected_legal_name, expected_fields=None):
    expected_dir = fixtures_dir / "expected"
    expected_dir.mkdir(parents=True, exist_ok=True)
    (expected_dir / f"{org_number}.json").write_text(
        json.dumps(
            {
                "org_number": org_number,
                "expected_legal_name": expected_legal_name,
                "expected_fields": expected_fields or {},
            }
        ),
        encoding="utf-8",
    )


def test_known_good_fixture_scores_full_marks(tmp_path):
    _write_label(
        tmp_path,
        "923609016",
        "EQUINOR ASA",
        expected_fields={"official_site": "equinor.com", "leader": "Anders Opedal"},
    )

    def process_company(org_number):
        return make_profile(
            org_number=org_number,
            legal_name=available_claim("EQUINOR ASA"),
            official_site=available_claim("https://www.equinor.com"),
            leaders=[available_claim("Anders Opedal (President and CEO)")],
        )

    report = run_self_check(tmp_path, process_company=process_company)

    assert report.weighted_company_recall == 100.0
    assert report.external_precision == 100.0
    assert report.coverage_score == 35.0
    assert report.passes_hard_gates() is True


def test_known_bad_fixture_wrong_company_tanks_precision(tmp_path):
    _write_label(tmp_path, "923609016", "EQUINOR ASA", expected_fields={"official_site": "equinor.com"})

    def process_company(org_number):
        # Published a claim, but for the wrong company entirely.
        return make_profile(
            org_number=org_number,
            legal_name=available_claim("TOTALLY DIFFERENT COMPANY AS"),
            official_site=available_claim("https://www.totally-different.example"),
        )

    report = run_self_check(tmp_path, process_company=process_company)

    assert report.external_precision == 0.0
    assert report.weighted_company_recall == 0.0
    assert report.passes_hard_gates() is False


def test_no_publication_does_not_penalize_precision(tmp_path):
    _write_label(tmp_path, "923609016", "EQUINOR ASA")

    def process_company(org_number):
        return make_profile(org_number=org_number)  # everything NOT_AVAILABLE

    report = run_self_check(tmp_path, process_company=process_company)

    assert report.external_precision == 100.0
    assert report.weighted_company_recall == 0.0


def test_partial_claim_recall_lowers_coverage_but_not_recall(tmp_path):
    _write_label(
        tmp_path,
        "923609016",
        "EQUINOR ASA",
        expected_fields={"official_site": "equinor.com", "leader": "Anders Opedal"},
    )

    def process_company(org_number):
        return make_profile(
            org_number=org_number,
            legal_name=available_claim("EQUINOR ASA"),
            # official_site matches, but the expected leader is missing.
            official_site=available_claim("https://www.equinor.com"),
        )

    report = run_self_check(tmp_path, process_company=process_company)

    assert report.weighted_company_recall == 100.0
    assert 21.0 < report.coverage_score < 35.0
