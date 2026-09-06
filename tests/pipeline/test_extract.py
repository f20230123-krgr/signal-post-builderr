"""Tests for src/pipeline/extract.py -- see docs/component-specs.md."""
from datetime import datetime, timezone
from pathlib import Path

from src.pipeline.crawl import FetchedPage
from src.pipeline.extract import extract
from src.models.profile import EvidenceState

PAGES = Path(__file__).parent.parent.parent / "fixtures" / "pages"


def _page(name: str, org_number: str, url: str = "https://example.com/") -> FetchedPage:
    html = (PAGES / org_number / name).read_text(encoding="utf-8")
    return FetchedPage(url=url, raw_html=html, fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), fetch_state=EvidenceState.AVAILABLE)


def test_uses_structured_path_when_json_ld_present():
    page = _page("official_site.html", "923609016")
    facts = extract(page)

    by_field = {f.field_name: f for f in facts if f.field_name in {"organization_name", "official_site", "registered_address"}}
    assert by_field["organization_name"].value == "Equinor ASA"
    assert by_field["organization_name"].extraction_method == "structured"
    assert by_field["official_site"].value == "https://www.equinor.com"
    assert "Stavanger" in by_field["registered_address"].value

    leaders = [f for f in facts if f.field_name == "leader"]
    assert any("Anders Opedal" in f.value for f in leaders)

    profiles = [f for f in facts if f.field_name == "company_profile"]
    assert any("linkedin.com/company/equinor" in f.value for f in profiles)

    # source policy: every claim must trace back to a content hash
    assert all(f.content_hash for f in facts)
    assert len({f.content_hash for f in facts}) == 1  # same page -> same hash

    # Careers section isn't in the JSON-LD -- activity scanning must still run.
    dated = [f for f in facts if f.field_name == "dated_activity"]
    assert any("Reservoir Engineer" in f.value for f in dated)

    # Structured data was found for identity fields -- no need for a raw
    # page_text fallback fact.
    assert not any(f.field_name == "page_text" for f in facts)


def test_falls_back_to_text_extraction_without_json_ld():
    page = _page("official_site.html", "997770234")
    facts = extract(page)

    page_text = [f for f in facts if f.field_name == "page_text"]
    assert len(page_text) == 1
    assert page_text[0].extraction_method == "text"
    assert "Kahoot" in page_text[0].value

    hiring = [f for f in facts if f.field_name == "hiring_signal"]
    assert any("Backend Engineer" in f.value for f in hiring)


def test_malformed_html_does_not_crash_stage():
    page = _page("official_site.html", "991753591")
    facts = extract(page)

    assert isinstance(facts, list)
    assert any("Gelato" in f.value for f in facts)


def test_never_invents_a_field_absent_from_the_page():
    page = _page("official_site.html", "997770234")  # no JSON-LD address/leader data
    facts = extract(page)

    assert not any(f.field_name in {"official_site", "registered_address", "leader"} for f in facts)
