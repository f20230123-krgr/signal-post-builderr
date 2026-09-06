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
from src.pipeline.registry_extras import fetch_registry_extras
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
    facts = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("997770234"))

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


def test_annual_accounts_latest_and_history_are_extracted():
    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    facts = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("923609016"))

    latest = [f for f in facts if f.field_name == "annual_accounts_latest"]
    assert len(latest) == 1
    assert "67,956,000,000" in latest[0].value or "67956000000" in latest[0].value
    assert latest[0].reporting_period == "FY2025"

    # never fabricated: no fields invented beyond what the API actually returned
    assert "USD" in latest[0].value


def test_workplaces_are_extracted():
    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    facts = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("923609016"))

    workplaces = [f for f in facts if f.field_name == "workplace"]
    assert len(workplaces) > 0
    assert any("SOTRA" in f.value for f in workplaces)
    assert all(f.match_confidence == 100.0 for f in workplaces)


def test_budget_exhausted_skips_remaining_registry_calls():
    entity = _entity(org_number="923609016", legal_name="EQUINOR ASA")
    # Exactly one request allowed -- only the first of the three registry
    # calls (roles) should go through; accounts and workplaces are skipped.
    budget = BudgetGovernor(BudgetLimits(max_requests=1, max_spend_usd=10, max_wall_clock_seconds=1000))

    facts = fetch_registry_extras(entity, budget, client=_client_for("923609016"))

    assert any(f.field_name == "leader" for f in facts)
    assert not any(f.field_name.startswith("annual_accounts") for f in facts)
    assert not any(f.field_name == "workplace" for f in facts)


def test_no_facts_when_entity_not_resolved():
    entity = _entity()
    entity.resolution_state = EvidenceState.NOT_AVAILABLE
    facts = fetch_registry_extras(entity, BudgetGovernor(), client=_client_for("997770234"))
    assert facts == []


def test_network_failure_degrades_gracefully_to_no_facts():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    entity = _entity()
    facts = fetch_registry_extras(entity, BudgetGovernor(), client=client, sleep=lambda s: None)
    assert facts == []
