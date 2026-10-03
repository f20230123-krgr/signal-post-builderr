"""The live registry record's `hjemmeside`: Builderr's universe snapshot lacks it for most
companies that have one (65 of 78 notable companies without one in the snapshot had it
live, 59 matching Builderr's own crawl), so it is read from the record we fetch anyway."""
import json
from datetime import datetime, timezone

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor
from src.pipeline.assemble import assemble
from src.pipeline.registry_extras import _registered_website, fetch_live_registry_details
from tests.pipeline.test_crawl import _entity as site_entity
from tests.pipeline.test_registry_extras import _entity

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _details(record: dict):
    text = json.dumps(record)

    def handler(request):
        return httpx.Response(200, text=text) if "/enheter/" in str(request.url) else httpx.Response(404)

    return fetch_live_registry_details(_entity(), BudgetGovernor(), client=httpx.Client(transport=httpx.MockTransport(handler))), text


def test_the_registered_website_is_read_from_the_live_record_with_its_own_text_as_the_span():
    details, text = _details({"organisasjonsnummer": "997770234", "navn": "X", "hjemmeside": "www.tomra.com"})

    fact = details.website
    assert fact.field_name == "official_site_registry" and fact.value == "https://www.tomra.com"
    assert fact.source_class == "official_registry" and fact.source_url.endswith("/enheter/997770234")
    assert fact.evidence_span == '"hjemmeside": "www.tomra.com"' and fact.evidence_span in text


def test_an_address_with_a_scheme_is_kept_and_an_absent_one_gives_nothing():
    assert _details({"hjemmeside": "http://abax.com/en"})[0].website.value == "http://abax.com/en"
    assert _details({"navn": "X"})[0].website is None


def test_values_that_are_not_a_company_website_are_ignored():
    for junk in ("", "-", "ingen", "post@firma.no", "www.facebook.com/firma", "https://www.linkedin.com/company/x", "no website yet", None, 5):
        assert _registered_website(junk) is None, junk
    assert _registered_website("firma.no") == "https://firma.no"


def test_assembly_cites_a_promoted_live_website_to_the_live_record_not_the_snapshot():
    details, _ = _details({"hjemmeside": "www.tomra.com"})
    promoted = site_entity("https://www.tomra.com")

    site = assemble(promoted, [details.website], None, now=lambda: NOW).online_presence.official_site

    assert site.value == "https://www.tomra.com" and site.state == EvidenceState.AVAILABLE
    assert site.source.endswith("/enheter/997770234") and site.evidence_span == '"hjemmeside": "www.tomra.com"'


def test_a_site_page_still_outranks_the_registry_record_as_evidence():
    from src.pipeline.verify import ConfirmedFact

    details, _ = _details({"hjemmeside": "www.tomra.com"})
    page = ConfirmedFact("official_site_evidence", "https://www.tomra.com/en", "https://www.tomra.com/", 100.0, NOW,
                         content_hash="a" * 64, extraction_method="text", source_class="company_owned", evidence_span="TOMRA Systems ASA")

    site = assemble(site_entity("https://www.tomra.com"), [details.website, page], None, now=lambda: NOW).online_presence.official_site

    assert site.source_class == "company_owned" and site.evidence_span == "TOMRA Systems ASA"
