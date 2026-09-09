"""
Export layer: CompanyProfile -> the submission envelope shape.

Contract: docs/component-specs.md -> "src/pipeline/envelope.py"

Added after reviewing the real challenge site. `src/models/profile.py`'s
nested `CompanyProfile` is our internal, Pydantic-validated model -- good for
enforcing invariants at assemble-time. But the Signalpost reference agent's
`OUTPUT_CONTRACT.md` defines the actual submission shape as a flat envelope:
a `claims` list (each referencing evidence by id) plus a separate,
deduplicated `evidence` list, alongside `run`/`changes`/`errors`/`operations`.
This module converts one to the other so the SUBMITTED JSONL matches what the
evaluator almost certainly parses, without throwing away the internal model's
correctness guarantees.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.models.profile import Claim, CompanyProfile, EvidenceState

_ERROR_STATES = {EvidenceState.FAILED, EvidenceState.BLOCKED}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _claim_entries(profile: CompanyProfile) -> list[tuple[str, Claim]]:
    """(field_name, Claim) pairs in a fixed order, matching the field names
    used throughout the pipeline (RawFact/ConfirmedFact field_name tags),
    with "official_site" renamed to "official_website" to match
    OUTPUT_CONTRACT.md's own example field name."""
    entries: list[tuple[str, Claim]] = [
        ("legal_name", profile.legal_identity.legal_name),
        ("public_brand", profile.legal_identity.public_brand),
        ("annual_accounts_latest", profile.annual_accounts.latest),
        ("official_website", profile.online_presence.official_site),
    ]
    entries += [("annual_accounts_history", c) for c in profile.annual_accounts.history]
    entries += [("leader", c) for c in profile.leadership.leaders]
    entries += [("workplace", c) for c in profile.leadership.workplaces]
    entries += [("company_profile", c) for c in profile.online_presence.company_profiles]
    entries += [("hiring_signal", c) for c in profile.activity.hiring_signals]
    entries += [("dated_activity", c) for c in profile.activity.dated_activity]
    return entries


def to_envelope(
    profile: CompanyProfile,
    run_id: str,
    operations: Optional[dict] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
) -> dict:
    """
    Build one submission envelope for `profile`, matching the reference
    agent's OUTPUT_CONTRACT.md shape exactly (claims/evidence/run/changes/
    errors/operations).

    Evidence is deduplicated by (source_url, content_hash) -- claims that
    came from the same page/response (e.g. two activity signals scraped off
    one page) share a single evidence entry, referenced by id, rather than
    repeating identical evidence for every claim.
    """
    ts = profile.run_timestamp
    started_at = started_at or ts
    completed_at = completed_at or ts
    operations = operations or {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0}

    evidence_ids_by_key: dict[tuple[str, Optional[str]], str] = {}
    evidence_list: list[dict] = []
    claims: list[dict] = []
    errors: list[dict] = []

    for field_name, claim in _claim_entries(profile):
        evidence_ids: list[str] = []
        if claim.state == EvidenceState.AVAILABLE and claim.source:
            key = (claim.source, claim.content_hash)
            evidence_id = evidence_ids_by_key.get(key)
            if evidence_id is None:
                evidence_id = f"ev-{len(evidence_list) + 1}"
                evidence_ids_by_key[key] = evidence_id
                evidence_list.append(
                    {
                        "id": evidence_id,
                        "source_url": claim.source,
                        "source_class": claim.source_class,
                        "retrieved_at": _iso(claim.retrieved_at) if claim.retrieved_at else None,
                        "content_sha256": claim.content_hash,
                        "claim_span": claim.value,
                        "linked_from": claim.linked_from,
                    }
                )
            evidence_ids = [evidence_id]

        claims.append(
            {
                "field": field_name,
                "value": claim.value,
                "availability": claim.state.value,
                "confidence": round(claim.match_confidence / 100, 4) if claim.match_confidence is not None else None,
                "evidence_ids": evidence_ids,
            }
        )

        if claim.state in _ERROR_STATES:
            errors.append({"field": field_name, "state": claim.state.value})

    return {
        "organisation_number": profile.org_number,
        "run": {
            "run_id": run_id,
            "started_at": _iso(started_at),
            "completed_at": _iso(completed_at),
            "terminal_status": "completed",
        },
        "claims": claims,
        "evidence": evidence_list,
        "changes": list(profile.refresh_metadata.material_changes),
        "errors": errors,
        "operations": operations,
    }
