"""
Loader for the frozen company-universe manifest Builderr provides for
identity anchoring (signalpost-company-universe-2025.jsonl.gz).

Contract: docs/component-specs.md -> "src/pipeline/universe.py"

Why this exists: the evaluation contract states "Builderr provides the frozen
official registry snapshot used for identity anchoring" -- this file is
byte-identical for every entrant (both the compressed archive and its
decompressed content are SHA-256-published in the brief) and already carries
legal_name/website/legal_form/employees/municipality per organisation number.
Using it instead of a live Brreg call for basic identity is free (no request
budget spent), instant, and inherently reproducible across runs/entrants --
resolve.py falls back to the live registry only for org numbers not covered
here (e.g. ad-hoc testing outside the 411,160-company eligible universe).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Union


def load_universe(path: Union[str, Path]) -> dict[str, dict]:
    """Load the universe JSONL (optionally gzipped) into a dict keyed by
    organisation_number. Each value is the raw record as published."""
    path = Path(path)
    opener = gzip.open if path.suffix == ".gz" else open

    entries: dict[str, dict] = {}
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            entries[record["organisation_number"]] = record
    return entries
