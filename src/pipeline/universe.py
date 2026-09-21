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

The file is not stored in this repository (12.6 MB, and Builderr publishes it),
so a clean checkout won't have it. `ensure_universe` fetches it once from
Builderr, verifies it against the SHA-256 Builderr publishes, and saves it;
if that fails for any reason the run simply continues on live registry lookups.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional, Union

import httpx

logger = logging.getLogger(__name__)

UNIVERSE_URL = "https://builderr.ai/signalpost-company-universe-2025.jsonl.gz"
# SHA-256 of the compressed archive, as published in Builderr's evaluation
# contract (docs/signalpost-evaluation-harness.md, "Corpus").
UNIVERSE_ARCHIVE_SHA256 = "1c89710e5b01f8617e86d09fbdff4a52f2f8dbbba297e74f7164b5984f5a0384"
DOWNLOAD_TIMEOUT_SECONDS = 120.0


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


def _save_atomically(directory: Path, name: str, body: bytes) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    fd, temp_name = tempfile.mkstemp(dir=directory, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
        os.replace(temp_name, target)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    return target


def ensure_universe(
    path: Union[str, Path],
    client: httpx.Client,
    expected_sha256: Optional[str] = None,
    url: str = UNIVERSE_URL,
    fallback_dir: Optional[Union[str, Path]] = None,
) -> tuple[Optional[Path], int]:
    """Return (path_to_the_manifest, requests_used).

    An existing file is used as it is. Otherwise the archive is downloaded (one
    request) and kept only if its SHA-256 matches the published one -- a
    truncated, tampered or wrong file is discarded, never half-saved. If `path`
    can't be written the file goes to `fallback_dir` (the system temp directory
    by default). Any failure returns (None, requests_used): the caller carries
    on with live registry lookups, which is slower for identity but correct.
    """
    path = Path(path)
    if path.exists():
        return path, 0

    expected = (expected_sha256 or UNIVERSE_ARCHIVE_SHA256).lower()
    try:
        response = client.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True)
    except (httpx.HTTPError, UnicodeError) as exc:
        logger.warning("could not download the company-universe manifest (%s); using live registry lookups", exc)
        return None, 1
    if response.status_code != 200:
        logger.warning("company-universe download returned HTTP %s; using live registry lookups", response.status_code)
        return None, 1

    body = response.content
    if hashlib.sha256(body).hexdigest() != expected:
        logger.warning("downloaded company-universe manifest failed its SHA-256 check; discarding it")
        return None, 1

    for directory in (path.parent, Path(fallback_dir) if fallback_dir else Path(tempfile.gettempdir())):
        try:
            return _save_atomically(directory, path.name, body), 1
        except OSError:
            continue
    logger.warning("could not save the company-universe manifest anywhere; using live registry lookups")
    return None, 1
