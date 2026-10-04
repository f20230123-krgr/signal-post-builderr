"""Tests for src/pipeline/site_evidence.py -- the official-website claim backed by
the company's own pages instead of a registry data file."""
from datetime import datetime, timezone

from src.models.profile import EvidenceState
from src.pipeline.assemble import assemble
from src.pipeline.crawl import FetchedPage
from src.pipeline.site_evidence import FIELD_NAME, name_span, official_site_evidence, org_number_span
from src.pipeline.verify import ConfirmedFact
from tests.pipeline.test_crawl import _entity

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _page(url, body="", title="Equinor ASA", final_url=None, linked_from=None, canonical=None):
    head = f"<title>{title}</title>" + (f'<link rel="canonical" href="{canonical}">' if canonical else "")
    return FetchedPage(url, f"<html><head>{head}</head><body>{body}</body></html>", NOW, EvidenceState.AVAILABLE, linked_from, final_url)


# --- the quoted span ---------------------------------------------------------


def test_the_org_number_span_quotes_the_page_as_written_in_any_format():
    for written in ("923609016", "923 609 016", "923.609.016"):
        html = f"<footer><p>EQUINOR ASA, Org.nr. {written} MVA</p></footer>"

        span = org_number_span(html, "923609016")

        assert span is not None and written in span
        assert span in "EQUINOR ASA, Org.nr. " + written + " MVA"


def test_another_organisations_number_is_not_a_match():
    assert org_number_span("<p>Org.nr 111 222 333</p>", "923609016") is None
    assert org_number_span("<p>Telefon 9236090160</p>", "923609016") is None  # part of a longer number


def test_the_name_span_prefers_the_page_name_closest_to_the_legal_name():
    html = '<html><head><title>Welcome</title><meta property="og:site_name" content="Equinor"></head></html>'

    assert name_span(html, "EQUINOR ASA") == "Equinor"
    assert name_span("<html><body>nothing</body></html>", "EQUINOR ASA") is None


def test_a_slogan_that_does_not_name_the_company_is_not_evidence_of_it():
    """A housing co-op registered at its property manager's site: the manager's
    title says nothing about the co-op, so the registry claim is kept."""
    html = "<html><head><title>Forretningsfører for sameie og borettslag | Bytt enkelt og trygt</title></head></html>"

    assert name_span(html, "HAUGE BYGÅRD") is None
    assert official_site_evidence([_page("https://example.com/", title="Forretningsfører for sameie og borettslag")], _entity("https://example.com/")) is None


# --- the fact ------------------------------------------------------------------


def test_the_website_is_evidenced_by_the_page_that_states_the_org_number():
    pages = [
        _page("https://example.com/", title="Home"),
        _page("https://example.com/kontakt", body="<p>EQUINOR ASA - org.nr 923 609 016</p>"),
    ]

    fact = official_site_evidence(pages, _entity("https://example.com/"))

    assert fact.field_name == FIELD_NAME
    assert fact.value == "https://example.com/"
    assert fact.source_url == "https://example.com/kontakt"
    assert fact.source_class == "company_owned"
    assert "923 609 016" in fact.evidence_span
    assert len(fact.content_hash) == 64


def test_without_an_org_number_the_pages_own_name_for_the_company_is_the_span():
    fact = official_site_evidence([_page("https://example.com/", title="Equinor ASA - Energy")], _entity("https://example.com/"))

    assert fact.evidence_span == "Equinor ASA - Energy"
    assert fact.source_url == "https://example.com/"


def test_the_value_is_where_the_site_resolves_not_the_bare_registry_string():
    """abax.com redirects to abax.com/en-gb; that is the site's address."""
    redirected = official_site_evidence([_page("https://abax.com/", final_url="https://abax.com/en-gb")], _entity("https://abax.com/"))
    canonical = official_site_evidence(
        [_page("https://example.com/", canonical="https://example.com/no/")], _entity("https://example.com/")
    )

    assert redirected.value == "https://abax.com/en-gb"
    assert canonical.value == "https://example.com/no/"


def test_a_canonical_link_to_another_domain_is_ignored():
    fact = official_site_evidence(
        [_page("https://example.com/", canonical="https://evil.test/")], _entity("https://example.com/")
    )

    assert fact.value == "https://example.com/"


def test_a_registered_address_that_moved_to_another_domain_is_published_at_its_new_address_when_the_page_names_the_company():
    """lundbeck.no redirects to lundbeck.com; Builderr's own crawl records the final address."""
    fact = official_site_evidence(
        [_page("https://example.com/", title="Equinor ASA", final_url="https://equinor-group.com/en")], _entity("https://example.com/")
    )

    assert fact.value == "https://equinor-group.com/en"
    assert fact.source_url == "https://equinor-group.com/en" and fact.evidence_span == "Equinor ASA"


def test_a_moved_site_whose_landing_page_does_not_name_the_company_gets_no_site_evidence():
    """A parent group's homepage is not proof that the subsidiary's site moved there."""
    page = _page("https://example.com/", title="Parent Group - Energy", final_url="https://parent-group.com/")

    assert official_site_evidence([page], _entity("https://example.com/")) is None


def test_a_moved_site_is_proven_by_the_org_number_on_the_landing_page_even_if_the_name_differs():
    page = _page("https://example.com/", body="<footer>Parent Group, org.nr 923 609 016</footer>", title="Welcome", final_url="https://parent-group.com/")

    fact = official_site_evidence([page], _entity("https://example.com/"))

    assert fact is not None and "923 609 016" in fact.evidence_span


def test_no_fetched_page_no_title_or_a_page_from_a_link_chain_gives_nothing():
    entity = _entity("https://example.com/")
    failed = FetchedPage("https://example.com/", "", NOW, EvidenceState.FAILED)
    untitled = FetchedPage("https://example.com/", "<html><body>hello</body></html>", NOW, EvidenceState.AVAILABLE)
    chained = _page("https://example.com/", linked_from="https://other.com/")

    assert official_site_evidence([], entity) is None
    assert official_site_evidence([failed], entity) is None
    assert official_site_evidence([untitled], entity) is None
    assert official_site_evidence([chained], entity) is None


def test_a_company_without_a_registered_site_has_no_site_evidence():
    assert official_site_evidence([_page("https://example.com/")], _entity(None)) is None


# --- assembly --------------------------------------------------------------------


def _fact(value="https://example.com/en", span="EQUINOR ASA, Org.nr. 923 609 016"):
    return ConfirmedFact(
        FIELD_NAME, value, "https://example.com/kontakt", 100.0, NOW,
        content_hash="a" * 64, extraction_method="text", source_class="company_owned", evidence_span=span,
    )


def test_assembly_prefers_site_evidence_over_the_registry_string():
    profile = assemble(_entity("https://example.com/"), [_fact()], None, now=lambda: NOW)

    site = profile.online_presence.official_site
    assert site.value == "https://example.com/en"
    assert site.source == "https://example.com/kontakt"
    assert site.source_class == "company_owned"
    assert site.evidence_span == "EQUINOR ASA, Org.nr. 923 609 016"


def test_assembly_falls_back_to_the_registry_claim_when_the_site_gave_no_evidence():
    site = assemble(_entity("https://example.com/"), [], None, now=lambda: NOW).online_presence.official_site

    assert site.value == "https://example.com/"
    assert site.source_class == "official_registry"


def test_the_envelope_cites_the_site_page_and_quotes_the_span():
    from src.pipeline.envelope import to_envelope

    envelope = to_envelope(assemble(_entity("https://example.com/"), [_fact()], None, now=lambda: NOW), run_id="r")

    claim = next(c for c in envelope["claims"] if c["field"] == "official_website")
    evidence = next(e for e in envelope["evidence"] if e["id"] == claim["evidence_ids"][0])
    assert evidence["source_url"] == "https://example.com/kontakt"
    assert evidence["claim_span"] == "EQUINOR ASA, Org.nr. 923 609 016"
