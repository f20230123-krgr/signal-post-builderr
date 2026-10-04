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


def test_report_includes_the_dated_summary():
    profile = make_profile(legal_name=available_claim("EQUINOR ASA"))

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert "As of " in html and "EQUINOR ASA (org. no." in html


def test_report_handles_empty_profile_list_without_crashing():
    html = render_html_report([], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert "<html" in html.lower()


def test_report_escapes_html_special_characters_in_claim_values():
    profile = make_profile(legal_name=available_claim("<script>alert(1)</script> AS"))

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_a_sourced_summary_links_to_its_source_for_verification():
    """Usability bar: "a user should be able to find, compare and verify
    company information on desktop and mobile." A sourced fact must carry
    a clickable link to that source, not just prose (here: the no-JavaScript
    fallback; the interactive viewer opens the same link in its evidence panel)."""
    profile = make_profile(
        legal_name=available_claim("EQUINOR ASA", source="https://data.brreg.no/enhetsregisteret/api/enheter/923609016"),
    )

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert 'href="https://data.brreg.no/enhetsregisteret/api/enheter/923609016"' in html


def test_a_company_with_no_source_shows_no_broken_link():
    profile = make_profile()  # everything NOT_AVAILABLE, no sources

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert 'href=""' not in html


def test_the_viewer_embeds_the_submitted_envelopes_verbatim():
    """The page is a viewer of what is submitted, not a second rendering of it."""
    import json
    import re

    from src.pipeline.envelope import to_envelope

    profile = make_profile(org_number="923609016", legal_name=available_claim("EQUINOR ASA"))
    envelope = to_envelope(profile, run_id="r")

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc), envelopes=[envelope])

    payload = re.search(r'<script type="application/json" id="atlas-data">(.*?)</script>', html, re.S).group(1)
    data = json.loads(payload)
    assert data["envelopes"][0]["organisation_number"] == "923609016"
    embedded = data["envelopes"][0]["summary"]
    assert " ".join(x["text"] for x in embedded["sentences"]) == envelope["summary"]["text"]
    assert embedded["sentences"] == envelope["summary"]["sentences"]
    assert data["envelopes"][0]["claims"] == [
        {k: v for k, v in c.items() if v is not None} for c in envelope["claims"]
    ]


def test_embedded_data_cannot_close_the_script_element():
    profile = make_profile(legal_name=available_claim("</script><b>x</b> AS"))

    html = render_html_report([profile], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert html.count("</script>") == 3  # the error handler, the data block and the app script only


def test_the_viewer_offers_find_compare_and_verify():
    html = render_html_report([make_profile()], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    for needle in ('id="q"', 'id="filters"', 'id="cmpGo"', 'id="drawer"', "Evidence", 'role="group"', 'data-theme="light"'):
        assert needle in html


def test_the_viewer_opens_in_dark_mode_by_default_and_light_is_an_explicit_choice():
    html = render_html_report([make_profile()], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    base = html.index(":root {")
    light = html.index(':root[data-theme="light"]')
    assert base < light
    assert "--bg: #0b171e" in html[base:light]  # the bare :root is the dark palette
    assert "prefers-color-scheme" not in html  # no automatic switch to light
    assert 'data-theme="light"' in html


def test_the_hosted_site_is_a_small_page_plus_a_separate_data_file(tmp_path):
    import json

    from src.pipeline.envelope import to_envelope
    from src.reporting import write_site

    profiles = [make_profile(org_number=f"92360901{i}", legal_name=available_claim(f"ACME {i} AS")) for i in range(5)]
    envelopes = [to_envelope(p, run_id="r") for p in profiles]

    stats = write_site(tmp_path, envelopes, datetime(2026, 1, 1, tzinfo=timezone.utc), fallback_limit=2)

    html = (tmp_path / "index.html").read_text(encoding="utf-8")
    data = json.loads((tmp_path / "data.json").read_text(encoding="utf-8"))
    assert stats["companies"] == 5 and len(data["envelopes"]) == 5
    assert 'id="atlas-data"></script>' in html  # nothing embedded: the page fetches data.json
    assert "ACME 0 AS" in html and "ACME 3 AS" not in html  # the no-JavaScript fallback is capped
    assert "Showing the first 2 of 5" in html
    assert stats["index_bytes"] < stats["data_bytes"] or stats["index_bytes"] < 200_000


def test_the_single_file_report_still_embeds_everything_for_offline_use():
    html = render_html_report([make_profile(legal_name=available_claim("ACME AS"))], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert '"organisation_number"' in html.split('id="atlas-data">')[1].split("</script>")[0]
