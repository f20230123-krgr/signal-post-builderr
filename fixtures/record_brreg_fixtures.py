"""
One-off script to record REAL registry responses from the official
Brønnøysundregisteret Enhetsregisteret API into fixtures/registry/.

Run manually and review the diff before committing -- fixtures must never be
silently overwritten from a live crawl (fixtures/README.md). Not part of the
test suite; the test suite only ever reads the recorded JSON files this
script produces, never the network.

Usage:
    python fixtures/record_brreg_fixtures.py
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/"
FIXTURES_DIR = Path(__file__).parent / "registry"

# Real, publicly registered Norwegian companies -- picked because each has a
# `hjemmeside` (official site) on file, giving crawl/extract/verify something
# real to chain from.
ORG_NUMBERS = [
    "923609016",  # EQUINOR ASA
    "997770234",  # KAHOOT! AS
    "925836613",  # NORSK TIPPING AS
    "991753591",  # GELATO ASA
    "000000000",  # does not exist -- records the real 404 shape
]


EXTRA_ENDPOINTS = {
    "roller": "https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller",
    "regnskap": "https://data.brreg.no/regnskapsregisteret/regnskap/{org}",
    "underenheter": "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org}",
}


def _fetch(url: str) -> tuple[int, object]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "signalpost-agent-fixture-recorder"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, None


def record(org_number: str) -> None:
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    url = BASE_URL + org_number
    out_path = FIXTURES_DIR / f"{org_number}.json"
    status, body = _fetch(url)
    out_path.write_text(json.dumps({"status": status, "body": body}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"recorded {org_number} -> {out_path.name} ({status})")


def record_extra(org_number: str, kind: str, url_template: str) -> None:
    extra_dir = FIXTURES_DIR / kind
    extra_dir.mkdir(parents=True, exist_ok=True)
    url = url_template.format(org=org_number)
    out_path = extra_dir / f"{org_number}.json"
    status, body = _fetch(url)
    out_path.write_text(json.dumps({"status": status, "body": body}, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"recorded {org_number} -> {kind}/{out_path.name} ({status})")


if __name__ == "__main__":
    for org_number in ORG_NUMBERS:
        record(org_number)
        if org_number != "000000000":
            for kind, template in EXTRA_ENDPOINTS.items():
                record_extra(org_number, kind, template)
