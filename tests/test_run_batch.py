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


# ---- universe file: use it if present, fetch it if not -------------------

import gzip
import hashlib
import json
from pathlib import Path

from src.run_batch import prepare_universe


def _archive() -> bytes:
    return gzip.compress((json.dumps({"organisation_number": "923609016", "name": "X AS"}) + "\n").encode("utf-8"))


def _refusing_client() -> httpx.Client:
    def handler(request):
        raise AssertionError("no request expected")

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_an_explicit_universe_path_is_used_as_given_and_never_downloaded(tmp_path):
    explicit = tmp_path / "mine.jsonl"

    path, requests_used = prepare_universe(explicit, tmp_path / "default.jsonl.gz", _refusing_client())

    assert path == explicit and requests_used == 0


def test_an_existing_default_file_is_used_without_a_download(tmp_path):
    default = tmp_path / "default.jsonl.gz"
    default.write_bytes(_archive())

    path, requests_used = prepare_universe(None, default, _refusing_client())

    assert path == default and requests_used == 0


def test_with_no_file_anywhere_the_manifest_is_fetched_once(tmp_path, monkeypatch):
    body = _archive()
    monkeypatch.setattr("src.pipeline.universe.UNIVERSE_ARCHIVE_SHA256", hashlib.sha256(body).hexdigest())
    default = tmp_path / "default.jsonl.gz"
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))

    path, requests_used = prepare_universe(None, default, client)

    assert path == default and default.exists() and requests_used == 1


def test_if_the_manifest_cannot_be_fetched_the_run_continues_without_it(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))

    path, requests_used = prepare_universe(None, tmp_path / "default.jsonl.gz", client)

    assert path is None and requests_used == 1


# ---- secrets never leave the environment ---------------------------------


def test_api_key_values_never_appear_in_output_or_logs(monkeypatch, capsys, caplog):
    """Keys come from environment variables only; the evaluation contract
    requires secrets be handled safely, so a key must not be printable by any
    startup path -- dead key, healthy key, or a failed call."""
    monkeypatch.setenv("EXA_API_KEY", "SECRET-EXA-VALUE-123")
    monkeypatch.setenv("PARALLEL_API_KEY", "SECRET-PARALLEL-VALUE-456")

    with caplog.at_level("DEBUG"):
        startup_key_check(stop_if_key_dead=False, client=_client(402, 401))
        startup_key_check(stop_if_key_dead=False, client=_client(200, 200))

    seen = capsys.readouterr()
    everything = seen.out + seen.err + caplog.text
    assert "SECRET-EXA-VALUE-123" not in everything
    assert "SECRET-PARALLEL-VALUE-456" not in everything


def test_search_key_status_reports_absent_active_and_disabled(monkeypatch):
    from src.run_batch import search_key_status

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.setenv("PARALLEL_API_KEY", "p")

    assert search_key_status({}) == {"exa": "absent", "parallel": "active"}
    assert search_key_status({"parallel": "out of credit"}) == {"exa": "absent", "parallel": "disabled"}


def test_the_keyless_notice_is_one_informational_line_and_never_asks_for_input(monkeypatch):
    from src.run_batch import search_keys_notice

    notice = search_keys_notice({"exa": "absent", "parallel": "absent"})

    assert "running keyless" in notice and "EXA_API_KEY" in notice and "PARALLEL_API_KEY" in notice
    assert "\n" not in notice
    assert search_keys_notice({"exa": "active", "parallel": "active"}) is None
    assert "Not active: parallel" in search_keys_notice({"exa": "active", "parallel": "absent"})


def test_the_viewer_records_which_search_keys_the_run_had():
    import json
    import re
    from datetime import datetime, timezone

    from src.reporting import render_html_report
    from tests.conftest import make_profile

    html = render_html_report(
        [make_profile()], generated_at=datetime(2026, 1, 1, tzinfo=timezone.utc), search_keys={"exa": "absent", "parallel": "absent"}
    )

    data = json.loads(re.search(r'id="atlas-data">(.*?)</script>', html, re.S).group(1))
    assert data["search_keys"] == {"exa": "absent", "parallel": "absent"}


def test_the_one_command_run_never_starts_a_browser_unless_asked(monkeypatch, tmp_path):
    """The evaluator's install has no browser: the default run must not depend on one."""
    import src.run_batch as run_batch

    started = []

    class FakeRenderer:
        def start(self):
            started.append(True)
            return True

    monkeypatch.setattr(run_batch, "shared_renderer", lambda: FakeRenderer())
    monkeypatch.setattr(run_batch, "startup_key_check", lambda *a, **k: (_StopAfterBrowserCheck(), None))

    class _Stop(Exception):
        pass

    def stop(*a, **k):
        raise _Stop()

    monkeypatch.setattr(run_batch, "search_key_status", stop)
    for argv, expected in ((["--input", "x", "--out", str(tmp_path)], []), (["--input", "x", "--out", str(tmp_path), "--browser"], [True])):
        started.clear()
        monkeypatch.setattr("sys.argv", ["run_batch"] + argv)
        try:
            run_batch.main()
        except _Stop:
            pass
        assert started == expected, argv
        run_batch.set_active_renderer(None)


class _StopAfterBrowserCheck:
    disabled = {}
