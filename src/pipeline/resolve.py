"""
Stage 1: Resolve.

Contract: docs/component-specs.md -> "src/pipeline/resolve.py"

Responsibility: turn a bare organization number into a verified entity record
by querying the official registry only. Never fabricate a name/address on
failure -- return an explicit state instead.

Registry: Brønnøysundregisteret's public Enhetsregisteret API
(https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}), a free,
unauthenticated, documented public API -- no rate-limit key required. Chosen
over other options because it's the canonical source and returns a unique
record per org number, no ambiguity to resolve on the lookup itself.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from src.models.profile import EvidenceState

REGISTRY_BASE_URL = "https://data.brreg.no/enhetsregisteret/api/enheter/"

# Capped retry policy for transient failures (timeouts, 5xx). Never retry
# indefinitely -- give up cleanly and report FAILED, per component-specs.md.
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 0.5


@dataclass
class ResolvedEntity:
    org_number: str
    legal_name: Optional[str]
    registered_address: Optional[str]
    official_site_candidate: Optional[str]
    resolution_state: EvidenceState
    source: str
    retrieved_at: datetime
    # SHA-256 of the exact registry content this entity was built from --
    # signalpost-sources.md: "every claim records ... content hash". None
    # when there's nothing to hash (not_available/ambiguous/failed).
    content_hash: Optional[str] = None


def _format_address(addr: Optional[dict]) -> Optional[str]:
    if not addr:
        return None
    lines = addr.get("adresse") or []
    parts = list(lines)
    postnummer = addr.get("postnummer")
    poststed = addr.get("poststed")
    if postnummer or poststed:
        parts.append(" ".join(p for p in (postnummer, poststed) if p))
    return ", ".join(p for p in parts if p) or None


def _normalize_site(hjemmeside: Optional[str]) -> Optional[str]:
    if not hjemmeside:
        return None
    if re.match(r"^https?://", hjemmeside, re.IGNORECASE):
        return hjemmeside
    return f"https://{hjemmeside}"


def _has_ambiguous_current_name(body: dict) -> bool:
    historical = body.get("historiskeNavn") or []
    current_count = sum(1 for entry in historical if entry.get("tilDato") is None)
    return current_count > 1


UNIVERSE_SOURCE_NAME = "signalpost-company-universe-2025.jsonl.gz"


def _resolve_from_universe(
    org_number: str, universe_entry: dict, now: Callable[[], datetime]
) -> ResolvedEntity:
    """Build a ResolvedEntity straight from Builderr's frozen universe manifest
    -- no network call, byte-identical for every entrant. `municipality` is a
    coarser stand-in for street-level registered_address (the universe file
    doesn't carry a street address); honest and sufficient since verify.py
    doesn't depend on address granularity."""
    canonical = json.dumps(universe_entry, sort_keys=True).encode("utf-8")
    return ResolvedEntity(
        org_number=org_number,
        legal_name=universe_entry.get("name"),
        registered_address=universe_entry.get("municipality"),
        official_site_candidate=_normalize_site(universe_entry.get("website")),
        resolution_state=EvidenceState.AVAILABLE,
        source=UNIVERSE_SOURCE_NAME,
        retrieved_at=now(),
        content_hash=hashlib.sha256(canonical).hexdigest(),
    )


def resolve(
    org_number: str,
    client: Optional[httpx.Client] = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    universe_entry: Optional[dict] = None,
) -> ResolvedEntity:
    """
    Query the official registry for `org_number` and return a ResolvedEntity.
    If `universe_entry` is given (this org_number's record from Builderr's
    frozen company-universe manifest, see src/pipeline/universe.py), it is
    used directly and no network call is made at all.

    Must:
      - Query only the official registry (or its documented public API).
      - Return resolution_state=AMBIGUOUS rather than guessing on multiple matches.
    Must not:
      - Fabricate legal_name/registered_address on failure; use
        NOT_AVAILABLE/FAILED instead and leave the value fields None.
    """
    if universe_entry is not None:
        return _resolve_from_universe(org_number, universe_entry, now)

    owns_client = client is None
    client = client or httpx.Client()
    url = REGISTRY_BASE_URL + org_number

    try:
        response = None
        last_error: Optional[Exception] = None
        for attempt in range(MAX_RETRIES):
            try:
                response = client.get(url, timeout=10.0)
                if response.status_code >= 500:
                    last_error = httpx.HTTPStatusError(
                        f"server error {response.status_code}", request=response.request, response=response
                    )
                    response = None
                    if attempt < MAX_RETRIES - 1:
                        sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                    continue
                break
            except httpx.TransportError as exc:
                last_error = exc
                response = None
                if attempt < MAX_RETRIES - 1:
                    sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))

        retrieved_at = now()

        if response is None:
            return ResolvedEntity(
                org_number=org_number,
                legal_name=None,
                registered_address=None,
                official_site_candidate=None,
                resolution_state=EvidenceState.FAILED,
                source=url,
                retrieved_at=retrieved_at,
            )

        if response.status_code == 404:
            return ResolvedEntity(
                org_number=org_number,
                legal_name=None,
                registered_address=None,
                official_site_candidate=None,
                resolution_state=EvidenceState.NOT_AVAILABLE,
                source=url,
                retrieved_at=retrieved_at,
            )

        response.raise_for_status()
        body = response.json()
        content_hash = hashlib.sha256(response.content).hexdigest()

        if _has_ambiguous_current_name(body):
            return ResolvedEntity(
                org_number=org_number,
                legal_name=None,
                registered_address=None,
                official_site_candidate=None,
                resolution_state=EvidenceState.AMBIGUOUS,
                source=url,
                retrieved_at=retrieved_at,
            )

        return ResolvedEntity(
            org_number=org_number,
            legal_name=body.get("navn"),
            registered_address=_format_address(body.get("forretningsadresse")),
            official_site_candidate=_normalize_site(body.get("hjemmeside")),
            resolution_state=EvidenceState.AVAILABLE,
            source=url,
            retrieved_at=retrieved_at,
            content_hash=content_hash,
        )
    finally:
        if owns_client:
            client.close()
