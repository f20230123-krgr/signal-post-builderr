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
    assert all(f.context_name == "Equinor ASA" for f in leaders)

    profiles = [f for f in facts if f.field_name == "company_profile"]
    assert any("linkedin.com/company/equinor" in f.value for f in profiles)
    assert all(f.context_name == "Equinor ASA" for f in profiles)

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

    # Real evaluator feedback: a bare careers heading and a "career
    # counselling" service both false-positived on the old keyword scan.
    # Prose mentioning "hiring" is not a job ad -- only structured
    # JobPosting JSON-LD (see test_jobposting_json_ld_becomes_hiring_signal)
    # counts now.
    assert not any(f.field_name == "hiring_signal" for f in facts)


def test_malformed_html_does_not_crash_stage():
    page = _page("official_site.html", "991753591")
    facts = extract(page)

    assert isinstance(facts, list)
    assert any("Gelato" in f.value for f in facts)


def test_never_invents_a_field_absent_from_the_page():
    page = _page("official_site.html", "997770234")  # no JSON-LD address/leader data
    facts = extract(page)

    assert not any(f.field_name in {"official_site", "registered_address", "leader"} for f in facts)


def _inline_page(html: str, url: str = "https://example.com/careers") -> FetchedPage:
    return FetchedPage(url=url, raw_html=html, fetched_at=datetime(2026, 1, 1, tzinfo=timezone.utc), fetch_state=EvidenceState.AVAILABLE)


def test_jobposting_json_ld_becomes_hiring_signal():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Backend Engineer",
     "datePosted":"2026-06-20","hiringOrganization":{"@type":"Organization","name":"Kahoot! AS"}}
    </script></head><body></body></html>"""

    facts = extract(_inline_page(html))

    hiring = [f for f in facts if f.field_name == "hiring_signal"]
    assert len(hiring) == 1
    assert "Backend Engineer" in hiring[0].value
    assert "2026-06-20" in hiring[0].value
    assert hiring[0].extraction_method == "structured"


def test_datepublished_json_ld_becomes_dated_activity():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"NewsArticle","headline":"Company wins award",
     "datePublished":"2026-07-01"}
    </script></head><body></body></html>"""

    facts = extract(_inline_page(html))

    dated = [f for f in facts if f.field_name == "dated_activity"]
    assert len(dated) == 1
    assert "Company wins award" in dated[0].value
    assert "2026-07-01" in dated[0].value
    assert dated[0].extraction_method == "structured"


def test_opengraph_site_name_becomes_public_brand():
    html = '<html><head><meta property="og:site_name" content="Kahoot!"></head><body></body></html>'

    facts = extract(_inline_page(html))

    brand = [f for f in facts if f.field_name == "public_brand"]
    assert len(brand) == 1
    assert brand[0].value == "Kahoot!"
    assert brand[0].extraction_method == "structured"


def test_canonical_link_becomes_official_site():
    html = '<html><head><link rel="canonical" href="https://www.kahoot.com/"></head><body></body></html>'

    facts = extract(_inline_page(html))

    sites = [f for f in facts if f.field_name == "official_site"]
    assert len(sites) == 1
    assert sites[0].value == "https://www.kahoot.com/"


def test_opengraph_and_canonical_absent_when_not_on_page():
    facts = extract(_inline_page("<html><body>plain text, no meta tags here at all</body></html>"))

    assert not any(f.field_name == "public_brand" for f in facts)
    assert not any(f.field_name == "official_site" for f in facts)


def test_graph_wrapped_json_ld_is_unwrapped_before_extraction():
    """Real-world regression, found by independent review: extruct returns a
    "@graph"-wrapped JSON-LD block (a very common real-world pattern, e.g.
    from SEO plugins) as ONE opaque object with keys ["@context", "@graph"]
    -- @type/name/datePublished etc. all live on the objects NESTED inside
    the @graph array, not on the wrapper itself. Without unwrapping it first,
    EVERY structured fact type (organization_name, leader, company_profile,
    hiring_signal, dated_activity) is silently invisible on any page using
    this pattern -- not just a context-metadata nuance, a total blind spot."""
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@graph":[
      {"@type":"Organization","name":"Graph Test AS","sameAs":["https://facebook.com/graphtest"]},
      {"@type":"JobPosting","title":"Backend Engineer","datePosted":"2026-06-20"}
    ]}
    </script></head></html>"""

    facts = extract(_inline_page(html))

    assert any(f.field_name == "organization_name" and f.value == "Graph Test AS" for f in facts)
    assert any(f.field_name == "company_profile" and f.context_name == "Graph Test AS" for f in facts)
    hiring = [f for f in facts if f.field_name == "hiring_signal"]
    assert hiring and "Backend Engineer" in hiring[0].value


def test_page_organization_name_recognizes_a_specialized_business_subtype():
    """Real-world regression, found by independent review: _page_organization_name
    only allow-listed a few schema.org types (Organization/LocalBusiness/
    Corporation), but Norwegian sites commonly use specialized LocalBusiness
    subtypes (AutomotiveBusiness, FinancialService, Store, ...) -- schema.org
    has dozens, an allow-list is the same losing game as a domain blacklist.
    Switched to excluding known non-organization content types instead
    (Article/NewsArticle/JobPosting/WebPage/etc.) -- safe because a missing
    or wrong context_name can only cause verify() to over-reject, never to
    wrongly accept, so broadening this can't create a new precision risk."""
    from src.pipeline.extract import _page_organization_name_from_html

    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"AutomotiveBusiness","name":"Bilverksted AS"}
    </script></head></html>"""

    assert _page_organization_name_from_html(html) == "Bilverksted AS"


def test_page_organization_name_ignores_a_bare_webpage_title():
    """The deny-list must not treat an ordinary WebPage's own title as an
    organization name -- that's exactly the original bug (a page titled
    "Om oss" being mistaken for the site owner's identity)."""
    from src.pipeline.extract import _page_organization_name_from_html

    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"WebPage","name":"Om oss"}
    </script></head></html>"""

    assert _page_organization_name_from_html(html) is None


def test_leader_and_company_profile_facts_carry_the_colocated_org_name():
    """Real-world regression, from real evaluator feedback: a shared/
    multi-tenant domain (e.g. a Norwegian housing-cooperative property
    manager like OBOS or USBL, which many small housing co-ops legitimately
    list as their registered "website") has its OWN JSON-LD Organization
    block (name="OBOS BBL") with its OWN sameAs/employee data. Without
    knowing which name that data belongs to, verify() previously accepted it
    for ANY entity whose official_site happened to be that shared domain --
    attaching the property manager's own social accounts to the individual
    housing cooperative. context_name is what lets verify() catch this."""
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Organization","name":"OBOS BBL",
     "sameAs":["https://www.facebook.com/obosmedlem"],
     "employee":[{"@type":"Person","name":"Some OBOS Exec","jobTitle":"CEO"}]}
    </script></head></html>"""

    facts = extract(_inline_page(html))

    profiles = [f for f in facts if f.field_name == "company_profile"]
    leaders = [f for f in facts if f.field_name == "leader"]
    assert profiles and all(f.context_name == "OBOS BBL" for f in profiles)
    assert leaders and all(f.context_name == "OBOS BBL" for f in leaders)


def test_structured_dated_activity_carries_the_pages_own_organization_name():
    """Real-world regression, found auditing a real 1,000-company batch:
    every housing co-op (borettslag) with USBL as its registered website
    inherited USBL's OWN "Om oss"/"Ledige stillinger"/"Presserom" pages as
    its "dated_activity" -- USBL's CMS stamps a generic Article-type JSON-LD
    block (with USBL's own name and a page datePublished) on every static
    page of the site. leader/company_profile already carry context_name for
    exactly this shared-domain scenario (see the OBOS test above);
    dated_activity was the one field type that didn't, so it slipped
    through verify()'s context_name gate untouched -- a live, unfixed
    precision gap, not just stale pre-fix data."""
    html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Organization","name":"USBL"}
    </script>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Article","name":"Om oss","datePublished":"2019-11-05"}
    </script>
    </head></html>"""

    facts = extract(_inline_page(html))

    dated = [f for f in facts if f.field_name == "dated_activity"]
    assert dated and all(f.context_name == "USBL" for f in dated)


def test_freetext_dated_activity_carries_the_pages_own_organization_name():
    """Same shared-domain gap as above, but for the free-text date-line scan
    path (_activity_facts) rather than the structured JSON-LD path -- both
    producers of dated_activity must attach the page's own org identity."""
    html = """<html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Organization","name":"USBL"}
    </script>
    </head><body><p>Usbl inngår samarbeid med Styretavla 2019-11-10</p></body></html>"""

    facts = extract(_inline_page(html))

    dated = [f for f in facts if f.field_name == "dated_activity" and f.extraction_method == "text"]
    assert dated and all(f.context_name == "USBL" for f in dated)


def test_page_organization_name_falls_back_to_opengraph_site_name():
    """Real-world regression, suggested by independent review: a site with
    no JSON-LD Organization block at all but a real og:site_name meta tag
    (a common pattern for sites that skip structured data) should still give
    dated_activity a context_name to check against, rather than silently
    giving up -- same safe-direction reasoning as the deny-list change:
    a wrong/missing context_name can only cause over-rejection, never a
    wrong-company acceptance."""
    from src.pipeline.extract import _page_organization_name_from_html

    html = '<html><head><meta property="og:site_name" content="Kahoot!"></head></html>'

    assert _page_organization_name_from_html(html) == "Kahoot!"


def test_context_name_is_none_when_the_json_ld_block_has_no_name():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Organization",
     "sameAs":["https://www.facebook.com/example"]}
    </script></head></html>"""

    facts = extract(_inline_page(html))

    profiles = [f for f in facts if f.field_name == "company_profile"]
    assert profiles and profiles[0].context_name is None


def test_an_abnormally_long_free_text_line_is_rejected_as_dated_activity():
    """Real-world regression, found by independent review of a real
    100-company batch: org 816981522 (BERATO INVEST AS) got a directory-site
    (1881.no) attributed as its official site, and crawling that shared
    directory scraped an OTHER, unrelated business's listing page as one
    giant ~8,000-character line (dozens of concatenated URLs/dates/scores
    with no HTML block tags to split them apart) -- published whole as a
    single "dated_activity" claim. Even with 1881.no now blacklisted at the
    discovery layer (see test_discovery.py), any future oversized
    concatenated blob from any source should be rejected on its face: a
    genuine dated-activity headline is never anywhere close to this long."""
    junk_line = " ".join(f"https://example.com/listing-{i} 2026-0{(i % 9) + 1}-01" for i in range(60))
    html = f"<html><body><p>{junk_line}</p></body></html>"

    facts = extract(_inline_page(html))

    assert not any(f.field_name == "dated_activity" and f.extraction_method == "text" for f in facts)


def test_a_normal_length_dated_activity_line_is_still_accepted():
    html = "<html><body><p>Company wins innovation award 2026-07-01</p></body></html>"

    facts = extract(_inline_page(html))

    dated = [f for f in facts if f.field_name == "dated_activity" and f.extraction_method == "text"]
    assert len(dated) == 1


def test_bare_careers_heading_is_not_a_hiring_signal():
    """Real evaluator feedback: a careers-page heading with no actual job
    listing was previously false-positived as hiring evidence."""
    html = "<html><body><h1>Careers</h1><p>We don't have openings right now.</p></body></html>"

    facts = extract(_inline_page(html))

    assert not any(f.field_name == "hiring_signal" for f in facts)


def test_career_counselling_service_is_not_a_hiring_signal():
    """Real evaluator feedback: a business whose own service description
    uses the word "career" (e.g. a career-counselling service) was
    previously false-positived as evidence that the business itself is
    hiring."""
    html = "<html><body><p>We offer career counselling and job search support for professionals.</p></body></html>"

    facts = extract(_inline_page(html))

    assert not any(f.field_name == "hiring_signal" for f in facts)
