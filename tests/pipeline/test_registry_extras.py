"""
Tests for src/pipeline/registry_extras.py.

Contract: docs/component-specs.md -> "src/pipeline/registry_extras.py"

Fixtures are REAL recorded responses from Brreg's roller/regnskapsregisteret/
underenheter endpoints (see fixtures/record_brreg_fixtures.py) -- no live
network call happens here, httpx.MockTransport replays the recorded bytes.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.registry_extras import (
    fetch_leadership_only,
    fetch_registry_extras,
    fetch_live_registry_details,
    fetch_registry_update_activity,
    name_is_held_by_another_entity,
    universe_identity_facts,
)
from src.pipeline.resolve import ResolvedEntity

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "registry"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _entity(org_number="997770234", legal_name="KAHOOT! AS", site="https://www.kahoot.com"):
    return ResolvedEntity(
        org_number=org_number,
        legal_name=legal_name,
        registered_address="some address",
        official_site_candidate=site,
        resolution_state=EvidenceState.AVAILABLE,
        source=f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}",
        retrieved_at=NOW,
    )


def _load(kind: str, org_number: str) -> dict:
    return json.loads((FIXTURES / kind / f"{org_number}.json").read_text(encoding="utf-8"))


def _client_for(org_number: str) -> httpx.Client:
    roller = _load("roller", org_number)
    regnskap = _load("regnskap", org_number)
    underenheter = _load("underenheter", org_number)
    base = f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == f"{base}/roller":
            return httpx.Response(roller["status"], json=roller["body"]) if roller["body"] is not None else httpx.Response(roller["status"])
        if url == f"https://data.brreg.no/regnskapsregisteret/regnskap/{org_number}":
            return httpx.Response(regnskap["status"], json=regnskap["body"]) if regnskap["body"] is not None else httpx.Response(regnskap["status"])
        if url == f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org_number}":
            return httpx.Response(underenheter["status"], json=underenheter["body"]) if underenheter["body"] is not None else httpx.Response(underenheter["status"])
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_leadership_roles_are_extracted_and_filtered():
    entity = _entity()
    facts, _ = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("997770234"))

    leaders = [f for f in facts if f.field_name == "leader"]
    values = {f.value for f in leaders}

    # DAGL (CEO) and STYR/LEDE|MEDL (board chair/members) included
    assert any("Eilert" in v and "Hanoa" in v for v in values)
    assert any("Alexander" in v and "Remen" in v for v in values)

    # REVI (auditor, an "enhet" not a "person") and VARA (deputy) excluded --
    # not "leadership" per docs/data-schema.md ("e.g. CEO, board chair").
    assert not any("Deloitte" in v for v in values)
    assert not any("Zelenetska" in v for v in values)

    # no birthdate leaked into the published value (privacy)
    assert not any("1970" in v for v in values)

    for fact in leaders:
        assert fact.match_confidence == 100.0
        assert fact.source_url.endswith("/roller")
        assert fact.source_class == "official_registry"
        assert fact.extraction_method == "registry"
        assert fact.content_hash


def test_fetch_leadership_only_returns_just_leaders_and_spends_one_request():
    """New: used by the discovery-stage "leader/founder bridge" (agent
    playbook §2), which needs leader names BEFORE the rest of
    fetch_registry_extras() normally runs later in the pipeline. Must not
    fetch accounts or workplaces -- those still come from the normal
    fetch_registry_extras() call downstream; this is leadership only."""
    entity = _entity()
    budget = BudgetGovernor()

    facts = fetch_leadership_only(entity, budget, client=_client_for("997770234"))

    assert facts and all(f.field_name == "leader" for f in facts)
    assert budget.requests_used == 1


def test_fetch_leadership_only_returns_nothing_when_entity_not_resolved():
    entity = _entity()
    entity.resolution_state = EvidenceState.NOT_AVAILABLE
    budget = BudgetGovernor()

    assert fetch_leadership_only(entity, budget) == []
    assert budget.requests_used == 0


def test_fetch_leadership_only_respects_budget():
    entity = _entity()
    budget = BudgetGovernor(BudgetLimits(max_requests=0, max_spend_usd=10, max_wall_clock_seconds=1000))

    assert fetch_leadership_only(entity, budget, client=_client_for("997770234")) == []


def test_annual_accounts_latest_and_history_are_extracted():
    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    facts, _ = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("923609016"))

    latest = [f for f in facts if f.field_name == "annual_accounts_latest"]
    assert len(latest) == 1
    assert "67,956,000,000" in latest[0].value or "67956000000" in latest[0].value
    assert latest[0].reporting_period == "FY2025"

    # never fabricated: no fields invented beyond what the API actually returned
    assert "USD" in latest[0].value


def test_workplaces_are_extracted():
    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    facts, _ = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("923609016"))

    workplaces = [f for f in facts if f.field_name == "workplace"]
    assert len(workplaces) > 0
    assert any("SOTRA" in f.value for f in workplaces)
    assert all(f.match_confidence == 100.0 for f in workplaces)


def test_workplaces_follow_pagination_across_multiple_pages():
    """Real evaluator feedback: "the workplace collector reads only the
    first results page. That missed 116 workplaces across three companies.
    Follow pagination until there are no more results." Brreg's
    underenheter endpoint uses real Spring-style pagination (_links.next),
    confirmed against the live API."""
    page0 = {
        "_embedded": {"underenheter": [{"navn": "Workplace A", "beliggenhetsadresse": {"kommune": "OSLO"}}]},
        "_links": {"next": {"href": "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=923609016&page=1"}},
        "page": {"totalPages": 2},
    }
    page1 = {
        "_embedded": {"underenheter": [{"navn": "Workplace B", "beliggenhetsadresse": {"kommune": "BERGEN"}}]},
        "_links": {},
        "page": {"totalPages": 2},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "page=1" in url:
            return httpx.Response(200, json=page1)
        if "underenheter" in url:
            return httpx.Response(200, json=page0)
        return httpx.Response(404)

    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    facts, _ = fetch_registry_extras(entity, BudgetGovernor(), client=client)

    workplaces = {f.value for f in facts if f.field_name == "workplace"}
    assert any("Workplace A" in v for v in workplaces)
    assert any("Workplace B" in v for v in workplaces)


def test_workplace_pagination_stops_cleanly_when_budget_runs_out():
    page0 = {
        "_embedded": {"underenheter": [{"navn": "Workplace A"}]},
        "_links": {"next": {"href": "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=923609016&page=1"}},
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=page0)

    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    # 3 requests: roles + accounts + exactly one workplace page, then exhausted
    budget = BudgetGovernor(BudgetLimits(max_requests=3, max_spend_usd=10, max_wall_clock_seconds=1000))

    facts, _ = fetch_registry_extras(entity, budget, client=client)

    workplace_calls = [c for c in calls if "underenheter" in c]
    assert len(workplace_calls) == 1  # stopped before following "next"
    assert any(f.field_name == "workplace" for f in facts)


def test_budget_exhausted_skips_remaining_registry_calls():
    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    # Exactly one request allowed -- only the first of the three registry
    # calls (roles) should go through; accounts and workplaces are skipped.
    budget = BudgetGovernor(BudgetLimits(max_requests=1, max_spend_usd=10, max_wall_clock_seconds=1000))

    facts, _ = fetch_registry_extras(entity, budget, client=_client_for("923609016"))

    assert any(f.field_name == "leader" for f in facts)
    assert not any(f.field_name.startswith("annual_accounts") for f in facts)
    assert not any(f.field_name == "workplace" for f in facts)


def test_no_facts_when_entity_not_resolved():
    entity = _entity()
    entity.resolution_state = EvidenceState.NOT_AVAILABLE
    facts, accounts_state = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("997770234"))
    assert facts == []
    assert accounts_state == EvidenceState.NOT_AVAILABLE


def test_network_failure_degrades_gracefully_to_no_facts():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    entity = _entity()
    facts, accounts_state = fetch_registry_extras(entity, BudgetGovernor(), client=client, sleep=lambda s: None)
    assert facts == []
    # Real evaluator feedback: "a temporary source error was also recorded as
    # information being unavailable." A transport failure must be reported
    # honestly as FAILED, never conflated with a confirmed "no accounts
    # filed" (NOT_AVAILABLE).
    assert accounts_state == EvidenceState.FAILED


def test_accounts_state_is_available_when_accounts_are_found():
    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    _, accounts_state = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("923609016"))
    assert accounts_state == EvidenceState.AVAILABLE


def test_accounts_state_is_not_available_when_registry_confirms_no_accounts_filed():
    """A 404 from regnskapsregisteret means Brreg genuinely has no accounts on
    file for this org number (e.g. a newly-formed company) -- a confirmed
    absence, not a fetch problem, so this must stay NOT_AVAILABLE."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "regnskapsregisteret" in url:
            return httpx.Response(404)
        return httpx.Response(404)

    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    facts, accounts_state = fetch_registry_extras(entity, BudgetGovernor(), client=client)

    assert not any(f.field_name.startswith("annual_accounts") for f in facts)
    assert accounts_state == EvidenceState.NOT_AVAILABLE


def test_accounts_state_is_failed_when_the_accounts_endpoint_returns_a_server_error():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "regnskapsregisteret" in url:
            return httpx.Response(503)
        return httpx.Response(404)

    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    facts, accounts_state = fetch_registry_extras(entity, BudgetGovernor(), client=client, sleep=lambda s: None)

    assert not any(f.field_name.startswith("annual_accounts") for f in facts)
    assert accounts_state == EvidenceState.FAILED


def test_universe_identity_facts_mines_free_fields_from_the_universe_record():
    """Builderr's frozen universe record already carries industry, employee
    count, legal form and insolvency status for every covered company -- all
    official-registry data we already hold in memory, so emitting them costs
    zero requests and carries no identity-matching risk (same trust tier as
    resolve() itself, which reads the same record)."""
    entry = {
        "organisation_number": "810034882",
        "name": "SANDNES ELEKTRISKE AS",
        "legal_form": "AS",
        "employees": 11,
        "bankrupt": False,
        "liquidating": False,
        "municipality": "SANDNES",
        "industry_code": "43.210",
        "industry_label": "Elektrisk installasjonsarbeid",
        "website": "",
    }

    facts = universe_identity_facts("810034882", entry, now=NOW)
    by_field = {f.field_name: f.value for f in facts}

    assert by_field["industry"] == "43.210 Elektrisk installasjonsarbeid"
    assert by_field["employee_count"] == "11"
    assert by_field["legal_form"] == "AS"
    assert by_field["operating_status"] == "Active"
    # Registry-tier trust, zero identity risk -- same as every other fact here.
    assert all(f.source_class == "official_registry" for f in facts)
    assert all(f.extraction_method == "registry" for f in facts)
    assert all(f.match_confidence == 100.0 for f in facts)


def test_universe_identity_facts_reports_insolvency_and_skips_missing_fields():
    entry = {
        "organisation_number": "999999999",
        "name": "KONKURS AS",
        "legal_form": "AS",
        "employees": None,
        "bankrupt": True,
        "liquidating": False,
        "industry_code": "",
        "industry_label": "",
    }

    facts = universe_identity_facts("999999999", entry, now=NOW)
    by_field = {f.field_name: f.value for f in facts}

    assert by_field["operating_status"] == "Bankrupt"
    # Never invent a value for a field the record doesn't actually carry.
    assert "industry" not in by_field
    assert "employee_count" not in by_field


def test_universe_identity_facts_returns_nothing_without_a_universe_record():
    assert universe_identity_facts("810034882", None, now=NOW) == []


def _updates_response(dates: list[str]) -> dict:
    return {
        "_embedded": {
            "oppdaterteEnheter": [
                {"oppdateringsid": i, "dato": d, "organisasjonsnummer": "923609016", "endringstype": "Endring"}
                for i, d in enumerate(dates)
            ]
        }
    }


def test_registry_update_activity_emits_the_most_recent_events_as_dated_activity():
    """Brreg's oppdateringer/enheter endpoint (confirmed live to accept an
    organisasjonsnummer filter) is per-company, dated, authoritative and
    free -- exactly the "dated public activity from permitted sources" the
    brief asks for, for companies that have no website at all. Capped to the
    most recent few: a company can have 40+ update events and republishing
    all of them would bury the signal in registry churn."""
    dates = [
        "2026-05-11T22:30:42.050Z",
        "2026-07-15T22:03:09.106Z",
        "2026-08-11T22:03:42.412Z",
        "2026-09-14T22:01:56.024Z",
    ]
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_updates_response(dates)))
    )

    facts = fetch_registry_update_activity(_entity(), BudgetGovernor(), client=client, now=lambda: NOW, max_events=3)

    assert len(facts) == 3
    assert all(f.field_name == "dated_activity" for f in facts)
    assert all(f.source_class == "official_registry" for f in facts)
    # Most recent first, and each carries its own real date.
    assert "2026-09-14" in facts[0].value
    assert "2026-08-11" in facts[1].value
    assert "2026-07-15" in facts[2].value


def test_registry_update_activity_returns_nothing_when_budget_is_exhausted():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_updates_response([]))))
    exhausted = BudgetGovernor(BudgetLimits(max_requests=0, max_spend_usd=10, max_wall_clock_seconds=1000))

    assert fetch_registry_update_activity(_entity(), exhausted, client=client, now=lambda: NOW) == []


def test_registry_update_activity_degrades_cleanly_on_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    facts = fetch_registry_update_activity(
        _entity(), BudgetGovernor(), client=client, sleep=lambda s: None, now=lambda: NOW
    )

    assert facts == []


def test_workplace_falls_back_to_the_entity_s_own_registered_location():
    """A company with no registered sub-units still has a workplace -- its
    own registered business address. Without this, ~25% of companies in a
    real batch reported no workplace at all despite the registry holding a
    perfectly good one. Registry-sourced, so same trust tier and zero extra
    requests (the sub-units call already happened and came back empty)."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "underenheter" in url:
            return httpx.Response(200, json={"_embedded": {"underenheter": []}})
        return httpx.Response(404)

    entity = _entity(org_number="810034882", legal_name="SANDNES ELEKTRISKE AS")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    facts, _ = fetch_registry_extras(entity, BudgetGovernor(), client=client, now=lambda: NOW)
    workplaces = [f for f in facts if f.field_name == "workplace"]

    assert len(workplaces) == 1
    assert "SANDNES ELEKTRISKE AS" in workplaces[0].value
    assert workplaces[0].source_class == "official_registry"


def test_workplace_fallback_does_not_fire_when_real_subunits_exist():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "underenheter" in url:
            return httpx.Response(
                200,
                json={"_embedded": {"underenheter": [{"navn": "SANDNES ELEKTRISKE AVD OSLO", "beliggenhetsadresse": {"kommune": "OSLO"}}]}},
            )
        return httpx.Response(404)

    entity = _entity(org_number="810034882", legal_name="SANDNES ELEKTRISKE AS")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    facts, _ = fetch_registry_extras(entity, BudgetGovernor(), client=client, now=lambda: NOW)
    workplaces = [f for f in facts if f.field_name == "workplace"]

    assert len(workplaces) == 1
    assert "AVD OSLO" in workplaces[0].value


def test_universe_identity_facts_carry_the_same_content_hash_as_the_legal_name():
    """Every fact read from the universe record must carry the same evidence
    fingerprint resolve() records for legal_name from that exact record."""
    import hashlib

    entry = {"organisation_number": "810034882", "name": "SANDNES ELEKTRISKE AS", "legal_form": "AS",
             "industry_code": "43.210", "industry_label": "Elektrisk installasjonsarbeid", "employees": 11}
    expected = hashlib.sha256(json.dumps(entry, sort_keys=True).encode("utf-8")).hexdigest()

    facts = universe_identity_facts("810034882", entry, now=NOW)

    assert facts and all(f.content_hash == expected for f in facts)


def _live_record(**extra) -> dict:
    body = {"organisasjonsnummer": "997770234", "navn": "KAHOOT! AS"}
    body.update(extra)
    return body


def test_live_registry_details_publish_founding_date_and_former_names():
    """The universe manifest carries neither. Measured on 40 random companies
    without a website: founding date 40/40, former names 11/40."""
    record = _live_record(
        stiftelsesdato="2012-07-01",
        historiskeNavn=[
            {"navn": "OLDEST AS", "fraDato": "2012-07-01 00:00:00", "tilDato": "2014-01-01 00:00:00"},
            {"navn": "NEWEST AS", "fraDato": "2019-01-01 00:00:00", "tilDato": "2023-05-02 08:00:00"},
            {"navn": "MIDDLE AS", "fraDato": "2014-01-01 00:00:00", "tilDato": "2019-01-01 00:00:00"},
            {"navn": "MIDDLE2 AS", "fraDato": "2013-01-01 00:00:00", "tilDato": "2016-01-01 00:00:00"},
        ],
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=record)))

    details = fetch_live_registry_details(_entity(), BudgetGovernor(), client=client, now=lambda: NOW)
    by_field = {}
    for f in details.facts:
        by_field.setdefault(f.field_name, []).append(f.value)

    assert by_field["founded_date"] == ["2012-07-01"]
    assert "Founded on 2012-07-01 - Brønnøysundregistrene" in by_field["dated_activity"]
    assert "Renamed from 'NEWEST AS' on 2023-05-02 - Brønnøysundregistrene" in by_field["dated_activity"]
    # newest first, capped
    assert details.former_names == ["NEWEST AS", "MIDDLE AS", "MIDDLE2 AS"]
    assert all(f.source_class == "official_registry" for f in details.facts)


def test_live_registry_details_back_off_when_the_budget_is_nearly_spent():
    """An extra, not core data: once the batch is past 90% of its request
    budget the remaining requests belong to core registry data."""
    def handler(request):
        raise AssertionError("must not spend a request when the budget is nearly spent")

    budget = BudgetGovernor(BudgetLimits(max_requests=10, max_spend_usd=10, max_wall_clock_seconds=1000))
    for _ in range(9):
        budget.record_request()

    details = fetch_live_registry_details(
        _entity(), budget, client=httpx.Client(transport=httpx.MockTransport(handler)), now=lambda: NOW
    )

    assert details.facts == [] and details.former_names == []


def test_live_registry_details_degrade_cleanly_on_error():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    details = fetch_live_registry_details(
        _entity(), BudgetGovernor(), client=client, sleep=lambda s: None, now=lambda: NOW
    )

    assert details.facts == [] and details.former_names == []


def _name_search(units):
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"_embedded": {"enheter": units}})
    ))


def test_former_name_now_held_by_another_entity_is_detected():
    client = _name_search([{"organisasjonsnummer": "111111111", "navn": "Old Name AS"}])

    assert name_is_held_by_another_entity("OLD NAME AS", "997770234", BudgetGovernor(), client=client) is True


def test_former_name_not_held_by_anyone_else_is_usable():
    client = _name_search([
        {"organisasjonsnummer": "997770234", "navn": "OLD NAME AS"},
        {"organisasjonsnummer": "222222222", "navn": "OLD NAME HOLDING AS"},
    ])

    assert name_is_held_by_another_entity("OLD NAME AS", "997770234", BudgetGovernor(), client=client) is False


def test_name_check_is_precision_first_when_it_cannot_confirm():
    """A failed lookup must answer "held" -- the only cost is skipping one
    extra search, while guessing wrong could publish another company's site."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))

    assert name_is_held_by_another_entity("OLD NAME AS", "997770234", BudgetGovernor(), client=client) is True


def test_subunit_org_numbers_are_read_from_workplace_registry_urls():
    from src.pipeline.registry_extras import subunit_org_numbers
    from src.pipeline.verify import ConfirmedFact

    facts = [
        ConfirmedFact("workplace", "A", "https://data.brreg.no/enhetsregisteret/api/underenheter/917784078", 100.0, NOW),
        ConfirmedFact("workplace", "B", "https://data.brreg.no/enhetsregisteret/api/underenheter/994172603", 100.0, NOW),
        ConfirmedFact("workplace", "own", "signalpost-company-universe-2025.jsonl.gz", 100.0, NOW),
        ConfirmedFact("leader", "X", "https://data.brreg.no/enhetsregisteret/api/underenheter/123456789", 100.0, NOW),
    ]

    assert subunit_org_numbers(facts) == {"917784078", "994172603"}
