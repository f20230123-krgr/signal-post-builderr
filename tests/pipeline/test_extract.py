"""Tests for src/pipeline/extract.py -- see docs/component-specs.md."""
from datetime import datetime, timezone
from pathlib import Path

from src.pipeline.crawl import FetchedPage
from src.pipeline.extract import MAX_FEED_ACTIVITY_ENTRIES, MAX_SOCIAL_PROFILE_LINKS, feed_activity_facts, social_profile_facts, extract
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


def test_a_list_valued_json_ld_name_does_not_crash_extraction():
    """Real-world regression, found running a real 1,000-company batch: one
    company's page crashed the entire pipeline run with
    "AttributeError: 'list' object has no attribute 'lower'" (caught by
    runner.py's outer safety net, so that one company got a degraded
    profile rather than crashing the batch -- but it's a real, fixable
    gap). schema.org's "name" property is typed Text, but ALSO legally
    accepts [Text] (multiple alternate names) per the spec -- a real site
    apparently used this. structured_facts() treated obj["name"] as
    guaranteed to be a string and fed it straight into organization_name's
    value, which later reaches name_similarity()'s .lower() call and
    crashes. Must not crash -- and must not invent a value either; a
    non-string name is treated as absent, same discipline already used for
    sameAs links (isinstance(link, str) and link)."""
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Organization","name":["Example AS","Example Company"]}
    </script></head></html>"""

    facts = extract(_inline_page(html))  # must not raise

    assert not any(f.field_name == "organization_name" for f in facts)


def test_a_list_valued_json_ld_title_does_not_crash_hiring_signal_extraction():
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":["Engineer","Utvikler"],"datePosted":"2026-06-20"}
    </script></head></html>"""

    facts = extract(_inline_page(html))  # must not raise

    assert not any(f.field_name == "hiring_signal" for f in facts)


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


def test_social_profile_links_in_plain_markup_become_company_profile_facts():
    """company_profile sat at ~4% of companies in a real 1,000-company batch
    because it was sourced ONLY from JSON-LD `sameAs`. Most real sites put
    their LinkedIn/Facebook/Instagram in a plain footer <a href> with no
    structured data at all. Provenance is identical to the sameAs path --
    these links are on the entity's own verified domain, which verify.py
    already gates on -- so this is coverage, not a precision trade."""
    html = """
    <html><body>
      <footer>
        <a href="https://www.linkedin.com/company/acme-norge/">LinkedIn</a>
        <a href="https://www.facebook.com/acmenorge">Facebook</a>
        <a href="https://instagram.com/acme.norge">Instagram</a>
      </footer>
    </body></html>
    """
    facts = social_profile_facts(html, "https://acme.no", datetime(2026, 1, 1, tzinfo=timezone.utc))
    values = [f.value for f in facts]

    assert "https://www.linkedin.com/company/acme-norge/" in values
    assert "https://www.facebook.com/acmenorge" in values
    assert "https://instagram.com/acme.norge" in values
    assert all(f.field_name == "company_profile" for f in facts)
    assert all(f.extraction_method == "structured" for f in facts)


def test_social_profile_extraction_ignores_share_buttons_and_platform_chrome():
    """A share/intent link is the page telling a visitor how to repost it --
    not the company declaring its own profile. Publishing one as a
    "company-owned profile" would be a wrong claim, not a thin one."""
    html = """
    <html><body>
      <a href="https://www.facebook.com/sharer/sharer.php?u=https://acme.no">Share</a>
      <a href="https://twitter.com/intent/tweet?url=https://acme.no">Tweet</a>
      <a href="https://www.linkedin.com/shareArticle?url=https://acme.no">Share</a>
      <a href="https://www.facebook.com/plugins/like.php">Like</a>
      <a href="https://example.com/not-social">Partner</a>
    </body></html>
    """
    assert social_profile_facts(html, "https://acme.no", datetime(2026, 1, 1, tzinfo=timezone.utc)) == []


def test_social_profile_extraction_dedups_and_caps():
    """A site-wide template repeats the same links on every page; a page can
    also list dozens. Dedup by URL and cap so one templated footer can't
    flood the section (same discipline as assemble.py's claim dedup)."""
    links = "".join(
        f'<a href="https://www.instagram.com/handle{i}">i</a>' for i in range(20)
    )
    html = f"<html><body><footer>{links}{links}</footer></body></html>"

    facts = social_profile_facts(html, "https://acme.no", datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert len(facts) == len({f.value for f in facts})
    assert len(facts) <= MAX_SOCIAL_PROFILE_LINKS


def test_public_brand_falls_back_to_application_name_meta_tags():
    """public_brand came only from og:site_name (plus JSON-LD Organization
    name). Plenty of real sites ship no og:site_name but do declare
    application-name / apple-mobile-web-app-title -- both single, structured,
    self-declared brand tags on the company's own verified domain, so they
    carry the same provenance as og:site_name and no extra request."""
    html = '<html><head><meta name="application-name" content="Acme Norge"></head><body></body></html>'
    page = _inline_page(html, url="https://acme.no")

    facts = extract(page)
    brands = [f.value for f in facts if f.field_name == "public_brand"]

    assert "Acme Norge" in brands


def test_og_site_name_still_wins_over_the_fallback_tags():
    """og:site_name is the most explicit brand declaration of the three, so a
    page carrying both must not have the weaker tag shadow it."""
    html = (
        '<html><head>'
        '<meta property="og:site_name" content="Acme Norge">'
        '<meta name="application-name" content="acme-pwa">'
        '</head><body></body></html>'
    )
    page = _inline_page(html, url="https://acme.no")

    brands = [f.value for f in extract(page) if f.field_name == "public_brand"]

    assert brands[0] == "Acme Norge"


def test_json_ld_event_becomes_dated_activity():
    """schema.org Event carries its date in startDate, not datePublished, so
    the existing dated-type handling skipped every event a company publishes
    on its own site -- real dated public activity, structured, already on a
    page we fetch anyway (zero extra requests)."""
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Event","name":"Aapent hus i Bergen",
     "startDate":"2026-10-04","location":{"@type":"Place","name":"Bergen"}}
    </script></head><body></body></html>"""

    facts = extract(_inline_page(html))
    dated = [f for f in facts if f.field_name == "dated_activity"]

    assert len(dated) == 1
    assert "Aapent hus i Bergen" in dated[0].value
    assert "2026-10-04" in dated[0].value


def test_json_ld_event_without_a_date_is_not_published_as_dated_activity():
    """An undated event is not dated activity -- the date IS the claim."""
    html = """<html><head><script type="application/ld+json">
    {"@context":"https://schema.org","@type":"Event","name":"Some event"}
    </script></head><body></body></html>"""

    assert [f for f in extract(_inline_page(html)) if f.field_name == "dated_activity"] == []


def test_rss_feed_entries_become_dated_activity():
    """A declared RSS/Atom feed is the highest-quality dated_activity a
    company publishes about itself: real headlines with real publication
    dates, structured, on its own domain. Registry update events give us
    company-level coverage everywhere; this gives real editorial depth for
    companies that actually publish."""
    xml = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>Acme Nyheter</title>
      <item><title>Acme opens Bergen office</title><pubDate>Mon, 01 Sep 2026 09:00:00 GMT</pubDate></item>
      <item><title>Acme wins contract</title><pubDate>Tue, 12 Aug 2026 09:00:00 GMT</pubDate></item>
    </channel></rss>"""

    facts = feed_activity_facts(xml, "https://acme.no/feed", datetime(2026, 1, 1, tzinfo=timezone.utc))
    values = [f.value for f in facts]

    assert any("Acme opens Bergen office" in v and "2026" in v for v in values)
    assert all(f.field_name == "dated_activity" for f in facts)


def test_atom_feed_entries_become_dated_activity():
    xml = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>Ny avdeling i Troms</title><updated>2026-07-04T10:00:00Z</updated></entry>
    </feed>"""

    facts = feed_activity_facts(xml, "https://acme.no/atom.xml", datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert len(facts) == 1
    assert "Ny avdeling i Troms" in facts[0].value
    assert "2026-07-04" in facts[0].value


def test_feed_entries_without_a_date_are_skipped_and_output_is_capped():
    """Undated entries aren't dated activity, and a feed can carry hundreds --
    same capping discipline as every other list section."""
    items = "".join(f"<item><title>Post {i}</title><pubDate>2026-08-0{i%9+1}</pubDate></item>" for i in range(30))
    xml = f"<rss><channel><item><title>No date here</title></item>{items}</channel></rss>"

    facts = feed_activity_facts(xml, "https://acme.no/feed", datetime(2026, 1, 1, tzinfo=timezone.utc))

    assert all("No date here" not in f.value for f in facts)
    assert len(facts) <= MAX_FEED_ACTIVITY_ENTRIES


def test_non_feed_content_yields_nothing():
    assert feed_activity_facts("<html><body>not a feed</body></html>", "https://acme.no", datetime(2026, 1, 1, tzinfo=timezone.utc)) == []
