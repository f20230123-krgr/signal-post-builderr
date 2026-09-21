"""
Local recall/precision/coverage estimator, run before every submission.

Contract: docs/component-specs.md -> "src/scoring/self_check.py"
Invoked by: .claude/commands/self-score.md (the /self-score slash command)

Must report the same metric definitions Builderr uses (weighted company
recall, external precision, 70/30 coverage split) so numbers are directly
comparable to the targets in docs/success-criteria.md. (Builderr no longer treats
coverage/recall/precision as qualification thresholds; these are our own bars.)

Metric definitions (docs/problem-statement.md):
  - weighted_company_recall: % of labeled companies whose legal_name was
    found and correctly matches the hand-verified label.
  - external_precision: of the companies where we PUBLISHED something
    (legal_name state == available), what fraction was the correct company.
    A batch that publishes nothing is not a precision violation (100% is
    vacuously correct -- see docs/success-criteria.md "never publish a claim
    on the wrong company", which is about wrong publications, not omissions).
  - coverage_score (0-35): 70% company-level recall + 30% claim-level recall,
    per the split described in problem-statement.md's "Coverage detail".
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from rapidfuzz import fuzz

from src.models.profile import CompanyProfile, EvidenceState

NAME_MATCH_THRESHOLD = 90

_FIELD_TO_CLAIMS: dict[str, Callable[[CompanyProfile], list[Optional[str]]]] = {
    "official_site": lambda p: [p.online_presence.official_site.value],
    "public_brand": lambda p: [p.legal_identity.public_brand.value],
    "annual_accounts_latest": lambda p: [p.annual_accounts.latest.value],
    "leader": lambda p: [c.value for c in p.leadership.leaders],
    "workplace": lambda p: [c.value for c in p.leadership.workplaces],
    "company_profile": lambda p: [c.value for c in p.online_presence.company_profiles],
    "hiring_signal": lambda p: [c.value for c in p.activity.hiring_signals],
    "dated_activity": lambda p: [c.value for c in p.activity.dated_activity],
    # Free registry identity fields (registry_extras.universe_identity_facts).
    # Optional on the profile, so guard against None rather than assuming.
    "industry": lambda p: [p.legal_identity.industry.value if p.legal_identity.industry else None],
    "employee_count": lambda p: [p.legal_identity.employee_count.value if p.legal_identity.employee_count else None],
    "legal_form": lambda p: [p.legal_identity.legal_form.value if p.legal_identity.legal_form else None],
    "operating_status": lambda p: [
        p.legal_identity.operating_status.value if p.legal_identity.operating_status else None
    ],
    "founded_date": lambda p: [p.legal_identity.founded_date.value if p.legal_identity.founded_date else None],
}


@dataclass
class SelfCheckReport:
    coverage_score: float  # out of 35
    weighted_company_recall: float  # 0-100 (%)
    external_precision: float  # 0-100 (%)

    def meets_targets(self) -> bool:
        return (
            self.coverage_score >= 21
            and self.weighted_company_recall >= 60.0
            and self.external_precision >= 95.0
        )

    # Kept for callers written before the rules were revised.
    passes_hard_gates = meets_targets


def _load_labels(fixtures_dir: Path) -> list[dict]:
    expected_dir = Path(fixtures_dir) / "expected"
    labels = []
    for path in sorted(expected_dir.glob("*.json")):
        labels.append(json.loads(path.read_text(encoding="utf-8")))
    return labels


def _names_match(a: Optional[str], b: str) -> bool:
    if not a:
        return False
    return fuzz.token_sort_ratio(a.lower(), b.lower()) >= NAME_MATCH_THRESHOLD


def _field_matches(profile: CompanyProfile, field_name: str, expected_substring: str) -> bool:
    getter = _FIELD_TO_CLAIMS.get(field_name)
    if getter is None:
        return False
    values = [v for v in getter(profile) if v]
    return any(expected_substring.lower() in v.lower() for v in values)


_UNIVERSE_CACHE: Optional[dict] = None


def _universe_entry(org_number: str) -> Optional[dict]:
    """This company's record from the frozen universe manifest, if present.

    The real runner resolves from the universe and mines it for free identity
    facts, so a harness that skipped it would score a pipeline that isn't the
    one shipping. Loaded once and cached -- the file is large and this runs
    across every fixture."""
    global _UNIVERSE_CACHE
    if _UNIVERSE_CACHE is None:
        from pathlib import Path as _Path

        from src.pipeline.universe import load_universe

        manifest = _Path("signalpost-company-universe-2025.jsonl.gz")
        _UNIVERSE_CACHE = load_universe(manifest) if manifest.exists() else {}
    return _UNIVERSE_CACHE.get(org_number)


def _default_process_company(org_number: str) -> CompanyProfile:
    """Real end-to-end pipeline run for one company -- used for actual
    /self-score invocations. Not exercised by this module's own pytest suite
    (see tests/scoring/test_self_check.py), which injects a fixture-backed
    process_company to keep the test suite network-free."""
    from src.pipeline.assemble import assemble
    from src.pipeline.crawl import DEFAULT_COMPANY_OWNED_PATHS, crawl
    from src.pipeline.extract import extract
    from src.pipeline.registry_extras import (
        fetch_live_registry_details,
        fetch_registry_extras,
        fetch_registry_update_activity,
        universe_identity_facts,
    )
    from src.pipeline.resolve import resolve
    from src.pipeline.verify import verify
    from src.orchestrator.budget import BudgetGovernor

    budget = BudgetGovernor()
    universe_entry = _universe_entry(org_number)
    entity = resolve(org_number, universe_entry=universe_entry)
    raw_facts = []
    if entity.resolution_state == EvidenceState.AVAILABLE:
        pages = crawl(entity, budget, company_owned_paths=DEFAULT_COMPANY_OWNED_PATHS)
        for page in pages:
            if page.fetch_state == EvidenceState.AVAILABLE:
                raw_facts.extend(extract(page))
    confirmed = [c for c in (verify(f, entity) for f in raw_facts) if c is not None]
    registry_facts, accounts_state = fetch_registry_extras(entity, budget)
    confirmed += registry_facts
    # Keep this harness on the same path the real runner takes -- it drifted
    # once already (the runner gained registry update events while this kept
    # scoring without them, quietly measuring a different pipeline than the
    # one that actually ships).
    confirmed += fetch_registry_update_activity(entity, budget)
    confirmed += universe_identity_facts(org_number, universe_entry, entity.retrieved_at)
    confirmed += fetch_live_registry_details(entity, budget).facts
    return assemble(entity, confirmed, previous_snapshot=None, accounts_state=accounts_state)


def run_self_check(
    fixtures_dir: Path,
    process_company: Optional[Callable[[str], CompanyProfile]] = None,
) -> SelfCheckReport:
    """
    Run the pipeline against the hand-labeled fixtures in `fixtures_dir` and
    compute coverage/recall/precision against the labels.
    """
    labels = _load_labels(fixtures_dir)
    process_company = process_company or _default_process_company

    n_labels = len(labels)
    correct_company_count = 0
    published_count = 0
    published_correct_count = 0
    claim_hits = 0
    claim_total = 0

    for label in labels:
        profile = process_company(label["org_number"])
        name_claim = profile.legal_identity.legal_name
        published = name_claim.state == EvidenceState.AVAILABLE
        correct = published and _names_match(name_claim.value, label["expected_legal_name"])

        if correct:
            correct_company_count += 1
        if published:
            published_count += 1
            if correct:
                published_correct_count += 1

        for field_name, expected_value in label.get("expected_fields", {}).items():
            claim_total += 1
            if correct and _field_matches(profile, field_name, expected_value):
                claim_hits += 1

    company_recall = (correct_company_count / n_labels * 100) if n_labels else 0.0
    claim_recall = (claim_hits / claim_total * 100) if claim_total else 0.0
    external_precision = (
        (published_correct_count / published_count * 100) if published_count else 100.0
    )
    coverage_score = 35.0 * (0.7 * (company_recall / 100) + 0.3 * (claim_recall / 100))

    return SelfCheckReport(
        coverage_score=coverage_score,
        weighted_company_recall=company_recall,
        external_precision=external_precision,
    )


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Run the Signalpost local self-check harness.")
    parser.add_argument("--fixtures", required=True, type=Path, help="Path to fixtures/ directory")
    args = parser.parse_args()

    report = run_self_check(args.fixtures)
    print(f"coverage_score: {report.coverage_score:.2f} / 35 (target: >=21)")
    print(f"weighted_company_recall: {report.weighted_company_recall:.2f}% (target: >=60%)")
    print(f"external_precision: {report.external_precision:.2f}% (target: >=95%)")
    print(f"meets_targets: {report.meets_targets()}")


if __name__ == "__main__":
    main()
