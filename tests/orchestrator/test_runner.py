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

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.orchestrator.runner import _default_process_one, run_batch, run_in_chunks
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


def test_default_process_one_resolves_from_universe_without_registry_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        # The plain entity-lookup endpoint must never be hit when a universe
        # entry is supplied -- fail loudly if it ever is.
        if str(request.url) == "https://data.brreg.no/enhetsregisteret/api/enheter/923609016":
            return httpx.Response(500)
        return httpx.Response(404)  # roller/regnskap/underenheter: no extra data

    client = httpx.Client(transport=httpx.MockTransport(handler))
    universe_entry = {"organisation_number": "923609016", "name": "EQUINOR ASA", "website": "www.equinor.com"}

    profile = _default_process_one(
        "923609016", BudgetGovernor(), previous_snapshot=None, universe_entry=universe_entry, client=client
    )

    assert profile.legal_identity.legal_name.value == "EQUINOR ASA"
    assert "https://data.brreg.no/enhetsregisteret/api/enheter/923609016" not in calls


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
            return httpx.Response(200, text="<html>We are hiring: Backend Engineer, Oslo</html>")
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
