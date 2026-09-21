"""
Tests for src/pipeline/universe.py.

Contract: docs/component-specs.md -> "src/pipeline/universe.py"

Builderr provides a frozen, hash-verified company-universe file for identity
anchoring (docs/problem-statement.md / signalpost-evaluation-harness.md:
"Builderr provides the frozen official registry snapshot used for identity
anchoring"). Loading it lets resolve() skip the live registry call entirely
for any org_number it covers -- free, byte-identical across every entrant.
"""
import gzip
import json

from src.pipeline.universe import load_universe

SAMPLE_LINES = [
    {
        "organisation_number": "923609016",
        "name": "EQUINOR ASA",
        "legal_form": "ASA",
        "employees": 21239,
        "bankrupt": False,
        "liquidating": False,
        "municipality": "STAVANGER",
        "municipality_number": "1103",
        "industry_code": "06.100",
        "industry_label": "Utvinning av raolje",
        "website": "www.equinor.com",
        "latest_submitted_accounts": "2025",
    },
    {
        "organisation_number": "810034882",
        "name": "SANDNES ELEKTRISKE AS",
        "legal_form": "AS",
        "employees": 11,
        "bankrupt": False,
        "liquidating": False,
        "municipality": "SANDNES",
        "municipality_number": "1108",
        "industry_code": "43.210",
        "industry_label": "Elektrisk installasjonsarbeid",
        "website": "",
        "latest_submitted_accounts": "2025",
    },
]


def test_loads_plain_jsonl_keyed_by_org_number(tmp_path):
    path = tmp_path / "universe.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in SAMPLE_LINES), encoding="utf-8")

    universe = load_universe(path)

    assert set(universe.keys()) == {"923609016", "810034882"}
    assert universe["923609016"]["name"] == "EQUINOR ASA"
    assert universe["923609016"]["website"] == "www.equinor.com"


def test_loads_gzipped_jsonl(tmp_path):
    path = tmp_path / "universe.jsonl.gz"
    content = "\n".join(json.dumps(line) for line in SAMPLE_LINES).encode("utf-8")
    path.write_bytes(gzip.compress(content))

    universe = load_universe(path)

    assert universe["810034882"]["municipality"] == "SANDNES"


def test_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "universe.jsonl"
    path.write_text(json.dumps(SAMPLE_LINES[0]) + "\n\n\n", encoding="utf-8")

    universe = load_universe(path)

    assert len(universe) == 1


# ---- fetching the manifest when a clean checkout doesn't have it ---------

import hashlib

import httpx

from src.pipeline.universe import UNIVERSE_ARCHIVE_SHA256, UNIVERSE_URL, ensure_universe


def _archive() -> bytes:
    return gzip.compress("\n".join(json.dumps(line) for line in SAMPLE_LINES).encode("utf-8"))


def _serving(body: bytes, status: int = 200, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        return httpx.Response(status, content=body)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_the_published_archive_hash_is_pinned():
    """Builderr publishes both hashes in the evaluation contract; this is the
    compressed archive's."""
    assert UNIVERSE_ARCHIVE_SHA256 == "1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384"
    assert UNIVERSE_URL == "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz"


def test_an_existing_file_is_used_and_nothing_is_requested(tmp_path):
    path = tmp_path / "universe.jsonl.gz"
    path.write_bytes(_archive())
    seen = []

    result, requests_used = ensure_universe(path, _serving(b"", seen=seen), expected_sha256="0" * 64)

    assert result == path and requests_used == 0 and seen == []


def test_a_missing_file_is_downloaded_verified_and_saved(tmp_path):
    body = _archive()
    path = tmp_path / "universe.jsonl.gz"
    seen = []

    result, requests_used = ensure_universe(
        path, _serving(body, seen=seen), expected_sha256=hashlib.sha256(body).hexdigest(), url="https://x.test/u.gz"
    )

    assert result == path and requests_used == 1
    assert seen == ["https://x.test/u.gz"]
    assert set(load_universe(result)) == {"923609016", "810034882"}


def test_a_download_that_does_not_match_the_published_hash_is_discarded(tmp_path):
    path = tmp_path / "universe.jsonl.gz"

    result, requests_used = ensure_universe(path, _serving(_archive()), expected_sha256="0" * 64)

    assert result is None and requests_used == 1
    assert not path.exists()
    assert list(tmp_path.iterdir()) == []  # no half-written leftovers either


def test_a_failed_download_falls_back_to_no_universe(tmp_path):
    path = tmp_path / "universe.jsonl.gz"

    result, requests_used = ensure_universe(path, _serving(b"", status=503), expected_sha256="0" * 64)

    assert result is None and requests_used == 1
    assert not path.exists()


def test_a_network_error_falls_back_to_no_universe(tmp_path):
    def handler(request):
        raise httpx.ConnectError("no route")

    result, _ = ensure_universe(
        tmp_path / "universe.jsonl.gz", httpx.Client(transport=httpx.MockTransport(handler)), expected_sha256="0" * 64
    )

    assert result is None


def test_an_unwritable_location_uses_the_fallback_directory(tmp_path):
    body = _archive()
    blocker = tmp_path / "file-not-dir"
    blocker.write_text("x")  # a path under a regular file can never be created
    fallback = tmp_path / "fallback"

    result, _ = ensure_universe(
        blocker / "universe.jsonl.gz", _serving(body), expected_sha256=hashlib.sha256(body).hexdigest(),
        fallback_dir=fallback,
    )

    assert result == fallback / "universe.jsonl.gz"
    assert set(load_universe(result)) == {"923609016", "810034882"}
