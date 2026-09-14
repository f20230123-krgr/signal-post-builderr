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
from src.pipeline.discovery import MAX_FETCH_RETRIES, discover_candidate_site, strip_leader_role_title, verify_discovered_site

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


def test_verify_discovered_site_rejects_a_url_that_embeds_a_different_companys_org_number():
    """Real-world regression, confirmed in a real 1,000-company batch: org
    836296222 ("GRE HOLDING AS") was linked to
    https://21st.ai/en/companies/306155/no/oslo/-/org-number-929396855-greve-holding-as/summary
    -- a page for a DIFFERENT company, org 929396855 ("GREVE HOLDING AS").
    Name-similarity alone can't reliably catch this: RapidFuzz scores "GRE
    HOLDING AS" vs "GREVE HOLDING AS" at 92-93 under every metric tested
    (partial_ratio, token_sort_ratio, token_set_ratio, WRatio), all above
    NAME_MATCH_THRESHOLD=90 -- short, generic Norwegian holding-company names
    are inherently prone to this. But the candidate URL itself embeds the
    OTHER company's real 9-digit Norwegian org number, which is a much more
    reliable, domain-agnostic signal (works even on a directory site not yet
    in AGGREGATOR_DOMAIN_BLACKLIST): if a URL contains a 9-digit sequence
    that isn't this entity's own org number, it belongs to someone else."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Greve Holding AS"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://21st.ai/en/companies/306155/no/oslo/-/org-number-929396855-greve-holding-as/summary",
        "GRE HOLDING AS",
        client,
        budget,
        org_number="836296222",
    )

    assert confirmed is None


def test_verify_discovered_site_accepts_a_url_that_embeds_this_companys_own_org_number():
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Equinor ASA"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.equinor.com/about/org-number-923609016",
        "EQUINOR ASA",
        client,
        budget,
        org_number="923609016",
    )

    assert confirmed is not None


def test_verify_discovered_site_with_no_org_number_mismatch_signal_still_works_as_before():
    """A candidate URL with no embedded 9-digit number at all (the normal
    case -- most homepages are just a bare domain) must be unaffected."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Equinor ASA"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://www.equinor.com", "EQUINOR ASA", client, budget, org_number="923609016")

    assert confirmed is not None


def test_verify_discovered_site_respects_budget():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>")))
    budget = BudgetGovernor(BudgetLimits(max_requests=0, max_spend_usd=10, max_wall_clock_seconds=1000))

    assert verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget) is None


def test_1881_no_is_blacklisted():
    """Real-world regression, found via independent review of a real
    100-company batch: org 816981522 (BERATO INVEST AS, no site of its own)
    was attributed 1881.no -- a Norwegian phone/business-directory site --
    as its official_website, and subsequent crawling of that shared
    directory domain scraped OTHER, wholly unrelated businesses' own
    listing pages (ama-trykkluft-as, advokatfellesskapet-drift-as,
    soermaskin-swt-as, ...) as if they were BERATO INVEST AS's own dated
    activity. Same directory-domain class as rosa.no/regnskapstall.no."""
    from src.pipeline.discovery import _is_blacklisted_domain

    assert _is_blacklisted_domain("https://www.1881.no/tlf/example-as/123456789")


def test_generate_no_is_blacklisted():
    """Real-world regression, found auditing a real 1,000-company batch and
    confirmed against the live Brreg API and the frozen universe file: 88
    unrelated shell/holding companies (all with NO registered website in
    either source -- e.g. org 818842422 ARTING EIENDOM AS, hjemmeside=None,
    universe website="") were all attributed "https://www.generate.no" as
    their official_website, and it cascaded into repeated duplicate
    company_profile facts too once the pipeline treated it as that entity's
    confirmed domain. generate.no lists/manages many unrelated companies
    (a company-formation/administration service), so it belongs on the same
    blacklist as rosa.no/21st.ai/etc, not treated as anyone's own site."""
    from src.pipeline.discovery import _is_blacklisted_domain

    assert _is_blacklisted_domain("https://www.generate.no")
    assert _is_blacklisted_domain("https://www.generate.no/about")


def test_verify_discovered_site_rejects_a_blacklisted_directory_domain_even_when_the_name_matches_exactly():
    """Real-world regression, found auditing a real 1,000-company batch:
    business-directory/aggregator pages (e.g. rosa.no, regnskapstall.no,
    virksomhet.brreg.no -- a government registry lookup page, not the
    company's own site) always echo the exact legal name verbatim, so they
    trivially clear NAME_MATCH_THRESHOLD and get published as
    "official_website" -- ~40% of discovered sites in that batch were one of
    these, none of them the company's actual site. A directory hit must be
    rejected on domain alone, before ever spending a request to fetch it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a blacklisted domain at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.rosa.no/tlf/equinor-asa/923609016", "EQUINOR ASA", client, budget
    )

    assert confirmed is None
    assert budget.requests_used == 0


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


def test_exa_skips_blacklisted_aggregator_domains_and_falls_through_to_the_next_result():
    """Real-world regression: unlike Parallel, Exa's request body has no
    exclude-domains parameter at all, and Exa is tried FIRST in the provider
    chain -- so a directory/aggregator hit from Exa sailed straight through
    with zero filtering. _first_webpage_url must reject a blacklisted domain
    client-side (defense in depth, not dependent on any one provider's own
    exclusion feature) and try the next result instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "requestId": "req-1",
                "results": [
                    {"title": "Directory listing", "url": "https://www.rosa.no/tlf/example-as/923609016"},
                    {"title": "Government registry lookup", "url": "https://virksomhet.brreg.no/nb/oppslag/enheter/923609016"},
                    {"title": "Homepage", "url": "https://example.no/"},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EXAMPLE AS", client, budget, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate == "https://example.no/"


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


def test_exa_402_payment_required_is_not_retried_and_falls_through_immediately():
    """Real-world regression, confirmed live against the real Exa API mid-
    session: a 402 Payment Required (quota exhausted) is a PERMANENT
    failure for the rest of the billing period, not a transient one --
    unlike a 5xx or network blip, retrying it can never succeed. The old
    code retried it anyway (MAX_FETCH_RETRIES times, with backoff sleep),
    wasting real wall-clock time on a call already known to be doomed, and
    still spent a full unit of request budget per attempt. With three
    discovery tiers (legal name, leader name, org number) each hitting Exa
    first, that waste multiplied 3x per company across a real 1,000-company
    batch -- a large factor in why that run took ~78 minutes instead of the
    usual ~22. Must call the endpoint exactly ONCE, not MAX_FETCH_RETRIES
    times, and fall through to Parallel immediately."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "exa.ai" in str(request.url):
            return httpx.Response(402)
        return httpx.Response(200, json=_parallel_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, sleep=lambda s: None,
        exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key",
    )

    exa_calls = [c for c in calls if "exa.ai" in c]
    assert len(exa_calls) == 1
    assert candidate == "https://equinor.com"


def test_exa_401_and_403_are_also_not_retried():
    for status in (401, 403):
        calls = []

        def handler(request: httpx.Request, status=status) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(status)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        budget = BudgetGovernor()

        discover_candidate_site(
            "EQUINOR ASA", client, budget, sleep=lambda s: None, exa_api_key="fake-exa-key", parallel_api_key=None
        )

        exa_calls = [c for c in calls if "exa.ai" in c]
        assert len(exa_calls) == 1, f"status {status} was retried"


def test_5xx_and_network_errors_are_still_retried():
    """Backward-compatible: a genuinely transient failure (server error,
    transport blip) must still get the existing retry behavior -- only
    permanent auth/payment failures skip retries."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site(
        "EQUINOR ASA", client, budget, sleep=lambda s: None, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    exa_calls = [c for c in calls if "exa.ai" in c]
    assert len(exa_calls) == MAX_FETCH_RETRIES


def test_explicit_none_key_is_never_overridden_by_the_real_environment(monkeypatch):
    """Real-world regression: exa_api_key=None must mean "this provider is
    explicitly disabled," never "not specified, fall back to the real
    environment." A previous version of discover_candidate_site treated an
    explicit None the same as "not provided" and silently read os.environ
    instead -- invisible in a dev session where the env var wasn't yet
    inherited by the running process, but a real problem once a persistent
    setx-set key naturally propagates to a fresh session."""
    monkeypatch.setenv("EXA_API_KEY", "a-real-key-that-must-be-ignored")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_ddg_response(None))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys())

    assert not any("exa.ai" in u for u in calls)


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


# --- Leader/founder bridge (agent playbook §2, learning harness §1) ---
#
# The retry-with-a-leader-name orchestration lives in runner.py, not here --
# see discover_candidate_site's and strip_leader_role_title's docstrings for
# why (a real, measured bug: retrying only when THIS function finds nothing
# misses the common case where it finds something that then fails
# verify_discovered_site's identity check). This module just provides the
# name-formatting helper and stays a plain single-query function.


def test_strip_leader_role_title_removes_the_parenthetical():
    assert strip_leader_role_title("Anders Opedal (Daglig leder)") == "Anders Opedal"


def test_strip_leader_role_title_leaves_a_bare_name_unchanged():
    assert strip_leader_role_title("Anders Opedal") == "Anders Opedal"


def test_only_attempted_providers_spend_budget():
    """Exa succeeds on the first try -- Parallel and DuckDuckGo must never be
    attempted, so only 1 request should be spent, not 3."""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_exa_response("https://equinor.com")))
    )
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key")

    assert budget.requests_used == 1
