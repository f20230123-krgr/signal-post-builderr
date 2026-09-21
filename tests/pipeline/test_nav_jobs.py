"""
Tests for src/pipeline/nav_jobs.py -- NAV's official job-vacancy feed as a
hiring_signal source. No live network: every call goes through MockTransport.
"""
import json
from datetime import datetime, timezone

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.nav_jobs import (
    MAX_ADS_PER_COMPANY,
    NavJobIndex,
    build_nav_job_index,
    nav_hiring_signal_facts,
)
from src.pipeline.resolve import ResolvedEntity

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJwdWJsaWMifQ.c2lnbmF0dXJl"


def _entity(org="982087600", name="ENGØ GÅRD AS"):
    return ResolvedEntity(
        org_number=org, legal_name=name, registered_address="TØNSBERG", official_site_candidate=None,
        resolution_state=EvidenceState.AVAILABLE, source="x", retrieved_at=NOW,
    )


def _item(uuid, business, status="ACTIVE"):
    return {"id": uuid, "url": f"/api/v1/feedentry/{uuid}", "date_modified": "2026-09-14T10:00:00+02:00",
            "_feed_entry": {"uuid": uuid, "status": status, "businessName": business}}


def _feed_handler(pages, token_status=200, seen=None):
    def handler(request):
        path = request.url.path
        if seen is not None:
            seen.append((path, dict(request.headers)))
        if path == "/api/publicToken":
            return httpx.Response(token_status, text=f"Current public token for Nav Job Vacancy Feed:\n{TOKEN}\n")
        if path == "/api/v1/feed":
            return httpx.Response(200, json={"items": pages[0], "next_url": "/api/v1/feed/p2" if len(pages) > 1 else None})
        if path.startswith("/api/v1/feed/p"):
            n = int(path.rsplit("p", 1)[1])
            nxt = f"/api/v1/feed/p{n + 1}" if n < len(pages) else None
            return httpx.Response(200, json={"items": pages[n - 1], "next_url": nxt})
        return httpx.Response(404)
    return handler


def test_index_keeps_only_ads_still_active_at_the_end_of_the_window():
    """The feed replays changes in order: an ad that later turns INACTIVE
    must drop out, and employer names are matched case/space-insensitively."""
    pages = [
        [_item("a1", "Engø  Gård As"), _item("a2", "OTHER AS")],
        [_item("a2", "OTHER AS", status="INACTIVE")],
    ]
    seen = []
    client = httpx.Client(transport=httpx.MockTransport(_feed_handler(pages, seen=seen)))

    index = build_nav_job_index(client, BudgetGovernor(), now=lambda: NOW)

    assert [ad["uuid"] for ad in index.candidates_for("ENGØ GÅRD AS")] == ["a1"]
    assert index.candidates_for("OTHER AS") == []
    assert index.pages_fetched == 2
    first_feed_call = next(h for p, h in seen if p == "/api/v1/feed")
    assert first_feed_call.get("authorization") == f"Bearer {TOKEN}"
    assert "if-modified-since" in first_feed_call


def test_index_stops_at_the_page_cap_and_says_so():
    """Hard cap so the feed can never eat the request budget. The feed is
    oldest-first, so the cap cuts off the newest ads -- reported, not hidden."""
    pages = [[_item(f"a{i}", "ACME AS")] for i in range(10)]
    client = httpx.Client(transport=httpx.MockTransport(_feed_handler(pages)))

    index = build_nav_job_index(client, BudgetGovernor(), now=lambda: NOW, max_pages=3)

    assert index.pages_fetched == 3
    assert index.truncated is True
    assert index.requests_used == 4  # token + 3 pages
    assert index.report()["truncated_at_page_cap"] is True


def test_index_respects_the_request_budget():
    pages = [[_item(f"a{i}", "ACME AS")] for i in range(10)]
    client = httpx.Client(transport=httpx.MockTransport(_feed_handler(pages)))
    budget = BudgetGovernor(BudgetLimits(max_requests=3, max_spend_usd=10, max_wall_clock_seconds=1000))

    build_nav_job_index(client, budget, now=lambda: NOW)

    assert budget.requests_used == 3


def test_index_is_empty_when_the_token_cannot_be_fetched():
    client = httpx.Client(transport=httpx.MockTransport(_feed_handler([[_item("a1", "ACME AS")]], token_status=503)))

    index = build_nav_job_index(client, BudgetGovernor(), now=lambda: NOW, sleep=lambda s: None)

    assert index.token is None and index.active_ads == 0


def _index_for(entries):
    index = NavJobIndex(token=TOKEN)
    for uuid in entries:
        index.ads_by_employer.setdefault("ENGØ GÅRD AS", []).append(
            {"uuid": uuid, "url": f"/api/v1/feedentry/{uuid}", "employer": "ENGØ GÅRD AS"}
        )
    return index


def _entry_handler(entries):
    def handler(request):
        uuid = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json={"uuid": uuid, "ad_content": entries[uuid]})
    return handler


def _ad(orgnr, title="Kokk søkes", published="2026-09-01T10:00:00+02:00", expires="2026-10-01T00:00:00+02:00"):
    return {"title": title, "published": published, "expires": expires,
            "employer": {"name": "Engø Gård As", "orgnr": orgnr},
            "contactList": [{"name": "Kari Nordmann", "email": "kari@example.no", "phone": "12345678"}]}


def test_hiring_signal_is_published_only_on_an_exact_org_number_match():
    entries = {"right": _ad("982087600"), "same-name-other-company": _ad("111111111", title="Servitør")}
    client = httpx.Client(transport=httpx.MockTransport(_entry_handler(entries)))

    facts = nav_hiring_signal_facts(_entity(), _index_for(list(entries)), client, BudgetGovernor(), now=lambda: NOW)

    assert [f.value for f in facts] == ["Kokk søkes (published 2026-09-01, apply by 2026-10-01) - NAV"]
    assert facts[0].field_name == "hiring_signal"
    assert facts[0].source_url == "https://arbeidsplassen.nav.no/stillinger/stilling/right"
    assert facts[0].content_hash


def test_hiring_signal_never_publishes_contact_details():
    """Ads include contact people's names, emails and phone numbers -- only
    the job title and its dates may be published."""
    client = httpx.Client(transport=httpx.MockTransport(_entry_handler({"right": _ad("982087600")})))

    facts = nav_hiring_signal_facts(_entity(), _index_for(["right"]), client, BudgetGovernor(), now=lambda: NOW)

    assert not any(s in facts[0].value for s in ("Kari", "kari@example.no", "12345678"))


def test_expired_ads_are_skipped():
    entries = {"old": _ad("982087600", expires="2026-09-01T00:00:00+02:00")}
    client = httpx.Client(transport=httpx.MockTransport(_entry_handler(entries)))

    assert nav_hiring_signal_facts(_entity(), _index_for(["old"]), client, BudgetGovernor(), now=lambda: NOW) == []


def test_ads_fetched_per_company_are_capped():
    uuids = [f"ad{i}" for i in range(MAX_ADS_PER_COMPANY + 4)]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"ad_content": _ad("982087600")})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    facts = nav_hiring_signal_facts(_entity(), _index_for(uuids), client, BudgetGovernor(), now=lambda: NOW)

    assert len(calls) == MAX_ADS_PER_COMPANY
    assert len(facts) == MAX_ADS_PER_COMPANY


def test_ad_fetches_back_off_when_the_budget_is_nearly_spent():
    def handler(request):
        raise AssertionError("must not fetch ads once the budget is nearly spent")

    budget = BudgetGovernor(BudgetLimits(max_requests=10, max_spend_usd=10, max_wall_clock_seconds=1000))
    for _ in range(9):
        budget.record_request()

    facts = nav_hiring_signal_facts(
        _entity(), _index_for(["right"]), httpx.Client(transport=httpx.MockTransport(handler)), budget, now=lambda: NOW
    )

    assert facts == []


def test_no_index_or_no_matching_ads_makes_no_request():
    def handler(request):
        raise AssertionError("no request expected")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert nav_hiring_signal_facts(_entity(), None, client, BudgetGovernor(), now=lambda: NOW) == []
    assert nav_hiring_signal_facts(_entity(name="UNRELATED AS"), _index_for(["right"]), client,
                                   BudgetGovernor(), now=lambda: NOW) == []


def test_an_ad_under_one_of_the_company_s_registered_sub_units_is_accepted():
    """NAV ads usually carry the workplace (sub-unit) org number, not the
    parent's -- confirmed live on INSIDER FACILITY SOLUTIONS AS, whose two
    active ads used two of its own registered sub-unit numbers."""
    client = httpx.Client(transport=httpx.MockTransport(_entry_handler({"right": _ad("917784078")})))

    facts = nav_hiring_signal_facts(
        _entity(), _index_for(["right"]), client, BudgetGovernor(), now=lambda: NOW,
        subunit_org_numbers={"917784078"},
    )

    assert len(facts) == 1


def test_a_sub_unit_number_that_is_not_this_company_s_is_still_rejected():
    client = httpx.Client(transport=httpx.MockTransport(_entry_handler({"right": _ad("917784078")})))

    facts = nav_hiring_signal_facts(
        _entity(), _index_for(["right"]), client, BudgetGovernor(), now=lambda: NOW,
        subunit_org_numbers={"111111111"},
    )

    assert facts == []


def test_the_index_reads_ads_modified_in_the_last_30_days():
    """A 14-day window (~70% of active ads) missed ads that are still live but
    were last modified longer ago: two real ads published on 2 and 4 September,
    live until 25 September and 31 October, dropped out of a run made three weeks
    later. 30 days reaches ~93% of active ads for about 33 more requests."""
    from email.utils import format_datetime
    from datetime import timedelta

    seen = []
    client = httpx.Client(transport=httpx.MockTransport(_feed_handler([[_item("a1", "X AS")]], seen=seen)))

    build_nav_job_index(client, BudgetGovernor(), now=lambda: NOW)

    first_feed_call = next(h for p, h in seen if p == "/api/v1/feed")
    assert first_feed_call["if-modified-since"] == format_datetime(NOW - timedelta(days=30), usegmt=True)


def test_the_page_cap_leaves_room_for_the_30_day_window():
    from src.pipeline.nav_jobs import MAX_NAV_FEED_PAGES

    assert MAX_NAV_FEED_PAGES >= 80  # ~64 pages cover 30 days; the cap cuts off the NEWEST ads
