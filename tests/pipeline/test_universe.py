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
