"""Fixes from checking the agent on the companies named in official feedback (2026-10-06).

On Equinor and Elopak the agent published the same Facebook and LinkedIn page twice (two
spellings of one URL), a careers page twice (its Danish copy), a job title with an HTML
entity left in it ("Finance &amp; Trading"), and a single Workday job ad labelled
"Careers page:" with a stray "?JobPosting" on its URL. The rules ask to "avoid duplicate
records" and to keep claims faithful to the source; each test pins one of those fixes.
"""
from datetime import datetime, timezone

import httpx

from src.orchestrator.budget import BudgetGovernor
from src.pipeline.assemble import assemble
from src.pipeline.careers import careers_page_facts, careers_page_key, clean_page_url
from src.pipeline.crawl import FetchedPage
from src.models.profile import EvidenceState
from src.pipeline.extract import extract
from src.pipeline.registry_extras import fetch_live_registry_details
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import ConfirmedFact

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _entity():
    return ResolvedEntity(
        org_number="923609016",
        legal_name="EQUINOR ASA",
        registered_address="Forusbeen 50, 4035 STAVANGER",
        official_site_candidate="https://www.equinor.com",
        resolution_state=EvidenceState.AVAILABLE,
        source="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=NOW,
        content_hash="entity-hash",
    )


def _fact(field_name, value):
    return ConfirmedFact(
        field_name=field_name, value=value, source_url="https://www.equinor.com/", match_confidence=95.0,
        retrieved_at=NOW, content_hash="h", extraction_method="text", source_class="company_owned",
    )


def test_one_social_profile_linked_in_two_spellings_is_one_claim():
    facts = [
        _fact("company_profile", "https://facebook.com/Equinor"),
        _fact("company_profile", "https://www.facebook.com/Equinor/"),
        _fact("company_profile", "https://linkedin.com/company/equinor"),
        _fact("company_profile", "https://www.linkedin.com/company/equinor/"),
        _fact("company_profile", "https://www.instagram.com/equinor/"),
    ]

    profile = assemble(_entity(), facts, previous_snapshot=None, now=lambda: NOW)

    assert [c.value for c in profile.online_presence.company_profiles] == [
        "https://facebook.com/Equinor", "https://linkedin.com/company/equinor", "https://www.instagram.com/equinor/",
    ]


def test_a_language_copy_of_a_careers_page_is_not_a_second_hiring_signal():
    facts = [
        _fact("hiring_signal", "Careers page: https://www.elopak.com/career/vacancies/"),
        _fact("hiring_signal", "Careers page: https://www.elopak.com/da/career/vacancies/"),
    ]

    profile = assemble(_entity(), facts, previous_snapshot=None, now=lambda: NOW)

    assert [c.value for c in profile.activity.hiring_signals] == ["Careers page: https://www.elopak.com/career/vacancies/"]
    assert careers_page_key("https://elopak.com/en-gb/Career/Vacancies") == careers_page_key("https://www.elopak.com/career/vacancies/")
    assert careers_page_key("https://acme.no/karriere/ledige") != careers_page_key("https://acme.no/karriere")


def test_a_single_job_ad_page_is_never_labelled_a_careers_page():
    ad = """<html><head><title>Summer Internship - Equinor careers</title>
    <script type="application/ld+json">{"@type": "JobPosting", "title": "Summer Internship"}</script></head>
    <body><h1>Careers</h1></body></html>"""
    url = "https://equinor.wd3.myworkdayjobs.com/en-US/EQNR/details/Summer-Internship_JR107237?JobPosting"

    assert careers_page_facts(ad, url, NOW, ats_domains={"myworkdayjobs.com"}) == []


def test_careers_urls_lose_fragments_tracking_and_bare_flags_but_keep_page_ids():
    assert clean_page_url("https://acme.no/karriere?utm_source=x&JobPosting#top") == "https://acme.no/karriere"
    assert clean_page_url("https://acme.no/jobs?id=42&utm_medium=y") == "https://acme.no/jobs?id=42"


def test_a_job_title_with_an_html_entity_reads_as_plain_text_and_keeps_the_page_text_as_span():
    html = """<html><head><script type="application/ld+json">
    {"@type": "JobPosting", "title": "Summer Internship - Finance &amp; Trading", "datePosted": "2026-09-30T08:00:00Z"}
    </script></head><body></body></html>"""
    page = FetchedPage(url="https://www.equinor.com/careers", raw_html=html, fetched_at=NOW, fetch_state=EvidenceState.AVAILABLE)

    [fact] = [f for f in extract(page) if f.field_name == "hiring_signal"]

    assert fact.value == "Summer Internship - Finance & Trading (posted 2026-09-30)"
    assert fact.evidence_span == "Summer Internship - Finance &amp; Trading"
    assert fact.effective_date == "2026-09-30"


def test_the_live_employee_count_is_dated_by_the_registry_and_quoted():
    record = {
        "organisasjonsnummer": "923609016", "navn": "EQUINOR ASA", "antallAnsatte": 201,
        "registreringsdatoAntallAnsatteEnhetsregisteret": "2026-09-14",
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=record)))

    details = fetch_live_registry_details(_entity(), BudgetGovernor(), client=client, now=lambda: NOW)

    [employees] = [f for f in details.identity_facts if f.field_name == "employee_count"]
    assert employees.value == "201"
    assert employees.effective_date == "2026-09-14"
    assert employees.evidence_span and employees.evidence_span.replace(" ", "") == '"antallAnsatte":201'
