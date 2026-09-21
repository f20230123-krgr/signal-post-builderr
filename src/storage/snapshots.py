"""
Append-only profile storage + refresh diffing.

Contract: docs/component-specs.md -> "src/storage/snapshots.py"

Must: every write is a NEW row/file keyed by (org_number, run_timestamp).
Must not: never mutate or delete an existing snapshot. This is what makes
"idempotent refresh" a hard gate we can actually test, not just a promise.

`history()` is an additive read method beyond the documented latest()/append()
pair -- it exists purely so tests (and operators) can inspect that appends are
genuinely additive, not to change the append/latest contract.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Union

from src.models.profile import CompanyProfile

# What counts as a "material" change for refresh diffing, per
# docs/testing-strategy.md section 6: legal_name, leadership (leaders +
# workplaces), official_site, annual_accounts.latest, or hiring_signals
# added/removed. Whitespace/formatting-only differences and re-fetches with a
# new retrieved_at on an otherwise-unchanged value are NOT material. Registry
# identity facts (employee_count, legal_form, industry) count once both
# snapshots carry them.
_WHITESPACE_RE = re.compile(r"\s+")


def _normalized(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    return _WHITESPACE_RE.sub(" ", value).strip()


def _claim_values(claims: list) -> set:
    return {_normalized(c.value) for c in claims if _normalized(c.value) is not None}


def diff_material_changes(
    previous: Optional[CompanyProfile], new: CompanyProfile
) -> list[str]:
    """
    Compare `new` against `previous` (the prior snapshot, or None on first
    run) and return a human-readable list of material changes. Only compares
    claim *values*, never `retrieved_at`, so a re-fetch of unchanged content
    never produces noise.
    """
    if previous is None:
        return []

    changes: list[str] = []

    old_legal_name = _normalized(previous.legal_identity.legal_name.value)
    new_legal_name = _normalized(new.legal_identity.legal_name.value)
    if old_legal_name != new_legal_name:
        changes.append(f"legal_name: {old_legal_name!r} -> {new_legal_name!r}")

    old_site = _normalized(previous.online_presence.official_site.value)
    new_site = _normalized(new.online_presence.official_site.value)
    if old_site != new_site:
        changes.append(f"official_site: {old_site!r} -> {new_site!r}")

    old_latest = _normalized(previous.annual_accounts.latest.value)
    new_latest = _normalized(new.annual_accounts.latest.value)
    old_period = previous.annual_accounts.latest.reporting_period
    new_period = new.annual_accounts.latest.reporting_period
    if old_latest != new_latest:
        changes.append(f"annual_accounts.latest: {old_latest!r} -> {new_latest!r}")
    elif old_period != new_period and (old_period is not None or new_period is not None):
        # A new filing can report an identical figure to the prior year --
        # still new information (real evaluator feedback: "a new financial
        # reporting period was missed when the amount stayed the same").
        # Compared separately from the value so this can't be masked by the
        # value-equality check above.
        changes.append(f"annual_accounts.latest reporting_period: {old_period!r} -> {new_period!r}")

    old_leaders = _claim_values(previous.leadership.leaders)
    new_leaders = _claim_values(new.leadership.leaders)
    if old_leaders != new_leaders:
        changes.append(f"leadership.leaders: {sorted(old_leaders)} -> {sorted(new_leaders)}")

    old_workplaces = _claim_values(previous.leadership.workplaces)
    new_workplaces = _claim_values(new.leadership.workplaces)
    if old_workplaces != new_workplaces:
        changes.append(
            f"leadership.workplaces: {sorted(old_workplaces)} -> {sorted(new_workplaces)}"
        )

    # Going bankrupt or into liquidation is the most decision-relevant change a
    # company can make between runs. Only compared when BOTH snapshots carry
    # the field: snapshots written before it existed have none, and treating
    # "absent -> Active" as a change would flag every company on the first
    # run after upgrading.
    old_status_claim = previous.legal_identity.operating_status
    new_status_claim = new.legal_identity.operating_status
    if old_status_claim is not None and new_status_claim is not None:
        old_status = _normalized(old_status_claim.value)
        new_status = _normalized(new_status_claim.value)
        if old_status and new_status and old_status != new_status:
            changes.append(f"operating_status: {old_status!r} -> {new_status!r}")

    # Live registry facts that genuinely change between runs. Same rule as
    # operating_status: only compared when BOTH snapshots carry the claim, so
    # snapshots written before a claim existed never look like a change.
    for label, previous_claim, new_claim in (
        ("employee_count", previous.legal_identity.employee_count, new.legal_identity.employee_count),
        ("legal_form", previous.legal_identity.legal_form, new.legal_identity.legal_form),
        ("industry", previous.legal_identity.industry, new.legal_identity.industry),
    ):
        if previous_claim is None or new_claim is None:
            continue
        old_value, new_value = _normalized(previous_claim.value), _normalized(new_claim.value)
        if old_value and new_value and old_value != new_value:
            changes.append(f"{label}: {old_value!r} -> {new_value!r}")

    old_hiring = _claim_values(previous.activity.hiring_signals)
    new_hiring = _claim_values(new.activity.hiring_signals)
    added = new_hiring - old_hiring
    removed = old_hiring - new_hiring
    if added:
        changes.append(f"hiring_signal added: {sorted(added)}")
    if removed:
        changes.append(f"hiring_signal removed: {sorted(removed)}")

    return changes


class SnapshotStore:
    """Append-only, keyed by (org_number, run_timestamp). One JSONL file per
    org_number under `base_dir`; each line is a full CompanyProfile."""

    def __init__(self, base_dir: Union[str, Path]):
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, org_number: str) -> Path:
        return self._base_dir / f"{org_number}.jsonl"

    def history(self, org_number: str) -> list[CompanyProfile]:
        """All snapshots for org_number, oldest first. Additive read helper,
        see module docstring."""
        path = self._path(org_number)
        if not path.exists():
            return []
        profiles = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    profiles.append(CompanyProfile.model_validate_json(line))
        return profiles

    def latest(self, org_number: str) -> Optional[CompanyProfile]:
        """Return the most recent prior snapshot for org_number, or None."""
        history = self.history(org_number)
        return history[-1] if history else None

    def append(self, profile: CompanyProfile) -> None:
        """
        Write `profile` as a new snapshot. Must never overwrite or delete any
        existing snapshot for this org_number -- always opened in append mode.
        """
        path = self._path(profile.org_number)
        with path.open("a", encoding="utf-8") as f:
            f.write(profile.model_dump_json())
            f.write("\n")
