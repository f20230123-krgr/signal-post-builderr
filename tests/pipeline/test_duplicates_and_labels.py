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


def _coop(site):
    return ResolvedEntity(
        org_number="971339071", legal_name="SAMEIET ST OLAV", registered_address="Olavs gate 1, 0165 OSLO",
        official_site_candidate=site, resolution_state=EvidenceState.AVAILABLE,
        source="https://data.brreg.no/enhetsregisteret/api/enheter/971339071", retrieved_at=NOW, content_hash="h",
    )


def test_a_housing_coops_registered_manager_website_is_ambiguous_not_its_own():
    """SAMEIET ST OLAV registers bate.no, its property manager's site: another company's
    website must not be published under the co-op's profile."""
    manager = assemble(_coop("https://www.bate.no"), [], previous_snapshot=None, now=lambda: NOW)
    own = assemble(_coop("https://st-olav.no"), [], previous_snapshot=None, now=lambda: NOW)

    assert manager.online_presence.official_site.state == EvidenceState.AMBIGUOUS
    assert manager.online_presence.official_site.value is None
    assert own.online_presence.official_site.value == "https://st-olav.no"


def test_a_coop_whose_manager_site_names_it_keeps_the_website():
    evidence = _fact("official_site_evidence", "https://www.bate.no/st-olav")
    profile = assemble(_coop("https://www.bate.no"), [evidence], previous_snapshot=None, now=lambda: NOW)

    assert profile.online_presence.official_site.value == "https://www.bate.no/st-olav"


def test_the_registry_listing_the_current_name_as_a_former_one_is_not_a_renaming():
    record = {
        "organisasjonsnummer": "971339071", "navn": "SAMEIET ST OLAV",
        "historiskeNavn": [
            {"navn": "SAMEIET ST OLAV", "fraDato": "1996-03-13", "tilDato": "1996-04-23"},
            {"navn": "SAMEIET ST OLAV V/ FORR.FØRER", "fraDato": "1990-01-01", "tilDato": "1996-03-13"},
        ],
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=record)))

    details = fetch_live_registry_details(_coop("https://st-olav.no"), BudgetGovernor(), client=client, now=lambda: NOW)

    renames = [f.value for f in details.facts if f.value.startswith("Renamed from")]
    assert renames == ["Renamed from 'SAMEIET ST OLAV V/ FORR.FØRER' on 1996-03-13 - Brønnøysundregistrene"]


def test_feed_and_page_headlines_read_as_plain_text_with_the_raw_text_as_span():
    from src.pipeline.extract import feed_activity_facts

    feed = """<rss><channel><item><title>World&amp;#8217;s largest Pio installation</title>
    <pubDate>Thu, 18 Sep 2026 08:00:00 GMT</pubDate></item></channel></rss>"""

    [fact] = feed_activity_facts(feed, "https://www.strongpoint.com/news/feed/", NOW)

    assert fact.value == "World\u2019s largest Pio installation (2026-09-18)"
    assert "&amp;#8217;" in fact.evidence_span


def test_a_dated_line_on_a_page_reads_headline_then_date_in_plain_text():
    html = "<html><body><p>2026-09-15 Building Healthcare Solutions Together: Kitron &amp; CellaVision</p></body></html>"
    page = FetchedPage(url="https://www.kitron.com", raw_html=html, fetched_at=NOW, fetch_state=EvidenceState.AVAILABLE)

    [fact] = [f for f in extract(page) if f.field_name == "dated_activity" and f.extraction_method == "text"]

    assert fact.value == "Building Healthcare Solutions Together: Kitron & CellaVision (2026-09-15)"
    assert fact.effective_date == "2026-09-15"
    assert fact.evidence_span and fact.evidence_span.startswith("2026-09-15 Building")


def test_the_latest_accounts_are_the_companys_own_and_group_accounts_are_labelled():
    """EQUINOR ASA files its own and its group's accounts for each year. The 'latest' claim was
    the group's (revenue 106.5 bn) while every discrete figure was the company's own (68.0 bn)."""
    from src.pipeline.registry_extras import fetch_registry_extras

    def filing(kind, revenue, year):
        return {
            "regnskapstype": kind, "valuta": "USD",
            "regnskapsperiode": {"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"},
            "resultatregnskapResultat": {"aarsresultat": 1.0, "driftsresultat": {"driftsinntekter": {"sumDriftsinntekter": revenue}}},
        }

    body = [filing("KONSERN", 106462000000.0, 2025), filing("SELSKAP", 67956000000.0, 2025), filing("SELSKAP", 72000000000.0, 2024)]

    def handler(request):
        if "regnskapsregisteret" in str(request.url):
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    facts, _ = fetch_registry_extras(_entity(), BudgetGovernor(), client=httpx.Client(transport=httpx.MockTransport(handler)))

    [latest] = [f for f in facts if f.field_name == "annual_accounts_latest"]
    history = [f.value for f in facts if f.field_name == "annual_accounts_history"]
    assert "67,956,000,000" in latest.value and latest.reporting_period == "FY2025"
    assert any(v.startswith("Group accounts (konsern): ") and "106,462,000,000" in v for v in history)
