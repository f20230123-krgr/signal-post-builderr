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
from src.pipeline.registry_extras import fetch_leadership_only, fetch_registry_extras
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
