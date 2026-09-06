"""
Wires the pipeline stages together for N companies concurrently.

Contract: docs/component-specs.md -> "src/orchestrator/runner.py"

Must guarantee exactly 100 terminal results are written for a 100-company
batch, even if some companies fail every stage -- they still get a profile,
correctly marked with failure states, per CLAUDE.md's working agreement
("never let a stage crash the batch").

Concurrency note (reconciles docs/architecture.md): the stages are
synchronous, httpx.Client-based functions (chosen so tests can use
httpx.MockTransport without an event loop -- see resolve.py/crawl.py). This
runner still bounds in-flight work with an asyncio.Semaphore as the
concurrency model prescribes; each company's synchronous pipeline just runs
via `asyncio.to_thread` rather than native async I/O. Same cap, same
I/O-bound-concurrency benefit, less async surface area to keep bug-free
against the October deadline.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

from src.models.profile import CompanyProfile, EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.assemble import assemble
from src.pipeline.crawl import DEFAULT_COMPANY_OWNED_PATHS, crawl
from src.pipeline.extract import extract
from src.pipeline.registry_extras import fetch_registry_extras
from src.pipeline.resolve import ResolvedEntity, resolve
from src.pipeline.verify import verify
from src.storage.snapshots import SnapshotStore

logger = logging.getLogger(__name__)

ProcessOne = Callable[[str, BudgetGovernor, Optional[CompanyProfile]], CompanyProfile]


def _degraded_profile(org_number: str, now: Callable[[], datetime]) -> CompanyProfile:
    """Terminal, honestly-FAILED profile for a company whose pipeline crashed
    unexpectedly -- guarantees the batch still emits one record per input."""
    fallback_entity = ResolvedEntity(
        org_number=org_number,
        legal_name=None,
        registered_address=None,
        official_site_candidate=None,
        resolution_state=EvidenceState.FAILED,
        source="",
        retrieved_at=now(),
    )
    return assemble(fallback_entity, confirmed_facts=[], previous_snapshot=None, now=now)


def _default_process_one(
    org_number: str,
    budget: BudgetGovernor,
    previous_snapshot: Optional[CompanyProfile] = None,
    universe_entry: Optional[dict] = None,
    client: Optional[httpx.Client] = None,
) -> CompanyProfile:
    """The real Resolve->Crawl->Extract->Verify->Assemble chain for one company.
    `universe_entry` (this company's record from Builderr's frozen universe
    manifest, see src/pipeline/universe.py) lets resolve() skip the live
    registry call entirely when available."""
    entity = resolve(org_number, client=client, universe_entry=universe_entry)

    raw_facts = []
    if entity.resolution_state == EvidenceState.AVAILABLE:
        pages = crawl(entity, budget, client=client, company_owned_paths=DEFAULT_COMPANY_OWNED_PATHS)
        for page in pages:
            if page.fetch_state == EvidenceState.AVAILABLE:
                raw_facts.extend(extract(page))

    confirmed_facts = [c for c in (verify(f, entity) for f in raw_facts) if c is not None]
    # Registry extras (roles/accounts/sub-units) are already-confirmed --
    # same authoritative registry as resolve.py, no identity risk to gate on.
    confirmed_facts += fetch_registry_extras(entity, budget, client=client)
    return assemble(entity, confirmed_facts, previous_snapshot)


async def run_batch(
    org_numbers: list[str],
    budget: BudgetGovernor,
    concurrency: int = 10,
    process_one: Optional[ProcessOne] = None,
    snapshot_store: Optional[SnapshotStore] = None,
    universe: Optional[dict[str, dict]] = None,
    client: Optional[httpx.Client] = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[CompanyProfile]:
    """
    Run the full Resolve->Crawl->Extract->Verify->Assemble->Persist pipeline
    for each org number, bounded by an asyncio semaphore sized per
    docs/architecture.md's concurrency model.

    `universe`, if given, is Builderr's frozen company-universe manifest
    (src/pipeline/universe.py) keyed by org_number -- each company's matching
    entry (or None if not covered) is threaded to `process_one` as
    `universe_entry` so resolve() can skip the live registry call entirely.

    Must: len(result) == len(org_numbers) always -- a stage failure produces
    a degraded profile, never a dropped one.
    """
    process_one = process_one or _default_process_one
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(org_number: str) -> CompanyProfile:
        async with semaphore:
            previous = snapshot_store.latest(org_number) if snapshot_store else None
            kwargs = {}
            if universe is not None:
                kwargs["universe_entry"] = universe.get(org_number)
            if client is not None:
                kwargs["client"] = client
            try:
                profile = await asyncio.to_thread(process_one, org_number, budget, previous, **kwargs)
            except Exception:
                logger.exception("run_batch: unhandled failure processing %s", org_number)
                profile = _degraded_profile(org_number, now)
            if snapshot_store is not None:
                snapshot_store.append(profile)
            return profile

    return list(await asyncio.gather(*(bounded(org_number) for org_number in org_numbers)))


async def run_in_chunks(
    org_numbers: list[str],
    chunk_size: int = 100,
    concurrency: int = 10,
    process_one: Optional[ProcessOne] = None,
    snapshot_store: Optional[SnapshotStore] = None,
    universe: Optional[dict[str, dict]] = None,
    client: Optional[httpx.Client] = None,
    budget_limits: Optional[BudgetLimits] = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> tuple[list[CompanyProfile], dict]:
    """
    Run a submission corpus larger than one daily batch as sequential
    `chunk_size`-company chunks, each with its OWN fresh BudgetGovernor.

    Why: docs/problem-statement.md's 2,000-request/45-minute/$10 envelope is
    the *daily 100-company evaluation batch*'s budget, not a cap on the
    one-time >=1,000-company submission corpus. Chunking this way means
    generating that corpus exercises the agent exactly as the real daily
    evaluation will (each chunk faithfully bounded), rather than either
    silently ignoring the envelope for one giant run or arbitrarily scaling
    the caps up.

    Returns (all_profiles, aggregate_report) where aggregate_report has
    requests_used/spend_used_usd/chunk_count summed across every chunk.
    """
    all_profiles: list[CompanyProfile] = []
    total_requests = 0
    total_spend = 0.0
    chunk_count = 0

    for start in range(0, len(org_numbers), chunk_size):
        chunk = org_numbers[start : start + chunk_size]
        budget = BudgetGovernor(budget_limits)
        profiles = await run_batch(
            chunk,
            budget,
            concurrency=concurrency,
            process_one=process_one,
            snapshot_store=snapshot_store,
            universe=universe,
            client=client,
            now=now,
        )
        all_profiles.extend(profiles)
        total_requests += budget.requests_used
        total_spend += budget.spend_used
        chunk_count += 1

    return all_profiles, {
        "requests_used": total_requests,
        "spend_used_usd": total_spend,
        "chunk_count": chunk_count,
    }
