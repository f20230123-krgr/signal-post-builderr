"""Tests for the optional headless-browser fallback (src/pipeline/render.py and the
completeness check in src/pipeline/crawl.py).

Builderr's playbook: "Crawl static HTML first. Escalate to a browser only when a
deterministic completeness check fails." These tests pin that check and the guarantees
around it. No browser runs here: a fake renderer stands in.
"""
from datetime import datetime, timezone

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.crawl import DEFAULT_ATS_DOMAINS, crawl, looks_script_built, needs_render
from src.pipeline.render import BrowserRenderer, RenderResult, active_renderer, set_active_renderer
from tests.pipeline.test_crawl import _entity

SHELL = '<html><head><script src="/a.js"></script><script src="/b.js"></script><script src="/c.js"></script></head><body><div id="root"></div></body></html>'
RENDERED = (
    '<html><body><nav><a href="/karriere">Karriere</a><a href="/nyheter">Nyheter</a></nav>'
    '<footer><a href="https://www.linkedin.com/company/acme/">LinkedIn</a></footer></body></html>'
)
STATIC_RICH = '<html><body><a href="/karriere">Karriere</a><a href="https://www.facebook.com/acme">f</a>' + "<p>text</p>" * 80 + "</body></html>"


class FakeRenderer:
    available = True

    def __init__(self, html=RENDERED, final_url=None):
        self.html, self.final_url, self.calls = html, final_url, []

    def render(self, url):
        self.calls.append(url)
        return RenderResult(self.html, self.final_url or url, 7)


def _client(routes):
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=routes[str(r.url)]) if str(r.url) in routes else httpx.Response(404)
    ))


# --- when is a browser worth it? ----------------------------------------------------------


def test_a_script_built_page_is_recognised_by_its_mount_point_or_its_lack_of_text():
    assert looks_script_built(SHELL)
    assert looks_script_built('<script>1</script><div id="__nuxt"></div>')
    assert not looks_script_built(STATIC_RICH)


def test_the_homepage_is_rendered_only_when_it_is_script_built_and_shows_no_careers_or_social_link():
    args = ("https://example.com/", "https://example.com/", set(DEFAULT_ATS_DOMAINS), None)

    assert needs_render(SHELL, *args) is True
    assert needs_render(STATIC_RICH, *args) is False  # a static page that already shows the links
    with_social = SHELL.replace('<div id="root">', '<div id="root"><a href="https://www.linkedin.com/company/acme/">in</a>')
    assert needs_render(with_social, *args) is False  # social found statically: nothing to gain


def test_other_pages_and_other_companies_pages_are_never_rendered():
    ats = set(DEFAULT_ATS_DOMAINS)

    assert needs_render(SHELL, "https://example.com/om-oss", "https://example.com/", ats, None) is False
    assert needs_render(SHELL, "https://example.com/", "https://example.com/", ats, "https://parent.com/") is False  # reached by a link chain
    assert needs_render(SHELL, "https://other.com/", "https://example.com/", ats, None) is False
    assert needs_render(SHELL, "https://example.com/", None, ats, None) is False


def test_a_script_built_careers_page_with_no_static_job_ads_is_rendered_but_one_listing_ads_is_not():
    ats = set(DEFAULT_ATS_DOMAINS)
    listing = '<a href="/stillinger/a-role">Elektriker Bergen</a><a href="/stillinger/b-role">Rørlegger Oslo</a>' + SHELL

    assert needs_render(SHELL, "https://example.com/karriere", "https://example.com/", ats, None) is True
    assert needs_render(listing, "https://example.com/karriere", "https://example.com/", ats, None) is False


# --- the crawl uses it, within limits ------------------------------------------------------


def test_the_crawl_reads_the_rendered_dom_so_links_hidden_by_scripts_are_found():
    renderer = FakeRenderer()
    routes = {"https://example.com/": SHELL, "https://example.com/karriere": "<html><title>Karriere</title></html>",
              "https://example.com/nyheter": "<html></html>"}

    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes),
                  follow_careers=True, follow_news=True, ats_domains=DEFAULT_ATS_DOMAINS, renderer=renderer)

    home = pages[0]
    assert home.rendered and "linkedin.com/company/acme" in home.raw_html
    assert renderer.calls == ["https://example.com/"]
    assert {"https://example.com/karriere", "https://example.com/nyheter"} <= {p.url for p in pages}  # links found only after rendering


def test_without_a_renderer_the_crawl_is_unchanged():
    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client({"https://example.com/": SHELL}),
                  follow_careers=True)

    assert not pages[0].rendered and pages[0].raw_html == SHELL


def test_a_page_that_does_not_need_a_browser_never_starts_one():
    renderer = FakeRenderer()

    crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client({"https://example.com/": STATIC_RICH}), renderer=renderer)

    assert renderer.calls == []


def test_rendering_stops_when_the_request_budget_is_running_low():
    renderer = FakeRenderer()
    tight = BudgetGovernor(BudgetLimits(max_requests=300))  # less headroom than a render may use

    pages = crawl(_entity("https://example.com/"), tight, client=_client({"https://example.com/": SHELL}), renderer=renderer)

    assert renderer.calls == [] and not pages[0].rendered


def test_a_failed_render_falls_back_to_the_static_page():
    class Broken(FakeRenderer):
        def render(self, url):
            return None

    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client({"https://example.com/": SHELL}), renderer=Broken())

    assert pages[0].raw_html == SHELL and not pages[0].rendered


def test_a_rendered_page_is_cached_so_a_repeat_run_costs_no_second_render(tmp_path):
    from src.storage.cache import ResponseCache

    cache = ResponseCache(tmp_path / "c.sqlite3")
    first, second = FakeRenderer(), FakeRenderer()
    routes = {"https://example.com/": SHELL}

    crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes), renderer=first, cache=cache)
    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes), renderer=second, cache=cache)

    assert first.calls == ["https://example.com/"] and second.calls == []
    assert pages[0].rendered and "linkedin.com" in pages[0].raw_html


# --- the renderer itself --------------------------------------------------------------------


def test_a_renderer_whose_browser_never_started_reports_unavailable_and_renders_nothing():
    renderer = BrowserRenderer()

    assert renderer.available is False
    assert renderer.render("https://example.com/") is None
    assert renderer.report() == {"available": False, "renders": 0, "requests": 0, "failures": 0}


def test_the_active_renderer_is_none_until_one_that_is_available_is_set():
    set_active_renderer(None)
    assert active_renderer() is None
    set_active_renderer(BrowserRenderer())  # not started: unavailable
    assert active_renderer() is None
    set_active_renderer(None)


def test_browser_requests_are_counted_in_the_same_counter_as_http_requests():
    from src.pipeline import net

    before = net.wire_request_count()
    net.add_external_request("example.com")

    assert net.wire_request_count() == before + 1
    assert net.wire_request_breakdown(top_hosts=0)["by_host"]["example.com"] >= 1


def test_the_renderer_refuses_a_url_the_outbound_policy_refuses():
    renderer = BrowserRenderer()
    renderer._available = True  # pretend a browser exists: the policy check runs before any page is queued

    assert renderer.render("http://127.0.0.1/admin") is None
    assert renderer.render("file:///etc/passwd") is None
