"""Every quoted span is text found verbatim in the source it cites.

Official C300 feedback: the strictest miss type was "a claim was present but lacked a clear
supporting receipt". An audit of 100 companies found 115 of 4,430 spans that were not in their
source as written: text joined across HTML tags, WordPress titles decoded from escaped JSON, a
role card's title read across several elements, and social links quoted in cleaned form.
"""
import json
from datetime import datetime, timezone

from src.models.profile import EvidenceState
from src.pipeline.careers import careers_page_facts
from src.pipeline.crawl import DEFAULT_ATS_DOMAINS, FetchedPage
from src.pipeline.extract import extract
from src.pipeline.keyless_sites import SiteHints, contact_span
from src.pipeline.news import wp_post_facts
from src.pipeline.site_evidence import org_number_span

NOW = datetime(2026, 10, 17, tzinfo=timezone.utc)


def test_an_org_number_split_across_tags_is_quoted_from_its_own_text_node():
    page = '<footer><span>Personvern og informasjonskapsler</span> <b>Organisasjonsnummer</b> <span>999 296 432</span></footer>'

    span = org_number_span(page, "999296432")

    assert span == "999 296 432" and span in page


def test_a_registered_phone_or_street_is_quoted_from_its_own_text_node():
    page = '<p>Ytre Laksevåg 49 | 5132 Nyborg | Tel: <a href="tel:55950730">55 95 07 30</a></p>'
    street = '<div><strong>Nordic Door AS</strong><span>Travveien 3</span><span>4580 Lyngdal</span></div>'

    by_phone = contact_span(page, SiteHints(phones=["55950730"]))
    by_street = contact_span(street, SiteHints(street="Travveien 3", postcode="4580", poststed="LYNGDAL"))

    assert by_phone == "55 95 07 30" and by_phone in page
    assert by_street == "Travveien 3" and by_street in street


def test_a_wordpress_title_is_quoted_as_the_json_writes_it():
    posts = [{"date": "2026-01-07T09:00:00", "title": {"rendered": "Fikk toppscore på alle kriterier – 1/2"}, "link": "https://consto.no/a"}]
    body = json.dumps(posts).replace("/", "\\/")  # WordPress: ASCII escapes and \/

    [fact] = wp_post_facts(body, "https://consto.no/wp-json/wp/v2/posts", NOW)

    assert fact.value == "Fikk toppscore på alle kriterier – 1/2 (2026-01-07)"
    assert fact.evidence_span in body


def test_a_role_card_title_is_quoted_from_one_of_its_own_text_nodes():
    page = (
        '<a href="/karriere/ledige-stillinger/planleggingsleder-12345"><h3>Planleggingsleder</h3>'
        "<span>Råde</span><span>Bygg, AF Bygg Øst</span><span>14.11.2026</span></a>"
        '<a href="/karriere/ledige-stillinger/prosjektokonom-67890"><h3>Prosjektøkonom</h3><span>Trondheim</span></a>'
    )
    html = f"<html><head><title>Ledige stillinger</title></head><body><h1>Ledige stillinger</h1>{page}</body></html>"

    facts = careers_page_facts(html, "https://www.afgruppen.no/karriere/ledige-stillinger/", NOW, ats_domains=DEFAULT_ATS_DOMAINS)

    assert facts and all(f.evidence_span in html for f in facts)


def test_a_social_link_is_quoted_as_the_page_writes_it():
    html = '<html><body><a href="https://www.youtube.com/user/energisenteret?sub_confirmation=1">YouTube</a></body></html>'
    page = FetchedPage(url="https://www.eis.no/", raw_html=html, fetched_at=NOW, fetch_state=EvidenceState.AVAILABLE)

    [fact] = [f for f in extract(page) if f.field_name == "company_profile"]

    assert fact.value == "https://www.youtube.com/user/energisenteret"
    assert fact.evidence_span in html
