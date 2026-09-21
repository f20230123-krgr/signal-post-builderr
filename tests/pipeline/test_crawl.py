"""Tests for src/pipeline/crawl.py -- see docs/component-specs.md."""
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.crawl import crawl
from src.pipeline.resolve import ResolvedEntity
from src.storage.cache import ResponseCache

PAGES = Path(__file__).parent.parent.parent / "fixtures" / "pages"


def _entity(site="https://example.com/") -> ResolvedEntity:
    return ResolvedEntity(
        org_number="923609016",
        legal_name="EQUINOR ASA",
        registered_address="Forusbeen 50, 4035 STAVANGER",
        official_site_candidate=site,
        resolution_state=EvidenceState.AVAILABLE,
        source="https://data.brreg.no/enhetsregisteret/api/enheter/923609016",
        retrieved_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def _handler_serving(pages_by_url: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url in pages_by_url:
            return httpx.Response(200, text=pages_by_url[url])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_budget_exhausted_mid_crawl_stops_cleanly_with_partial_pages():
    real_page = (PAGES / "923609016" / "official_site.html").read_text(encoding="utf-8")
    client = _handler_serving(
        {
            "https://example.com/": real_page,
            "https://example.com/about": real_page,
        }
    )
    budget = BudgetGovernor(BudgetLimits(max_requests=1, max_spend_usd=10, max_wall_clock_seconds=1000))
    entity = _entity("https://example.com/")

    pages = crawl(
        entity,
        budget,
        client=client,
        secondary_urls=["https://example.com/about"],
        allowed_domains={"example.com"},
    )

    assert len(pages) == 2
    assert pages[0].fetch_state == EvidenceState.AVAILABLE
    assert pages[0].raw_html == real_page
    assert pages[1].fetch_state == EvidenceState.BLOCKED
    assert pages[1].raw_html == ""


def test_js_shell_detection_triggers_playwright_fallback():
    shell_html = (PAGES / "edge_cases" / "js_shell.html").read_text(encoding="utf-8")
    rendered_html = (PAGES / "edge_cases" / "js_shell_rendered.html").read_text(encoding="utf-8")
    client = _handler_serving({"https://example.com/": shell_html})
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    render_calls = []

    def fake_render(url: str) -> str:
        render_calls.append(url)
        return rendered_html

    pages = crawl(entity, budget, client=client, render_js=fake_render, allowed_domains=set())

    assert len(pages) == 1
    assert pages[0].fetch_state == EvidenceState.AVAILABLE
    assert pages[0].raw_html == rendered_html
    assert render_calls == ["https://example.com/"]


def test_js_shell_without_playwright_available_keeps_static_content():
    shell_html = (PAGES / "edge_cases" / "js_shell.html").read_text(encoding="utf-8")
    client = _handler_serving({"https://example.com/": shell_html})
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, render_js=None, allowed_domains=set())

    assert len(pages) == 1
    assert pages[0].fetch_state == EvidenceState.AVAILABLE
    assert pages[0].raw_html == shell_html


def test_disallowed_url_is_skipped_not_fetched(caplog):
    real_page = (PAGES / "923609016" / "official_site.html").read_text(encoding="utf-8")
    client = _handler_serving(
        {
            "https://example.com/": real_page,
            "https://not-permitted.example.org/": "<html>should never be fetched</html>",
        }
    )
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    with caplog.at_level("WARNING"):
        pages = crawl(
            entity,
            budget,
            client=client,
            secondary_urls=["https://not-permitted.example.org/"],
            allowed_domains=set(),
        )

    urls_fetched = {p.url for p in pages}
    assert urls_fetched == {"https://example.com/"}
    assert "not-permitted.example.org" in caplog.text


def test_malformed_server_redirect_is_reported_failed_not_uncaught():
    """Real-world regression: a site can return a Location header with an
    empty hostname label (e.g. "https://.example.no/...") -- httpx's
    redirect-following then raises a bare UnicodeError from the idna codec,
    not an httpx.TransportError. This must degrade to FAILED like any other
    fetch problem, not propagate past crawl() and abort the whole company."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise UnicodeError("label empty or too long")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, sleep=lambda s: None, allowed_domains=set())

    assert pages[0].fetch_state == EvidenceState.FAILED


def test_too_many_redirects_is_reported_failed_not_uncaught():
    """Real-world regression, same class as the malformed-redirect one above:
    a page can redirect in a loop. httpx.TooManyRedirects is an
    httpx.HTTPError, not an httpx.TransportError -- found running the real
    1,000-company batch (in discovery.py's verify_discovered_site, which
    shares this same fetch-error-handling pattern)."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, sleep=lambda s: None, allowed_domains=set())

    assert pages[0].fetch_state == EvidenceState.FAILED


def test_static_fetch_retries_then_marks_failed_on_persistent_transport_error():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.TransportError("connection reset")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, sleep=lambda s: None, allowed_domains=set())

    assert len(pages) == 1
    assert pages[0].fetch_state == EvidenceState.FAILED
    assert len(calls) > 1


def test_http_404_is_reported_not_available_not_treated_as_fetched():
    client = _handler_serving({})  # any URL -> 404, per _handler_serving's default
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, allowed_domains=set())

    assert len(pages) == 1
    assert pages[0].fetch_state == EvidenceState.NOT_AVAILABLE
    assert pages[0].raw_html == ""


def test_http_403_is_reported_blocked():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(403)))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, allowed_domains=set())

    assert pages[0].fetch_state == EvidenceState.BLOCKED


def test_http_500_after_retries_is_reported_failed():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, sleep=lambda s: None, allowed_domains=set())

    assert pages[0].fetch_state == EvidenceState.FAILED


def test_no_official_site_candidate_returns_no_pages():
    entity = _entity(site=None)
    budget = BudgetGovernor()
    pages = crawl(entity, budget, client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))))
    assert pages == []


def test_company_owned_paths_are_opt_in_and_same_domain():
    """Playbook/learning-harness both prioritise same-domain company pages
    (/about, /careers, /contact, etc.) -- explicitly opt-in via
    company_owned_paths so existing single-URL callers/tests are unaffected."""
    seen_urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_urls.append(str(request.url))
        return httpx.Response(200, text="<html><body>hi</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com")

    pages = crawl(entity, budget, client=client, company_owned_paths=["/about", "/careers"])

    assert len(pages) == 3  # official site + 2 company-owned paths
    assert "https://example.com/about" in seen_urls
    assert "https://example.com/careers" in seen_urls
    # same domain -- no allow-list violation, no extra allowed_domains needed
    assert all(p.fetch_state == EvidenceState.AVAILABLE for p in pages)


def test_company_owned_paths_default_to_none_unchanged_behavior():
    client = _handler_serving({"https://example.com/": "<html>hi</html>"})
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client)

    assert len(pages) == 1


def test_company_owned_paths_respect_budget():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="hi")))
    budget = BudgetGovernor(BudgetLimits(max_requests=1, max_spend_usd=10, max_wall_clock_seconds=1000))
    entity = _entity("https://example.com")

    pages = crawl(entity, budget, client=client, company_owned_paths=["/about", "/careers"])

    assert len(pages) == 3
    assert pages[0].fetch_state == EvidenceState.AVAILABLE
    assert pages[1].fetch_state == EvidenceState.BLOCKED
    assert pages[2].fetch_state == EvidenceState.BLOCKED


# --- cache wiring (evaluator feedback: "bounded ... crawler with ... caching") ---


def test_cache_hit_serves_content_without_spending_budget(tmp_path):
    cache = ResponseCache(tmp_path / "cache.sqlite3")
    cache.put("https://example.com/", "2026-09-08", "<html>cached page</html>")

    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="<html>live page</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor(BudgetLimits(max_requests=0, max_spend_usd=10, max_wall_clock_seconds=1000))
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, cache=cache, date_bucket="2026-09-08", allowed_domains=set())

    assert calls == []  # never hit the network
    assert pages[0].raw_html == "<html>cached page</html>"
    assert pages[0].fetch_state == EvidenceState.AVAILABLE


def test_cache_miss_fetches_and_stores_result(tmp_path):
    cache = ResponseCache(tmp_path / "cache.sqlite3")
    client = _handler_serving({"https://example.com/": "<html>fresh</html>"})
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, cache=cache, date_bucket="2026-09-08", allowed_domains=set())

    assert pages[0].raw_html == "<html>fresh</html>"
    assert cache.get("https://example.com/", "2026-09-08") == "<html>fresh</html>"


# --- sitemap.xml / robots.txt discovery ---


def test_sitemap_discovery_adds_priority_pages():
    sitemap_xml = """<?xml version="1.0"?>
    <urlset>
      <url><loc>https://example.com/</loc></url>
      <url><loc>https://example.com/careers</loc></url>
      <url><loc>https://example.com/blog/post-1</loc></url>
      <url><loc>https://example.com/team</loc></url>
    </urlset>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml)
        return httpx.Response(200, text="<html>page content here, plenty of text</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, use_sitemap=True, allowed_domains=set())

    urls = {p.url for p in pages}
    assert "https://example.com/careers" in urls
    assert "https://example.com/team" in urls
    # not a priority-keyword page -- not worth the budget
    assert "https://example.com/blog/post-1" not in urls


def test_sitemap_discovery_never_treats_a_nested_sitemap_as_a_content_page():
    """Real-world regression, found auditing a real 100-company batch: a
    sitemap index's own <loc> entries can point at OTHER sitemap files (e.g.
    "/sitemap/news/sitemap.xml"), not just real content pages. "news" is a
    priority keyword, so that nested sitemap URL was being crawled and
    extract()'d as if it were a normal HTML page -- with no HTML block tags
    to split on, its raw XML (hundreds of concatenated <loc>/<lastmod>
    pairs) became a single giant garbage "dated_activity" fact. A URL whose
    path ends in .xml is always itself a sitemap, never a content page --
    must never be added to the discovered page list, regardless of whether
    it also happens to match a priority keyword."""
    sitemap_xml = """<?xml version="1.0"?>
    <urlset>
      <url><loc>https://example.com/news</loc></url>
      <url><loc>https://example.com/sitemap/news/sitemap.xml</loc></url>
    </urlset>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml)
        return httpx.Response(200, text="<html>page content here, plenty of text</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, use_sitemap=True, allowed_domains=set())

    urls = {p.url for p in pages}
    assert "https://example.com/news" in urls
    assert "https://example.com/sitemap/news/sitemap.xml" not in urls


def test_sitemap_discovery_recognizes_norwegian_only_news_page_paths():
    """Real-world gap, confirmed by independent review: our priority-keyword
    list had the English "/news" but no Norwegian-only equivalent -- a
    Norwegian-language site can have "/aktuelt" or "/nyheter" with no
    "/news" at all, and neither word is a substring of any existing keyword
    (unlike "/presse", already covered since "press" is a substring of
    "presse"). Missing this meant sitemap discovery silently skipped a
    real, common category of Norwegian company pages."""
    sitemap_xml = """<?xml version="1.0"?>
    <urlset>
      <url><loc>https://example.com/aktuelt</loc></url>
      <url><loc>https://example.com/nyheter/2026-vekst</loc></url>
      <url><loc>https://example.com/blog/unrelated-post</loc></url>
    </urlset>"""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml)
        return httpx.Response(200, text="<html>page content here, plenty of text</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, use_sitemap=True, allowed_domains=set())

    urls = {p.url for p in pages}
    assert "https://example.com/aktuelt" in urls
    assert "https://example.com/nyheter/2026-vekst" in urls
    assert "https://example.com/blog/unrelated-post" not in urls


# Norwegian news pages (/aktuelt, /nyheter) are no longer GUESSED at as fixed
# paths: on the 1,000-company corpus the two together produced claims on a single
# page, at ~2 requests per site. They are still found wherever a site really has
# them, through the sitemap keywords tested just above and the pages' own links.


def test_robots_txt_disallow_is_respected(caplog):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /careers\n")
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(
                200,
                text='<urlset><url><loc>https://example.com/careers</loc></url>'
                '<url><loc>https://example.com/about</loc></url></urlset>',
            )
        return httpx.Response(200, text="<html>page content here, plenty of text</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    with caplog.at_level("WARNING"):
        pages = crawl(entity, budget, client=client, use_sitemap=True, allowed_domains=set())

    urls = {p.url for p in pages}
    assert "https://example.com/careers" not in urls
    assert "https://example.com/about" in urls


def test_sitemap_discovery_is_opt_in():
    """use_sitemap defaults to False -- existing callers unaffected."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, text="<html>hi</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    crawl(entity, budget, client=client, allowed_domains=set())

    assert not any("robots.txt" in u or "sitemap.xml" in u for u in calls)


# --- following official outbound links to allow-listed ATS platforms ---


def test_follows_official_outbound_link_to_allow_listed_ats_domain():
    careers_html = '<html><body><a href="https://boards.greenhouse.io/examplecorp">Open roles</a></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/":
            return httpx.Response(200, text=careers_html)
        if url == "https://boards.greenhouse.io/examplecorp":
            return httpx.Response(200, text="<html>Backend Engineer, Oslo</html>")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, ats_domains={"greenhouse.io"}, allowed_domains=set())

    urls = {p.url for p in pages}
    assert "https://boards.greenhouse.io/examplecorp" in urls
    ats_page = next(p for p in pages if p.url == "https://boards.greenhouse.io/examplecorp")
    assert ats_page.linked_from == "https://example.com/"
    assert ats_page.fetch_state == EvidenceState.AVAILABLE


def test_default_ats_domains_include_recman_and_jobylon():
    """recman.no and jobylon.com added after independent review, verified
    live before adding (unlike webcruiter.no, checked the same way and
    found to render NO structured data at all): a real recman.no listing
    (apply.recman.no/job_post.php, for POWER Norge AS) and a real
    jobylon.com listing (emp.jobylon.com, for Hotel Norge by Scandic) were
    each fetched and inspected directly -- both render a genuine
    "@type": "JobPosting" JSON-LD block our extractor already knows how to
    read. Norwegian-market ATS platforms, unlike the mostly US-centric
    prior list (greenhouse/lever/workable/teamtailor/workday)."""
    from src.pipeline.crawl import DEFAULT_ATS_DOMAINS

    assert "recman.no" in DEFAULT_ATS_DOMAINS
    assert "jobylon.com" in DEFAULT_ATS_DOMAINS


def test_follows_official_outbound_link_to_recman_subdomain():
    """recman.no listings commonly live on a per-customer subdomain (e.g.
    apply.recman.no) -- must match via the same subdomain rule already used
    for boards.greenhouse.io."""
    careers_html = '<html><body><a href="https://apply.recman.no/job_post.php?id=1">Ledige stillinger</a></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://example.com/":
            return httpx.Response(200, text=careers_html)
        if url == "https://apply.recman.no/job_post.php?id=1":
            return httpx.Response(200, text="<html>Salgstalent, Oslo</html>")
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, ats_domains={"recman.no"}, allowed_domains=set())

    urls = {p.url for p in pages}
    assert "https://apply.recman.no/job_post.php?id=1" in urls


def test_does_not_follow_outbound_link_to_non_allow_listed_domain():
    html = '<html><body><a href="https://random-blog.example.net/">Blog</a></body></html>'
    client = _handler_serving({"https://example.com/": html})
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, ats_domains={"greenhouse.io"}, allowed_domains=set())

    assert not any(p.url.startswith("https://random-blog.example.net") for p in pages)


def test_ats_link_following_is_opt_in():
    """ats_domains defaults to None -- outbound links are never followed
    unless explicitly requested."""
    html = '<html><body><a href="https://boards.greenhouse.io/examplecorp">Jobs</a></body></html>'
    client = _handler_serving({"https://example.com/": html})
    budget = BudgetGovernor()
    entity = _entity("https://example.com/")

    pages = crawl(entity, budget, client=client, allowed_domains=set())

    assert len(pages) == 1


def test_crawl_follows_a_declared_rss_feed_on_the_official_domain():
    """A site declaring <link rel="alternate" type="application/rss+xml">
    is pointing at its own published activity. Same domain, so no new
    allow-list risk, and only fetched when actually declared -- a company
    with no feed costs nothing."""
    home = (
        '<html><head><link rel="alternate" type="application/rss+xml" '
        'title="Nyheter" href="/feed.xml"></head><body>Home</body></html>'
    )
    fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if request.url.path == "/feed.xml":
            return httpx.Response(200, text="<rss><channel></channel></rss>")
        return httpx.Response(200, text=home)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pages = crawl(_entity("https://acme.no"), BudgetGovernor(), client=client)

    assert any(u.endswith("/feed.xml") for u in fetched)
    assert any(p.url.endswith("/feed.xml") for p in pages)


def test_crawl_does_not_invent_a_feed_when_none_is_declared():
    fetched = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        return httpx.Response(200, text="<html><body>No feed here</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    crawl(_entity("https://acme.no"), BudgetGovernor(), client=client)

    assert not any("feed" in u for u in fetched)


# ---- spending fewer of the 2,000 real requests ---------------------------
# Builderr counts every redirect hop and retry. These pin the three places the
# crawl was spending requests for (almost) nothing.


def test_default_guessed_paths_are_the_four_that_actually_yield_claims():
    """Measured on the 1,000-company corpus (293 sites): pages at these paths
    yielded claims 25 times of the 30 that any of the 13 guessed paths did.
    The other nine cost ~9 requests per site for ~0.1% of claims."""
    from src.pipeline.crawl import DEFAULT_COMPANY_OWNED_PATHS

    assert DEFAULT_COMPANY_OWNED_PATHS == ["/kontakt", "/om-oss", "/about", "/contact"]


def test_after_a_redirect_to_another_host_later_pages_go_straight_to_it():
    """example.no -> www.example.no on every page would cost two requests per
    page. Once the first fetch shows where the site really lives, the rest of
    the crawl asks there directly."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "example.no":
            return httpx.Response(301, headers={"Location": str(request.url.copy_with(host="www.example.no"))})
        return httpx.Response(200, text="<html><body>hello there, a page with enough text to count</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    pages = crawl(
        _entity("https://example.no/"), budget, client=client,
        company_owned_paths=["/om-oss", "/kontakt", "/about"],
    )

    assert [p.fetch_state for p in pages] == [EvidenceState.AVAILABLE] * 4
    # one redirect hop for the first page only; the three guessed paths went to www directly
    assert requested.count("https://example.no/") == 1
    assert [u for u in requested if u.startswith("https://example.no")] == ["https://example.no/"]
    assert len(requested) == 5  # root (2 hops) + 3 direct


def test_a_redirect_to_an_unrelated_domain_is_not_followed_for_later_pages():
    """Rebasing is only for the same site under another host name, never a
    way to leave the entity's own domain."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "example.no":
            return httpx.Response(302, headers={"Location": "https://other-company.com/"})
        return httpx.Response(200, text="<html><body>an unrelated site with enough text on it</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    crawl(_entity("https://example.no/"), BudgetGovernor(), client=client, company_owned_paths=["/om-oss"])

    assert "https://example.no/om-oss" in requested  # still asked on the entity's own host


def test_a_site_that_keeps_failing_is_given_up_on_after_three_failures():
    """A server that answers 503 to everything cost 2 attempts x 16 pages = 32
    requests for one company in a real batch."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    pages = crawl(
        _entity("https://broken.no/"), budget, client=client,
        company_owned_paths=["/kontakt", "/om-oss", "/about", "/contact"],
        sleep=lambda s: None,
    )

    assert len(pages) == 5  # every page still gets a terminal state...
    assert all(p.fetch_state == EvidenceState.FAILED for p in pages)
    assert len(requested) <= 8  # ...but the site was abandoned early (not 5 x 2 = 10)


def test_one_failure_does_not_stop_the_rest_of_a_healthy_site():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/kontakt":
            return httpx.Response(503)
        return httpx.Response(200, text="<html><body>a page with enough visible text to count as content</body></html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    pages = crawl(
        _entity("https://fine.no/"), BudgetGovernor(), client=client,
        company_owned_paths=["/kontakt", "/om-oss", "/about"], sleep=lambda s: None,
    )

    states = {p.url: p.fetch_state for p in pages}
    assert states["https://fine.no/om-oss"] == EvidenceState.AVAILABLE
    assert states["https://fine.no/about"] == EvidenceState.AVAILABLE


def test_a_redirect_loop_is_one_failed_fetch_not_a_retry_storm():
    """robots.txt -> robots.txt-Home -> robots.txt-Home ... cost 191 requests
    for one company in a real batch (20 hops x 2 attempts x several fetches)."""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path.startswith("/robots.txt"):
            return httpx.Response(302, headers={"Location": "https://loopy.no/robots.txt-Home"})
        return httpx.Response(200, text="<html><body>a page with enough visible text to count as content</body></html>")

    from src.pipeline.net import new_client

    client = new_client(
        transport=httpx.MockTransport(handler),
        policy=__import__("src.pipeline.net", fromlist=["OutboundPolicy"]).OutboundPolicy(resolver=lambda *a, **k: []),
    )

    pages = crawl(_entity("https://loopy.no/"), BudgetGovernor(), client=client, use_sitemap=True, sleep=lambda s: None)

    assert any(p.fetch_state == EvidenceState.AVAILABLE for p in pages)  # the site itself is still crawled
    assert len(requested) <= 15
