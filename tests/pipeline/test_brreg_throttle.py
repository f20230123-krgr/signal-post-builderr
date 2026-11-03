"""Brønnøysund's "slow down" (429, or 503 with Retry-After): wait the asked-for pause, shared
by every worker, and retry a bounded number of times, instead of losing that company's
registry data (measured 2026-10-17: 75 of 100 companies lost roles and accounts)."""
import httpx

from src.pipeline import brreg_throttle
from src.pipeline.registry_extras import _get
from src.pipeline.resolve import resolve
from src.models.profile import EvidenceState


def _client(responses):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        status, headers = responses[min(len(calls) - 1, len(responses) - 1)]
        body = {"organisasjonsnummer": "923609016", "navn": "EXAMPLE AS"} if status == 200 else {}
        return httpx.Response(status, json=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_a_throttled_request_waits_the_asked_for_pause_and_is_retried():
    client, calls = _client([(429, {"Retry-After": "2"}), (429, {"Retry-After": "2"}), (200, {})])
    slept = []

    response = _get(client, "https://data.brreg.no/enhetsregisteret/api/enheter/923609016/roller", slept.append)

    assert response is not None and response.status_code == 200
    assert len(calls) == 3 and slept and max(slept) <= brreg_throttle.MAX_WAIT_SECONDS


def test_the_pause_is_capped_and_retries_are_bounded():
    client, calls = _client([(429, {"Retry-After": "600"})])
    slept = []

    response = _get(client, "https://data.brreg.no/x", slept.append)

    assert response is not None and response.status_code == 429  # given up, reported by the caller
    assert len(calls) == 1 + brreg_throttle.MAX_THROTTLE_RETRIES
    assert all(s <= brreg_throttle.MAX_WAIT_SECONDS for s in slept)


def test_the_pause_is_shared_by_every_worker():
    brreg_throttle.note_throttle(httpx.Response(429, headers={"Retry-After": "3"}))
    slept = []

    brreg_throttle.wait_if_paused(slept.append)

    assert slept and 0 < slept[0] <= 3


def test_a_plain_server_error_keeps_the_old_behaviour():
    client, calls = _client([(503, {})])
    slept = []

    assert _get(client, "https://data.brreg.no/x", slept.append) is None
    assert len(calls) == 2  # MAX_RETRIES, no extra throttle retries without Retry-After


def test_resolve_survives_a_throttled_registry():
    client, calls = _client([(429, {"Retry-After": "1"}), (200, {})])

    entity = resolve("923609016", client=client, sleep=lambda s: None)

    assert entity.resolution_state == EvidenceState.AVAILABLE and entity.legal_name == "EXAMPLE AS"
