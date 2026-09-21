"""
Tests for src/orchestrator/runner.py -- see docs/component-specs.md.

test_mixed_success_failure_batch_still_yields_100_results is THE
"exactly 100 terminal results" hard-gate test. Do not weaken or skip it once
implemented.
"""
import asyncio
import threading
import time
from datetime import datetime, timezone

import httpx
import pytest

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.orchestrator.runner import DeadProviderAbort, _default_process_one, run_batch, run_in_chunks
from src.pipeline.discovery import ProviderHealth
from src.storage.snapshots import SnapshotStore
from tests.conftest import make_profile


def test_mixed_success_failure_batch_still_yields_100_results():
    org_numbers = [f"{i:09d}" for i in range(100)]

    def process_one(org_number, budget, previous_snapshot=None):
        if int(org_number) % 3 == 0:
            raise RuntimeError(f"simulated stage crash for {org_number}")
        return make_profile(org_number=org_number, legal_name=None, is_first_run=True)

    budget = BudgetGovernor()
    profiles = asyncio.run(run_batch(org_numbers, budget, process_one=process_one, concurrency=10))

    assert len(profiles) == 100
    assert {p.org_number for p in profiles} == set(org_numbers)

    by_org = {p.org_number: p for p in profiles}
    for org_number in org_numbers:
        profile = by_org[org_number]
        if int(org_number) % 3 == 0:
            assert profile.legal_identity.legal_name.state == EvidenceState.FAILED
        else:
            assert profile.refresh_metadata.is_first_run is True


def test_concurrency_cap_is_respected_under_load():
    active = 0
    max_active = 0
    lock = threading.Lock()

    def process_one(org_number, budget, previous_snapshot=None):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return make_profile(org_number=org_number)

    org_numbers = [f"{i:09d}" for i in range(20)]
    budget = BudgetGovernor()

    profiles = asyncio.run(run_batch(org_numbers, budget, process_one=process_one, concurrency=4))

    assert len(profiles) == 20
    assert max_active <= 4
    assert max_active >= 2  # actually running concurrently, not serialized


def test_snapshot_store_is_used_for_refresh_when_provided(tmp_path):
    store = SnapshotStore(tmp_path)
    previous = make_profile(
        org_number="923609016",
        run_timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        is_first_run=True,
    )
    store.append(previous)

    seen_previous = {}

    def process_one(org_number, budget, previous_snapshot=None):
        seen_previous[org_number] = previous_snapshot
        return make_profile(org_number=org_number, is_first_run=previous_snapshot is None)

    budget = BudgetGovernor()
    profiles = asyncio.run(
        run_batch(["923609016"], budget, process_one=process_one, snapshot_store=store, concurrency=2)
    )

    assert seen_previous["923609016"] is not None
    assert seen_previous["923609016"].run_timestamp == previous.run_timestamp
    assert len(store.history("923609016")) == 2
    assert profiles[0].org_number == "923609016"


def test_run_batch_passes_matching_universe_entry_to_default_process_one():
    seen_entries = {}

    def process_one(org_number, budget, previous_snapshot=None, universe_entry=None):
        seen_entries[org_number] = universe_entry
        return make_profile(org_number=org_number)

    universe = {"923609016": {"organisation_number": "923609016", "name": "EQUINOR ASA", "website": "www.equinor.com"}}
    budget = BudgetGovernor()

    asyncio.run(
        run_batch(
            ["923609016", "000000001"],
            budget,
            process_one=process_one,
            universe=universe,
            concurrency=2,
        )
    )

    assert seen_entries["923609016"] == universe["923609016"]
    assert seen_entries["000000001"] is None


def test_default_process_one_resolves_identity_from_the_universe_not_a_registry_lookup():
    """Identity still comes from the frozen manifest, never from a live
    lookup -- that is what makes it free and byte-identical for every
    entrant.

    The entity endpoint IS hit at most once per company now, deliberately:
    fetch_live_registry_details reads the founding date and former names,
    which the manifest doesn't carry (measured: founding date 40/40, former
    names 11/40). That one extra call must not become the source of identity,
    which is what this pins -- legal_name is still sourced from the manifest
    even here, where that call returns nothing usable."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(404)  # entity details/roller/regnskap/underenheter: no extra data

    client = httpx.Client(transport=httpx.MockTransport(handler))
    universe_entry = {"organisation_number": "923609016", "name": "EQUINOR ASA", "website": "www.equinor.com"}

    profile = _default_process_one(
        "923609016", BudgetGovernor(), previous_snapshot=None, universe_entry=universe_entry, client=client
    )

    assert profile.legal_identity.legal_name.value == "EQUINOR ASA"
    assert profile.legal_identity.legal_name.source == "signalpost-company-universe-2025.jsonl.gz"
    entity_lookups = [c for c in calls if c == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"]
    assert len(entity_lookups) <= 1, f"expected at most the details fetch, got {len(entity_lookups)}"


def test_run_in_chunks_processes_every_company_across_multiple_chunks():
    org_numbers = [f"{i:09d}" for i in range(10)]

    def process_one(org_number, budget, previous_snapshot=None, universe_entry=None):
        return make_profile(org_number=org_number)

    profiles, report = asyncio.run(
        run_in_chunks(org_numbers, chunk_size=4, process_one=process_one, concurrency=10)
    )

    assert len(profiles) == 10
    assert {p.org_number for p in profiles} == set(org_numbers)
    assert report["chunk_count"] == 3  # 4 + 4 + 2


def test_run_in_chunks_gives_each_chunk_a_fresh_budget():
    """A company blocked by budget exhaustion in one chunk must not affect
    the next chunk -- each 100-company daily batch gets its own fresh
    2000-request/45-minute envelope in the real evaluation."""
    seen_budgets = []

    def process_one(org_number, budget, previous_snapshot=None, universe_entry=None):
        budget.record_request()
        seen_budgets.append((org_number, budget.can_spend_request()))
        return make_profile(org_number=org_number)

    org_numbers = [f"{i:09d}" for i in range(4)]
    limits = BudgetLimits(max_requests=1, max_spend_usd=10, max_wall_clock_seconds=1000)

    profiles, report = asyncio.run(
        run_in_chunks(org_numbers, chunk_size=2, process_one=process_one, budget_limits=limits, concurrency=1)
    )

    assert len(profiles) == 4
    # every company saw a budget that had exactly 1 request left when it ran
    # (fresh per chunk, not shared/exhausted across chunks)
    assert all(can_spend is False for _, can_spend in seen_budgets)
    assert report["requests_used"] == 4  # 1 per company, none blocked by a stale budget


def test_run_in_chunks_reports_aggregate_operations():
    org_numbers = [f"{i:09d}" for i in range(6)]

    def process_one(org_number, budget, previous_snapshot=None, universe_entry=None):
        budget.record_request()
        budget.record_spend(0.01)
        return make_profile(org_number=org_number)

    profiles, report = asyncio.run(run_in_chunks(org_numbers, chunk_size=3, process_one=process_one, concurrency=3))

    assert report["requests_used"] == 6
    assert round(report["spend_used_usd"], 2) == 0.06
    assert report["chunk_count"] == 2


def test_default_process_one_uses_sitemap_ats_and_cache_discovery(tmp_path):
    """Wiring test: _default_process_one must actually pass the new
    discovery options through to crawl(), not just have them exist as
    unused parameters."""
    from src.storage.cache import ResponseCache

    sitemap_xml = '<urlset><url><loc>https://example.com/careers</loc></url></urlset>'
    careers_html = '<html><body>Careers. <a href="https://boards.greenhouse.io/examplecorp">Jobs</a></body></html>'
    call_counts: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        call_counts[url] = call_counts.get(url, 0) + 1
        if url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(
                200,
                json={
                    "organisasjonsnummer": "923609016",
                    "navn": "EXAMPLE CORP AS",
                    "hjemmeside": "example.com",
                    "historiskeNavn": [],
                },
            )
        if url == "https://example.com/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        if url == "https://example.com/sitemap.xml":
            return httpx.Response(200, text=sitemap_xml)
        if url == "https://example.com/careers":
            return httpx.Response(200, text=careers_html)
        if url == "https://boards.greenhouse.io/examplecorp":
            return httpx.Response(
                200,
                text='<html><head><script type="application/ld+json">'
                '{"@type":"JobPosting","title":"Backend Engineer, Oslo","datePosted":"2026-06-20"}'
                "</script></head></html>",
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cache = ResponseCache(tmp_path / "cache.sqlite3")
    fixed_now = lambda: datetime(2026, 9, 8, tzinfo=timezone.utc)

    profile = _default_process_one("923609016", BudgetGovernor(), client=client, cache=cache, now=fixed_now)

    assert profile.legal_identity.legal_name.value == "EXAMPLE CORP AS"
    # reached via sitemap discovery -> ATS link-following, not the fixed
    # company_owned_paths list alone
    assert any("greenhouse.io" in c.source for c in profile.activity.hiring_signals if c.source)

    counts_after_first_run = dict(call_counts)

    # Second run, same cache, same date -- every page fetch should be served
    # from cache, not the network, per "cache hits free".
    _default_process_one("923609016", BudgetGovernor(), client=client, cache=cache, now=fixed_now)

    page_urls = {"https://example.com/careers", "https://example.com/robots.txt", "https://example.com/sitemap.xml"}
    for url in page_urls:
        assert call_counts.get(url, 0) == counts_after_first_run.get(url, 0), f"{url} was re-fetched instead of served from cache"


def test_default_process_one_uses_search_discovery_when_no_registry_website():
    """Wiring test: when the registry has no website on file, discovery.py's
    DuckDuckGo-backed candidate search must be tried, and a verified
    candidate must be crawled/used like any other official site."""
    ddg_response = {
        "Heading": "Example Corp",
        "Infobox": {"content": [{"label": "Website", "value": "[example.com]"}]},
    }
    org_page_html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"EXAMPLE CORP AS"}</script></head><body>We are hiring: Engineer</body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(
                200,
                json={
                    "organisasjonsnummer": "923609016",
                    "navn": "EXAMPLE CORP AS",
                    "historiskeNavn": [],
                    # no "hjemmeside" -- no website on file
                },
            )
        if "duckduckgo.com" in url:
            return httpx.Response(200, json=ddg_response)
        if url in ("https://example.com", "https://example.com/", "https://example.com/robots.txt"):
            return httpx.Response(200, text=org_page_html)
        return httpx.Response(404)  # sitemap.xml, company_owned_paths etc -- fine to be absent

    client = httpx.Client(transport=httpx.MockTransport(handler))

    profile = _default_process_one("923609016", BudgetGovernor(), client=client)

    assert profile.online_presence.official_site.value in ("https://example.com", "https://example.com/")
    assert profile.online_presence.official_site.source_class == "external"


def test_default_process_one_falls_back_to_leader_name_when_company_name_search_finds_nothing():
    """End-to-end wiring test for the leader/founder bridge (agent playbook
    §2): a generic legal name search finds nothing, but the verified CEO
    name (fetched from Brreg's roller endpoint before discovery runs)
    succeeds. Confirms fetch_leadership_only is actually wired into
    _default_process_one, not just unit-tested in isolation."""
    roller_response = {
        "rollegrupper": [
            {
                "type": {"kode": "DAGL"},
                "roller": [
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                        "person": {"navn": {"fornavn": "Anders", "etternavn": "Opedal"}},
                    }
                ],
            }
        ]
    }
    org_page_html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"GENERIC HOLDING AS"}</script></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(
                200,
                json={"organisasjonsnummer": "923609016", "navn": "GENERIC HOLDING AS", "historiskeNavn": []},
            )
        if url.endswith("/roller"):
            return httpx.Response(200, json=roller_response)
        if "duckduckgo.com" in url:
            query = dict(request.url.params).get("q", "")
            if "Opedal" in query:
                return httpx.Response(
                    200,
                    json={"Heading": "x", "Infobox": {"content": [{"label": "Website", "value": "[example.com]"}]}},
                )
            return httpx.Response(200, json={"Heading": "x", "Infobox": None})
        if url in ("https://example.com", "https://example.com/", "https://example.com/robots.txt"):
            return httpx.Response(200, text=org_page_html)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    profile = _default_process_one("923609016", BudgetGovernor(), client=client)

    assert profile.online_presence.official_site.value in ("https://example.com", "https://example.com/")
    assert profile.online_presence.official_site.source_class == "external"


def test_default_process_one_falls_back_to_leader_name_when_company_name_candidate_fails_verification():
    """Real-world regression, found by re-measuring on a real unseen batch:
    an earlier version of this fallback lived entirely inside
    discover_candidate_site and only retried when it found NOTHING at all --
    missing the far more common case where the legal-name search finds SOME
    candidate that then fails verify_discovered_site's identity check
    (wrong company, unrelated page). Measured literally zero coverage
    improvement on 92 real companies until fixed. This test pins the fix:
    the company-name search finds a real page for a DIFFERENT company
    (fails verification), and only the leader-name retry finds the actual
    right site."""
    roller_response = {
        "rollegrupper": [
            {
                "type": {"kode": "DAGL"},
                "roller": [
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                        "person": {"navn": {"fornavn": "Anders", "etternavn": "Opedal"}},
                    }
                ],
            }
        ]
    }
    wrong_company_html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"TOTALLY UNRELATED AS"}</script></head></html>'
    right_company_html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"GENERIC HOLDING AS"}</script></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(
                200,
                json={"organisasjonsnummer": "923609016", "navn": "GENERIC HOLDING AS", "historiskeNavn": []},
            )
        if url.endswith("/roller"):
            return httpx.Response(200, json=roller_response)
        if "duckduckgo.com" in url:
            query = dict(request.url.params).get("q", "")
            if "Opedal" in query:
                return httpx.Response(
                    200,
                    json={"Heading": "x", "Infobox": {"content": [{"label": "Website", "value": "[right.example]"}]}},
                )
            # legal name alone finds a real page -- for the WRONG company
            return httpx.Response(
                200,
                json={"Heading": "x", "Infobox": {"content": [{"label": "Website", "value": "[wrong.example]"}]}},
            )
        if url in ("https://wrong.example", "https://wrong.example/"):
            return httpx.Response(200, text=wrong_company_html)
        if url in ("https://right.example", "https://right.example/", "https://right.example/robots.txt"):
            return httpx.Response(200, text=right_company_html)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    profile = _default_process_one("923609016", BudgetGovernor(), client=client)

    assert profile.online_presence.official_site.value in ("https://right.example", "https://right.example/")
    assert profile.online_presence.official_site.source_class == "external"


def test_default_process_one_falls_back_to_org_number_search_when_leader_name_also_fails():
    """End-to-end wiring test for the third discovery fallback tier: an
    exact 9-digit org number is a much stronger match signal than a fuzzy
    name (Norwegian company/business directories, chambers and official
    filings commonly cite it verbatim), so it's tried as a last-resort query
    seed after BOTH the legal name and the leader name fail to produce a
    verified site."""
    roller_response = {
        "rollegrupper": [
            {
                "type": {"kode": "DAGL"},
                "roller": [
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                        "person": {"navn": {"fornavn": "Anders", "etternavn": "Opedal"}},
                    }
                ],
            }
        ]
    }
    right_company_html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"GENERIC HOLDING AS"}</script></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(
                200,
                json={"organisasjonsnummer": "923609016", "navn": "GENERIC HOLDING AS", "historiskeNavn": []},
            )
        if url.endswith("/roller"):
            return httpx.Response(200, json=roller_response)
        if "duckduckgo.com" in url:
            query = dict(request.url.params).get("q", "")
            if "923609016" in query:
                return httpx.Response(
                    200,
                    json={"Heading": "x", "Infobox": {"content": [{"label": "Website", "value": "[org-number.example]"}]}},
                )
            # neither the legal name nor the leader name find anything
            return httpx.Response(200, json={"Heading": "x", "Infobox": None})
        if url in ("https://org-number.example", "https://org-number.example/", "https://org-number.example/robots.txt"):
            return httpx.Response(200, text=right_company_html)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    profile = _default_process_one("923609016", BudgetGovernor(), client=client)

    assert profile.online_presence.official_site.value in ("https://org-number.example", "https://org-number.example/")
    assert profile.online_presence.official_site.source_class == "external"


# --- Stop if a discovery key is dead (opt-in, local testing only) ---


def test_stop_if_key_dead_aborts_before_processing_anything_when_a_key_is_already_dead():
    health = ProviderHealth()
    health.disable("exa", "HTTP 402 (exhausted credits or invalid key)")
    processed = []

    def process_one(org_number, budget, previous_snapshot=None):
        processed.append(org_number)
        return make_profile(org_number=org_number)

    with pytest.raises(DeadProviderAbort) as excinfo:
        asyncio.run(
            run_batch(
                ["000000001", "000000002"], BudgetGovernor(), process_one=process_one,
                provider_health=health, stop_if_key_dead=True,
            )
        )

    assert processed == []
    assert "exa" in excinfo.value.dead_providers


def test_stop_if_key_dead_aborts_when_a_key_dies_mid_run(tmp_path):
    """The company whose run killed the key was processed with degraded
    discovery -- it must not be written to snapshot history, and nothing
    after it may start."""
    health = ProviderHealth()
    store = SnapshotStore(tmp_path / "snapshots")
    processed = []

    def process_one(org_number, budget, previous_snapshot=None):
        processed.append(org_number)
        health.disable("exa", "HTTP 402 (exhausted credits or invalid key)")
        return make_profile(org_number=org_number)

    with pytest.raises(DeadProviderAbort):
        asyncio.run(
            run_batch(
                ["000000001", "000000002", "000000003"], BudgetGovernor(), process_one=process_one,
                snapshot_store=store, provider_health=health, stop_if_key_dead=True, concurrency=1,
            )
        )

    assert processed == ["000000001"]
    assert store.latest("000000001") is None


def test_without_the_flag_a_dead_key_never_costs_terminal_results():
    """The default is what the graded run uses: a dead key must never reduce
    the number of profiles produced (exactly-N terminal results hard gate)."""
    health = ProviderHealth()
    health.disable("exa", "HTTP 402 (exhausted credits or invalid key)")

    def process_one(org_number, budget, previous_snapshot=None):
        return make_profile(org_number=org_number)

    org_numbers = [f"{i:09d}" for i in range(5)]
    profiles = asyncio.run(run_batch(org_numbers, BudgetGovernor(), process_one=process_one, provider_health=health))

    assert len(profiles) == 5


def test_run_in_chunks_honours_stop_if_key_dead():
    health = ProviderHealth()
    health.disable("parallel", "HTTP 401 (exhausted credits or invalid key)")

    def process_one(org_number, budget, previous_snapshot=None):
        raise AssertionError("must not process any company")

    with pytest.raises(DeadProviderAbort):
        asyncio.run(
            run_in_chunks(
                ["000000001"], process_one=process_one, provider_health=health, stop_if_key_dead=True,
            )
        )


# --- Former-name search tier and NAV index wiring ---


def _former_name_handler(name_holders, site_html_by_url):
    """Registry + DuckDuckGo mock: company GENERIC HOLDING AS (923609016) was
    renamed from OLD BRAND AS; only the former-name search finds a site."""
    def handler(request):
        url = str(request.url)
        if url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(200, json={
                "organisasjonsnummer": "923609016", "navn": "GENERIC HOLDING AS",
                "historiskeNavn": [{"navn": "OLD BRAND AS", "fraDato": "2015-01-01 00:00:00",
                                    "tilDato": "2024-03-01 00:00:00"}],
            })
        if request.url.path == "/enhetsregisteret/api/enheter" and "navn" in request.url.params:
            return httpx.Response(200, json={"_embedded": {"enheter": name_holders}})
        if "duckduckgo.com" in url:
            if "OLD BRAND" in dict(request.url.params).get("q", ""):
                return httpx.Response(200, json={"Heading": "x", "Infobox": {"content": [
                    {"label": "Website", "value": "[oldbrand.example]"}]}})
            return httpx.Response(200, json={"Heading": "x", "Infobox": None})
        for site, html in site_html_by_url.items():
            if url.rstrip("/") == site or url == site + "/robots.txt":
                return httpx.Response(200, text=html)
        return httpx.Response(404)
    return handler


_OLD_BRAND_HTML = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Old Brand AS"}</script></head></html>'


def test_former_name_tier_finds_a_site_still_running_under_the_old_name(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(
        _former_name_handler(name_holders=[], site_html_by_url={"https://oldbrand.example": _OLD_BRAND_HTML})
    ))

    profile = _default_process_one("923609016", BudgetGovernor(), client=client)

    assert profile.online_presence.official_site.value in ("https://oldbrand.example", "https://oldbrand.example/")
    assert any("Renamed from 'OLD BRAND AS'" in c.value for c in profile.activity.dated_activity)


def test_former_name_tier_is_skipped_when_another_company_now_holds_that_name(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(_former_name_handler(
        name_holders=[{"organisasjonsnummer": "555555555", "navn": "OLD BRAND AS"}],
        site_html_by_url={"https://oldbrand.example": _OLD_BRAND_HTML},
    )))

    profile = _default_process_one("923609016", BudgetGovernor(), client=client)

    assert profile.online_presence.official_site.value is None


def test_nav_job_index_is_built_once_per_run_not_once_per_chunk(monkeypatch):
    """The feed is the same for every company: a 1,000-company run pays for
    it once, on the first chunk's budget."""
    import src.orchestrator.runner as runner_module
    from src.pipeline.nav_jobs import NavJobIndex

    builds = []

    def fake_build(client, budget, now=None):
        builds.append(budget)
        return NavJobIndex(token="t")

    monkeypatch.setattr(runner_module, "build_nav_job_index", fake_build)

    def process_one(org_number, budget, previous_snapshot=None):
        return make_profile(org_number=org_number)

    org_numbers = [f"{i:09d}" for i in range(6)]
    _, report = asyncio.run(run_in_chunks(org_numbers, chunk_size=2, process_one=process_one, use_nav_jobs=True))

    assert len(builds) == 1
    assert report["nav_job_index"]["truncated_at_page_cap"] is False


def test_nav_jobs_are_off_unless_asked_for(monkeypatch):
    import src.orchestrator.runner as runner_module

    def fake_build(*args, **kwargs):
        raise AssertionError("NAV index must not be built unless use_nav_jobs=True")

    monkeypatch.setattr(runner_module, "build_nav_job_index", fake_build)

    def process_one(org_number, budget, previous_snapshot=None):
        return make_profile(org_number=org_number)

    _, report = asyncio.run(run_in_chunks(["000000001"], process_one=process_one))

    assert report["nav_job_index"] is None


# --- Exa limited to the first search round (opt-in spend limit) ---


def _exa_keys_per_round(monkeypatch, exa_first_round_only):
    import src.orchestrator.runner as runner_module

    monkeypatch.setenv("EXA_API_KEY", "fake-exa")
    monkeypatch.setenv("PARALLEL_API_KEY", "fake-parallel")
    seen = []

    def fake_discover(query_name, client, budget, **kwargs):
        seen.append(kwargs.get("exa_api_key"))
        return None

    monkeypatch.setattr(runner_module, "discover_candidate_site", fake_discover)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    universe_entry = {"organisation_number": "923609016", "name": "GENERIC HOLDING AS", "website": ""}

    _default_process_one(
        "923609016", BudgetGovernor(), universe_entry=universe_entry, client=client,
        exa_first_round_only=exa_first_round_only,
    )
    return seen


def test_exa_first_round_only_uses_exa_for_the_company_name_search_alone(monkeypatch):
    seen = _exa_keys_per_round(monkeypatch, exa_first_round_only=True)

    assert seen[0] == "fake-exa"
    assert len(seen) > 1 and all(key is None for key in seen[1:])


def test_by_default_exa_is_used_on_every_search_round(monkeypatch):
    """The default -- what an evaluator's one-command run gets."""
    seen = _exa_keys_per_round(monkeypatch, exa_first_round_only=False)

    assert len(seen) > 1 and all(key == "fake-exa" for key in seen)


# ---- real (wire) request counting across chunks --------------------------


def test_run_in_chunks_charges_real_wire_requests_not_just_logical_ones():
    """One logical fetch that really cost 7 requests (redirects + retries) must
    count as 7 against the 2,000 limit."""
    counter = {"n": 0}

    def process_one(org_number, budget, previous_snapshot=None, universe_entry=None):
        counter["n"] += 7
        budget.record_request()
        return make_profile(org_number=org_number)

    org_numbers = [f"{i:09d}" for i in range(4)]

    _, report = asyncio.run(
        run_in_chunks(
            org_numbers, chunk_size=2, process_one=process_one, concurrency=1,
            wire_counter=lambda: counter["n"],
        )
    )

    assert report["requests_used"] == 28


def test_startup_requests_are_charged_to_the_first_chunk():
    """The key check / universe download run before chunk 1 but belong to the
    same evaluated run, so the first chunk's budget must include them."""
    counter = {"n": 3}  # 3 requests already made at startup

    def process_one(org_number, budget, previous_snapshot=None, universe_entry=None):
        counter["n"] += 7
        return make_profile(org_number=org_number)

    _, report = asyncio.run(
        run_in_chunks(
            ["000000001", "000000002"], chunk_size=1, process_one=process_one, concurrency=1,
            wire_counter=lambda: counter["n"], startup_wire_requests=3,
        )
    )

    assert report["requests_used"] == (3 + 7) + 7


def test_a_chunk_is_stopped_by_real_requests_even_when_stages_under_report():
    counter = {"n": 0}
    seen = []

    def process_one(org_number, budget, previous_snapshot=None, universe_entry=None):
        seen.append(budget.can_spend_request())
        counter["n"] += 6  # each company really costs 6, but records nothing
        return make_profile(org_number=org_number)

    limits = BudgetLimits(max_requests=10, max_spend_usd=10, max_wall_clock_seconds=1000)
    asyncio.run(
        run_in_chunks(
            ["000000001", "000000002", "000000003"], chunk_size=3, process_one=process_one, concurrency=1,
            budget_limits=limits, wire_counter=lambda: counter["n"],
        )
    )

    assert seen == [True, True, False]


# ---- identity facts when the universe file is absent ---------------------

_LIVE_BODY = {
    "organisasjonsnummer": "923609016",
    "navn": "EXAMPLE CORP AS",
    "hjemmeside": "example.no",
    "historiskeNavn": [],
    "stiftelsesdato": "2010-05-01",
    "naeringskode1": {"kode": "62.010", "beskrivelse": "Programmeringstjenester"},
    "organisasjonsform": {"kode": "AS", "beskrivelse": "Aksjeselskap"},
    "antallAnsatte": 12,
    "konkurs": False,
    "underAvvikling": False,
    "underTvangsavviklingEllerTvangsopplosning": False,
}


def _live_registry_client(body, calls=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if str(request.url) == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(200, json=body)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_without_the_universe_file_identity_claims_come_from_the_live_registry_record():
    """A clean evaluator checkout has no universe file. The four identity
    claims must not silently disappear with it."""
    profile = _default_process_one(
        "923609016", BudgetGovernor(), universe_entry=None, client=_live_registry_client(_LIVE_BODY)
    )

    identity = profile.legal_identity
    assert identity.industry.value == "62.010 Programmeringstjenester"
    assert identity.employee_count.value == "12"
    assert identity.legal_form.value == "AS"
    assert identity.operating_status.value == "Active"
    assert identity.industry.source == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"
    assert identity.industry.source_class == "official_registry"


def test_without_the_universe_file_the_live_record_is_fetched_once_not_twice():
    """resolve() already downloaded the very record the details step needs;
    fetching it again would waste a request per company against the 2,000 limit."""
    calls = []

    _default_process_one(
        "923609016", BudgetGovernor(), universe_entry=None, client=_live_registry_client(_LIVE_BODY, calls)
    )

    entity_lookups = [c for c in calls if c == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"]
    assert len(entity_lookups) == 1


def test_with_the_universe_file_identity_claims_come_from_it_and_are_not_duplicated():
    universe_entry = {
        "organisation_number": "923609016", "name": "EXAMPLE CORP AS", "website": "example.no",
        "industry_code": "43.210", "industry_label": "Elektrisk installasjonsarbeid",
        "employees": 11, "legal_form": "AS", "bankrupt": False, "liquidating": False,
    }

    profile = _default_process_one(
        "923609016", BudgetGovernor(), universe_entry=universe_entry, client=_live_registry_client(_LIVE_BODY)
    )

    assert profile.legal_identity.industry.value == "43.210 Elektrisk installasjonsarbeid"
    assert profile.legal_identity.industry.source == "signalpost-company-universe-2025.jsonl.gz"  # not the live record's


def test_the_leader_name_used_for_the_search_fallback_is_fetched_once_not_twice():
    """The registry's leaders were fetched once for the search fallback and
    again for the profile: one wasted request per company that reaches the
    fallback (about three in four of those with no registered website)."""
    roller_response = {
        "rollegrupper": [
            {
                "type": {"kode": "DAGL"},
                "roller": [
                    {
                        "type": {"kode": "DAGL", "beskrivelse": "Daglig leder"},
                        "person": {"navn": {"fornavn": "Anders", "etternavn": "Opedal"}},
                    }
                ],
            }
        ]
    }
    org_page_html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"GENERIC HOLDING AS"}</script></head></html>'
    roller_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(200, json={"organisasjonsnummer": "923609016", "navn": "GENERIC HOLDING AS", "historiskeNavn": []})
        if url.endswith("/roller"):
            roller_calls.append(url)
            return httpx.Response(200, json=roller_response)
        if "duckduckgo.com" in url:
            if "Opedal" in dict(request.url.params).get("q", ""):
                return httpx.Response(200, json={"Heading": "x", "Infobox": {"content": [{"label": "Website", "value": "[example.com]"}]}})
            return httpx.Response(200, json={"Heading": "x", "Infobox": None})
        if url in ("https://example.com", "https://example.com/", "https://example.com/robots.txt"):
            return httpx.Response(200, text=org_page_html)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    profile = _default_process_one("923609016", BudgetGovernor(), client=client)

    assert profile.online_presence.official_site.value in ("https://example.com", "https://example.com/")  # fallback still works
    assert len(roller_calls) == 1
    assert any("Opedal" in str(c.value) for c in profile.leadership.leaders)  # and the leader is still published


def test_the_employee_count_is_the_live_registry_value_when_it_is_available():
    """The universe file is a 2025 snapshot; the live record is current. This is
    also the field a real refresh changes (Builderr's own sample expects
    registry.employees to move from 2 to 3)."""
    universe_entry = {
        "organisation_number": "923609016", "name": "EXAMPLE CORP AS", "website": "example.no",
        "industry_code": "43.210", "industry_label": "Elektrisk installasjonsarbeid",
        "employees": 11, "legal_form": "AS", "bankrupt": False, "liquidating": False,
    }

    profile = _default_process_one(
        "923609016", BudgetGovernor(), universe_entry=universe_entry, client=_live_registry_client(_LIVE_BODY)
    )

    assert profile.legal_identity.employee_count.value == "12"  # live, not the manifest's 11
    assert profile.legal_identity.employee_count.source == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016"


def test_the_manifest_employee_count_is_used_when_the_live_record_cannot_be_read():
    universe_entry = {
        "organisation_number": "923609016", "name": "EXAMPLE CORP AS", "website": "example.no",
        "employees": 11, "legal_form": "AS", "bankrupt": False, "liquidating": False,
    }
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))

    profile = _default_process_one("923609016", BudgetGovernor(), universe_entry=universe_entry, client=client)

    assert profile.legal_identity.employee_count.value == "11"
