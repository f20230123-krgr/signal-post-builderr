"""
Tests for src/reporting.py.

Evaluator feedback: "a usable desktop/mobile results view." Static HTML, no
server, no JS framework -- native <details>/<summary> for per-company
expansion and a CSS media query for the mobile breakpoint, per
docs/success-criteria.md's UX bar ("legible/reviewable on desktop and
mobile").
"""
from datetime import datetime, timezone

from src.models.profile import EvidenceState
from src.reporting import render_html_report
from tests.conftest import available_claim, make_profile


def test_report_is_responsive_html():
    html = render_html_report([make_profile()], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert "<meta name=\"viewport\"" in html
    assert "@media" in html  # a mobile breakpoint exists


def test_report_includes_org_number_and_legal_name_for_each_company():
    profiles = [
        make_profile(org_number="923609016", legal_name=available_claim("EQUINOR ASA")),
        make_profile(org_number="997770234", legal_name=available_claim("KAHOOT! AS")),
    ]

    html = render_html_report(profiles, generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert "923609016" in html
    assert "EQUINOR ASA" in html
    assert "997770234" in html
    assert "KAHOOT! AS" in html


def test_report_shows_not_available_honestly_never_blank_or_fabricated():
    profile = make_profile(org_number="000000000")  # everything NOT_AVAILABLE

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert "not available" in html.lower() or "not_available" in html.lower()


def test_report_includes_synthesis_answers():
    profile = make_profile(legal_name=available_claim("EQUINOR ASA"))

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert "What is the company" in html and "legal name and brand?" in html


def test_report_handles_empty_profile_list_without_crashing():
    html = render_html_report([], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "<html" in html.lower()


def test_report_escapes_html_special_characters_in_claim_values():
    profile = make_profile(legal_name=available_claim("<script>alert(1)</script> AS"))

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
