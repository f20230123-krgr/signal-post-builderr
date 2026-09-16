"""
Tests for src/run_batch.py's startup key check -- the part of the CLI that
decides, before any company is processed, whether a discovery key is dead and
whether to stop. No live network: provider calls go through MockTransport.
"""
import httpx
import pytest

from src.run_batch import startup_key_check


def _client(exa_status: int, parallel_status: int) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        status = exa_status if "exa.ai" in str(request.url) else parallel_status
        return httpx.Response(status, json={"results": []})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _keys(monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "fake-exa")
    monkeypatch.setenv("PARALLEL_API_KEY", "fake-parallel")


def test_a_dead_key_with_the_stop_flag_exits_before_any_work(monkeypatch, capsys):
    _keys(monkeypatch)

    with pytest.raises(SystemExit) as excinfo:
        startup_key_check(stop_if_key_dead=True, client=_client(402, 200))

    assert excinfo.value.code == 1
    out = capsys.readouterr().out
    assert "exa" in out and "402" in out
    assert "stopped" in out.lower()


def test_a_dead_key_without_the_flag_warns_and_continues(monkeypatch, capsys):
    """Default path, used by the graded run: warn up front, keep going, and
    skip the dead provider from the very first company."""
    _keys(monkeypatch)

    health, requests_used = startup_key_check(stop_if_key_dead=False, client=_client(402, 200))

    assert not health.is_available("exa")
    assert health.is_available("parallel")
    assert requests_used == 2
    out = capsys.readouterr().out
    assert "exa" in out and "402" in out
    assert "continuing" in out.lower()


def test_healthy_keys_print_nothing_and_disable_nothing(monkeypatch, capsys):
    _keys(monkeypatch)

    health, _ = startup_key_check(stop_if_key_dead=True, client=_client(200, 200))

    assert health.disabled == {}
    assert capsys.readouterr().out == ""


def test_no_configured_keys_makes_no_request(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("PARALLEL_API_KEY", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call a provider with no key")

    health, requests_used = startup_key_check(
        stop_if_key_dead=True, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert health.disabled == {}
    assert requests_used == 0


def test_the_run_spend_cap_is_applied_and_counts_the_startup_exa_search(monkeypatch):
    _keys(monkeypatch)

    health, _ = startup_key_check(stop_if_key_dead=False, client=_client(200, 200), spend_cap_usd=10.0)

    assert health.spent_usd == pytest.approx(0.007)
    assert health.reserve_spend("exa", 9.995) is False  # 0.007 + 9.995 = 10.002 > cap
    assert not health.is_available("exa")
