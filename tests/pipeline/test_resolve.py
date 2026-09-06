"""
Tests for src/pipeline/resolve.py.
Cases required by docs/component-specs.md -- fixtures/registry/*.json holds
real recorded Brønnøysundregisteret API responses (see
fixtures/record_brreg_fixtures.py); no live network call happens here --
httpx.MockTransport replays the recorded bytes.
"""
import json
from pathlib import Path

import httpx
import pytest

from src.models.profile import EvidenceState
from src.pipeline.resolve import resolve

FIXTURES = Path(__file__).parent.parent.parent / "fixtures" / "registry"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _client_for(fixture: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if fixture["body"] is None:
            return httpx.Response(fixture["status"])
        return httpx.Response(fixture["status"], json=fixture["body"])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_exact_match_lookup():
    fixture = _load("923609016.json")
    entity = resolve("923609016", client=_client_for(fixture))

    assert entity.org_number == "923609016"
    assert entity.legal_name == "EQUINOR ASA"
    assert entity.resolution_state == EvidenceState.AVAILABLE
    assert entity.official_site_candidate == "https://www.equinor.com"
    assert "STAVANGER" in entity.registered_address
    assert entity.source
    assert entity.retrieved_at is not None
    assert entity.content_hash  # source policy: every claim records a content hash


def test_exact_match_lookup_second_company():
    fixture = _load("997770234.json")
    entity = resolve("997770234", client=_client_for(fixture))

    assert entity.legal_name == "KAHOOT! AS"
    assert entity.official_site_candidate == "https://www.kahoot.com"
    assert entity.resolution_state == EvidenceState.AVAILABLE


def test_no_match_returns_not_available():
    fixture = _load("000000000.json")
    entity = resolve("000000000", client=_client_for(fixture))

    assert entity.resolution_state == EvidenceState.NOT_AVAILABLE
    assert entity.legal_name is None
    assert entity.registered_address is None
    assert entity.official_site_candidate is None


def test_ambiguous_match_returns_ambiguous_state():
    fixture = _load("ambiguous_synthetic.json")
    entity = resolve("912345678", client=_client_for(fixture))

    assert entity.resolution_state == EvidenceState.AMBIGUOUS
    assert entity.legal_name is None


def test_registry_timeout_returns_failed_after_capped_retries():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise httpx.TimeoutException("connect timed out", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))

    entity = resolve("923609016", client=client, sleep=lambda seconds: None)

    assert entity.resolution_state == EvidenceState.FAILED
    assert entity.legal_name is None
    assert 1 < len(calls) <= 5  # retried a bounded number of times, not forever


def test_registry_server_error_retries_then_fails():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    entity = resolve("923609016", client=client, sleep=lambda seconds: None)

    assert entity.resolution_state == EvidenceState.FAILED
    assert len(calls) > 1


def test_universe_entry_resolves_without_any_network_call():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500)  # would fail loudly if ever called

    client = httpx.Client(transport=httpx.MockTransport(handler))
    universe_entry = {
        "organisation_number": "923609016",
        "name": "EQUINOR ASA",
        "website": "www.equinor.com",
        "municipality": "STAVANGER",
    }

    entity = resolve("923609016", client=client, universe_entry=universe_entry)

    assert calls == []
    assert entity.legal_name == "EQUINOR ASA"
    assert entity.official_site_candidate == "https://www.equinor.com"
    assert entity.registered_address == "STAVANGER"
    assert entity.resolution_state == EvidenceState.AVAILABLE
    assert entity.source == "signalpost-company-universe-2025.jsonl.gz"
    assert entity.content_hash


def test_universe_entry_with_blank_website_has_no_site_candidate():
    universe_entry = {
        "organisation_number": "810034882",
        "name": "SANDNES ELEKTRISKE AS",
        "website": "",
        "municipality": "SANDNES",
    }

    entity = resolve("810034882", universe_entry=universe_entry)

    assert entity.official_site_candidate is None
    assert entity.resolution_state == EvidenceState.AVAILABLE
