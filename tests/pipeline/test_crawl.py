"""Tests for src/pipeline/crawl.py -- see docs/component-specs.md."""
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.crawl import crawl
from src.pipeline.resolve import ResolvedEntity

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
