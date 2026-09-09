"""
Tests for src/pipeline/discovery.py -- candidate website discovery for
companies with no official site on file in the registry.

Source policy: "search/discovery providers for candidate generation only,
never as evidence itself" -- a search hit from ANY provider is never trusted
on its own; `verify_discovered_site` independently fetches the candidate and
requires the SAME name-match threshold verify.py uses before accepting it.

Provider chain, in order: Exa (best free tier, no card) -> Parallel (also
generous, no card, native source-exclusion) -> DuckDuckGo (always available,
free, no key, but Wikipedia-infobox-gated -- weak for small/non-notable
companies, see module docstring). Each tier is skipped (not attempted, no
budget spent) when its API key isn't set -- `discover_candidate_site`'s
`exa_api_key`/`parallel_api_key` params default to reading
EXA_API_KEY/PARALLEL_API_KEY from the environment, but tests always pass
them explicitly as None/a fake value so a developer's real shell environment
can never make a test flaky.
"""
from datetime import datetime, timezone

import httpx

from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.discovery import discover_candidate_site, verify_discovered_site

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _no_keys(**overrides):
    """Explicit, hermetic 'no paid provider configured' kwargs -- forces the
    chain straight to DuckDuckGo regardless of the real shell's environment."""
    kwargs = {"exa_api_key": None, "parallel_api_key": None}
    kwargs.update(overrides)
    return kwargs


def _ddg_response(infobox_website: str | None) -> dict:
    content = []
    if infobox_website:
        content.append({"label": "Website", "value": f"[{infobox_website}]"})
    return {"Heading": "Equinor", "Infobox": {"content": content} if content else None}


def test_query_strips_the_legal_entity_suffix():
    """Real-world regression: DuckDuckGo's Instant Answer infobox match is
    keyed to Wikipedia article titles, which use a company's common name
    ("Equinor"), not its legal name with entity suffix ("Equinor ASA") --
    querying with the suffix intact returns nothing at all, even for a
    company with a real Wikipedia infobox."""
    seen_queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(dict(request.url.params)["q"])
        return httpx.Response(200, json=_ddg_response("equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys())

    assert seen_queries == ["EQUINOR"]


def test_discovers_website_from_infobox():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "duckduckgo.com" in str(request.url)
        return httpx.Response(200, json=_ddg_response("equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys())

    assert candidate == "https://equinor.com"


def test_accepts_a_202_response_with_a_valid_body():
    """Real-world regression: DuckDuckGo's Instant Answer API sometimes
    returns 202 Accepted (not 200) for a perfectly valid, full response
    body -- must not be treated as a failure."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(202, json=_ddg_response("equinor.com"))))
    budget = BudgetGovernor()

    assert discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys()) == "https://equinor.com"


def test_retries_once_on_an_empty_body_before_giving_up():
    """Real-world regression: DuckDuckGo occasionally returns a 200 with a
    completely empty body for a query that works moments later -- worth one
    retry before concluding there's genuinely no result."""
    responses = [httpx.Response(200, content=b""), httpx.Response(200, json=_ddg_response("equinor.com"))]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    assert discover_candidate_site("EQUINOR ASA", client, budget, sleep=lambda s: None, **_no_keys()) == "https://equinor.com"


def test_no_infobox_returns_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ddg_response(None))))
    budget = BudgetGovernor()

    assert discover_candidate_site("SOME SMALL COMPANY AS", client, budget, **_no_keys()) is None


def test_discovery_respects_budget():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ddg_response("equinor.com"))))
    budget = BudgetGovernor(BudgetLimits(max_requests=0, max_spend_usd=10, max_wall_clock_seconds=1000))

    assert discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys()) is None


def test_discovery_handles_network_failure_gracefully():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    assert discover_candidate_site("EQUINOR ASA", client, budget, sleep=lambda s: None, **_no_keys()) is None


def test_verify_discovered_site_accepts_matching_name():
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Equinor ASA"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget, now=lambda: NOW)

    assert confirmed is not None
    assert confirmed.field_name == "official_site"
    assert confirmed.value == "https://equinor.com"
    assert confirmed.source_class == "external"
    assert confirmed.extraction_method == "discovery"


def test_verify_discovered_site_rejects_mismatched_name():
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Totally Different Company AS"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget, now=lambda: NOW)

    assert confirmed is None


def test_verify_discovered_site_falls_back_to_page_text_when_no_json_ld():
    html = "<html><body><p>Equinor ASA is a Norwegian energy company.</p></body></html>"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget, now=lambda: NOW)

    assert confirmed is not None


def test_verify_discovered_site_handles_too_many_redirects_gracefully():
    """Real-world regression, found running the real 1,000-company batch
    with live Exa/Parallel discovery: a discovered candidate can redirect in
    a loop. httpx.TooManyRedirects is an httpx.HTTPError, not an
    httpx.TransportError -- the same exception-type gap already fixed once
    for crawl.py's malformed-redirect case, just not carried over here. Must
    degrade to None, not propagate past this function and lose the whole
    company to the runner's outer safety net."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget, sleep=lambda s: None)

    assert confirmed is None


def test_verify_discovered_site_respects_budget():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>")))
    budget = BudgetGovernor(BudgetLimits(max_requests=0, max_spend_usd=10, max_wall_clock_seconds=1000))

    assert verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget) is None


# --- Exa (primary provider) ---


def _exa_response(url: str | None) -> dict:
    results = [{"title": "Company", "url": url}] if url else []
    return {"requestId": "req-1", "results": results}


def test_exa_is_tried_first_when_key_present():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers.get("x-api-key") == "fake-exa-key"
        return httpx.Response(200, json=_exa_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate == "https://equinor.com"
    assert calls == ["https://api.exa.ai/search"]


def test_skips_non_webpage_results_like_pdfs():
    """Real-world regression: a real Parallel query for a real company's
    official site returned a PDF annual report as the top result, not the
    homepage. verify_discovered_site correctly rejected it (extruct/
    trafilatura can't extract an org name from a PDF), but that wastes a
    verify-fetch on something that could never work -- skip obviously
    non-webpage results (by file extension) before returning a candidate at
    all, and fall through to the next result instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "requestId": "req-1",
                "results": [
                    {"title": "Annual Report", "url": "https://example.com/reports/annual-2023.pdf"},
                    {"title": "Logo", "url": "https://example.com/assets/logo.png"},
                    {"title": "Homepage", "url": "https://example.com/"},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EXAMPLE AS", client, budget, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate == "https://example.com/"


def test_exa_no_results_returns_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_exa_response(None))))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "SOME SMALL COMPANY AS", client, budget, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate is None


def test_exa_network_failure_does_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, sleep=lambda s: None, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate is None


def test_exa_skipped_entirely_without_a_key():
    """No EXA_API_KEY -- must not attempt a request to Exa's endpoint at all."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_ddg_response(None))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys())

    assert not any("exa.ai" in u for u in calls)


# --- Parallel (fallback provider) ---


def _parallel_response(url: str | None) -> dict:
    results = [{"title": "Company", "url": url, "excerpts": ["..."]}] if url else []
    return {"search_id": "s-1", "results": results}


def test_parallel_is_tried_when_exa_has_no_key():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers.get("x-api-key") == "fake-parallel-key"
        return httpx.Response(200, json=_parallel_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key=None, parallel_api_key="fake-parallel-key"
    )

    assert candidate == "https://equinor.com"
    assert calls == ["https://api.parallel.ai/v1/search"]


def test_parallel_request_excludes_aggregator_domains_and_requests_multiple_results():
    """Parallel's advanced_settings.source_policy.exclude_domains and
    advanced_settings.max_results are used natively, matching the same
    aggregator blacklist principle already applied elsewhere in the project
    (proff.no/purehelp.no were already excluded from direct crawling for the
    same reason). Nesting confirmed against the real live API -- a flatter
    `advanced_settings.exclude_domains` 422s with "Extra inputs are not
    permitted"."""
    seen_body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=_parallel_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, exa_api_key=None, parallel_api_key="fake-parallel-key")

    advanced = seen_body.get("advanced_settings", {})
    excluded = set(advanced.get("source_policy", {}).get("exclude_domains", []))
    assert {"proff.no", "purehelp.no", "facebook.com", "linkedin.com", "wikipedia.org"} <= excluded
    assert advanced.get("max_results", 0) >= 5


def test_parallel_is_tried_when_exa_yields_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        if "exa.ai" in str(request.url):
            return httpx.Response(200, json=_exa_response(None))
        return httpx.Response(200, json=_parallel_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key"
    )

    assert candidate == "https://equinor.com"


def test_parallel_not_tried_when_exa_already_found_something():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_exa_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key"
    )

    assert not any("parallel.ai" in u for u in calls)


def test_duckduckgo_is_last_resort_when_both_paid_providers_yield_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "exa.ai" in url:
            return httpx.Response(200, json=_exa_response(None))
        if "parallel.ai" in url:
            return httpx.Response(200, json=_parallel_response(None))
        return httpx.Response(200, json=_ddg_response("equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key"
    )

    assert candidate == "https://equinor.com"


def test_all_three_providers_absent_or_empty_returns_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ddg_response(None))))
    budget = BudgetGovernor()

    assert discover_candidate_site("SOME SMALL COMPANY AS", client, budget, **_no_keys()) is None


def test_only_attempted_providers_spend_budget():
    """Exa succeeds on the first try -- Parallel and DuckDuckGo must never be
    attempted, so only 1 request should be spent, not 3."""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_exa_response("https://equinor.com")))
    )
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key")

    assert budget.requests_used == 1
