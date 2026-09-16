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
import os
from datetime import datetime, timezone
from typing import Callable, Optional

import httpx

import dataclasses

from src.models.profile import CompanyProfile, EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.assemble import assemble
from src.pipeline.crawl import DEFAULT_ATS_DOMAINS, DEFAULT_COMPANY_OWNED_PATHS, crawl
from src.pipeline.discovery import (
    ProviderHealth,
    discover_candidate_site,
    strip_leader_role_title,
    verify_discovered_site,
)
from src.pipeline.extract import extract
from src.pipeline.nav_jobs import NavJobIndex, build_nav_job_index, nav_hiring_signal_facts
from src.pipeline.registry_extras import (
    fetch_leadership_only,
    fetch_registry_extras,
    fetch_live_registry_details,
    fetch_registry_update_activity,
    name_is_held_by_another_entity,
    subunit_org_numbers,
    universe_identity_facts,
)
from src.pipeline.resolve import ResolvedEntity, resolve
from src.pipeline.verify import ConfirmedFact, verify
from src.storage.cache import ResponseCache
from src.storage.snapshots import SnapshotStore

logger = logging.getLogger(__name__)

ProcessOne = Callable[[str, BudgetGovernor, Optional[CompanyProfile]], CompanyProfile]


class DeadProviderAbort(RuntimeError):
    """Raised instead of finishing a run when `stop_if_key_dead` is set and a
    discovery provider's key is (or becomes) unusable.

    Opt-in, for local testing only: there's no point spending minutes on a
    run you'll throw away once a key is out of credits. It must never be the
    default -- Builderr runs the one-command instruction as-is, and a run that
    stops produces fewer profiles than the exactly-N-results hard gate
    requires. Raised (rather than returning partial profiles) so no caller
    can mistake a truncated run for a complete one."""

    def __init__(self, dead_providers: dict[str, str]):
        self.dead_providers = dict(dead_providers)
        details = "; ".join(f"{p}: {r}" for p, r in self.dead_providers.items())
        super().__init__(f"stopping run, discovery provider key unusable -- {details}")


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
    cache: Optional[ResponseCache] = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    provider_health: Optional[ProviderHealth] = None,
    nav_job_index: Optional[NavJobIndex] = None,
    exa_first_round_only: bool = False,
) -> CompanyProfile:
    """The real Resolve->Crawl->Extract->Verify->Assemble chain for one company.
    `universe_entry` (this company's record from Builderr's frozen universe
    manifest, see src/pipeline/universe.py) lets resolve() skip the live
    registry call entirely when available. `cache`, if given, is shared
    across the whole batch so a repeat fetch of the same (url, date) is free
    against the request budget (docs/component-specs.md)."""
    entity = resolve(org_number, client=client, universe_entry=universe_entry)
    # Fetched before discovery: its former names are a search tier below.
    live_details = fetch_live_registry_details(entity, budget, client=client, now=now)

    discovered_site_fact = None
    if (
        entity.resolution_state == EvidenceState.AVAILABLE
        and entity.official_site_candidate is None
        and entity.legal_name is not None
    ):
        # No website on file in the registry (~89% of a random universe
        # sample) -- try candidate discovery. Never trusted on its own:
        # verify_discovered_site independently re-fetches and name-matches
        # before anything is accepted. See discovery.py.
        #
        # Reading EXA_API_KEY/PARALLEL_API_KEY here, not inside discovery.py
        # itself, is deliberate -- per the evaluation-harness brief,
        # "server-side secrets supplied through documented environment
        # variables only." discovery.py stays a pure function of its
        # explicit inputs (same pattern as client/cache above); this is the
        # one place that resolves them from the environment.
        real_client = client or httpx.Client()
        try:
            exa_api_key = os.environ.get("EXA_API_KEY")
            parallel_api_key = os.environ.get("PARALLEL_API_KEY")

            def _discover_and_verify(
                query_name: str, alternate_names: Optional[list[str]] = None, first_round: bool = False
            ) -> Optional[ConfirmedFact]:
                # exa_first_round_only (opt-in, never the default): Exa is
                # used for the company-name search only and every fallback
                # round goes to the free providers -- roughly one paid
                # search per company instead of up to four.
                use_exa = first_round or not exa_first_round_only
                candidate = discover_candidate_site(
                    query_name, real_client, budget,
                    exa_api_key=exa_api_key if use_exa else None, parallel_api_key=parallel_api_key,
                    provider_health=provider_health, cache=cache,
                )
                if not candidate:
                    return None
                return verify_discovered_site(
                    candidate, entity.legal_name, real_client, budget, now=now, org_number=entity.org_number,
                    alternate_names=alternate_names,
                )

            discovered_site_fact = _discover_and_verify(entity.legal_name, first_round=True)

            if discovered_site_fact is None:
                # Leader/founder bridge (agent playbook §2): a verified CEO/
                # board-chair name (free, 100%-confidence, from Brreg's own
                # roller endpoint) is a fallback search seed when the
                # company name alone doesn't produce a VERIFIED site -- a
                # generic/short legal name is common among small Norwegian
                # holding/shell companies. Real-world regression: retrying
                # only when discover_candidate_site found nothing (rather
                # than when the whole discover+verify attempt failed) missed
                # the common case where a candidate WAS found but then
                # failed identity verification -- measured zero improvement
                # on a real batch until fixed to retry here instead. See
                # discover_candidate_site's docstring.
                #
                # fetch_registry_extras() below still re-fetches leadership
                # normally as part of its own flow -- one small, bounded
                # duplicate roles request, see fetch_leadership_only's
                # docstring for why that trade-off is acceptable.
                leadership_facts = fetch_leadership_only(entity, budget, client=real_client, now=now)
                for leader_fact in leadership_facts[:1]:
                    leader_name = strip_leader_role_title(leader_fact.value)
                    if leader_name:
                        discovered_site_fact = _discover_and_verify(f"{leader_name} {entity.legal_name}")

            if discovered_site_fact is None:
                # Third and last fallback tier: the exact 9-digit org number
                # itself as the query seed. An org number is a much stronger
                # match signal than any fuzzy name -- Norwegian business
                # directories, chambers of commerce and official filings
                # commonly cite it verbatim, so a page that mentions it is a
                # far more reliable candidate than a name-similarity match
                # could ever be. Still only a candidate: goes through the
                # exact same verify_discovered_site identity check (by legal
                # name, not the org number) as every other discovery result.
                discovered_site_fact = _discover_and_verify(entity.org_number)

            if (
                discovered_site_fact is None
                and live_details.former_names
                and not budget.should_degrade()
            ):
                # Fourth tier: the company's most recent former name. A company
                # renamed recently often still runs its site under the old
                # name, which every tier above rejects on name match. Limited
                # to ONE former name to cap the cost (a name check, a search
                # and a verify fetch), skipped once the budget is nearly spent,
                # and only used if no other registered entity currently holds
                # that name -- otherwise a site under it is probably theirs.
                former_name = live_details.former_names[0]
                if not name_is_held_by_another_entity(former_name, entity.org_number, budget, client=real_client):
                    discovered_site_fact = _discover_and_verify(former_name, alternate_names=[former_name])
        finally:
            if client is None:
                real_client.close()

    # `crawl_entity` is a "promoted" copy carrying the discovered site as its
    # official_site_candidate, used ONLY for crawl()/verify() (so the normal
    # allow-list/provenance logic treats it exactly like a registry site).
    # `entity` (the original, still official_site_candidate=None) is what
    # goes to assemble() -- keeping them separate is what lets
    # assemble.py's official_site fallback correctly label this claim
    # source_class="external" instead of mislabeling it "official_registry".
    crawl_entity = entity
    if discovered_site_fact:
        crawl_entity = dataclasses.replace(entity, official_site_candidate=discovered_site_fact.value)

    raw_facts = []
    if crawl_entity.resolution_state == EvidenceState.AVAILABLE:
        pages = crawl(
            crawl_entity,
            budget,
            client=client,
            company_owned_paths=DEFAULT_COMPANY_OWNED_PATHS,
            use_sitemap=True,
            ats_domains=DEFAULT_ATS_DOMAINS,
            cache=cache,
            now=now,
        )
        for page in pages:
            if page.fetch_state == EvidenceState.AVAILABLE:
                raw_facts.extend(extract(page, now=now))

    confirmed_facts = [c for c in (verify(f, crawl_entity) for f in raw_facts) if c is not None]
    if discovered_site_fact:
        confirmed_facts.append(discovered_site_fact)
    # Registry extras (roles/accounts/sub-units) are already-confirmed --
    # same authoritative registry as resolve.py, no identity risk to gate on.
    registry_facts, accounts_state = fetch_registry_extras(entity, budget, client=client)
    confirmed_facts += registry_facts
    # Industry/employees/legal form/status straight out of the universe record
    # already in memory -- zero requests, same registry trust tier.
    confirmed_facts += universe_identity_facts(org_number, universe_entry, now())
    # Dated registry-change events: the only dated_activity source that works
    # for the ~89% of companies with no website to crawl at all.
    confirmed_facts += fetch_registry_update_activity(entity, budget, client=client, now=now)
    confirmed_facts += live_details.facts
    if nav_job_index is not None:
        nav_client = client or httpx.Client()
        try:
            confirmed_facts += nav_hiring_signal_facts(
                entity, nav_job_index, nav_client, budget, now=now,
                subunit_org_numbers=subunit_org_numbers(registry_facts),
            )
        finally:
            if client is None:
                nav_client.close()
    return assemble(entity, confirmed_facts, previous_snapshot, accounts_state=accounts_state, now=now)


async def run_batch(
    org_numbers: list[str],
    budget: BudgetGovernor,
    concurrency: int = 10,
    process_one: Optional[ProcessOne] = None,
    snapshot_store: Optional[SnapshotStore] = None,
    universe: Optional[dict[str, dict]] = None,
    client: Optional[httpx.Client] = None,
    cache: Optional[ResponseCache] = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    provider_health: Optional[ProviderHealth] = None,
    stop_if_key_dead: bool = False,
    nav_job_index: Optional[NavJobIndex] = None,
    exa_first_round_only: bool = False,
) -> list[CompanyProfile]:
    """
    Run the full Resolve->Crawl->Extract->Verify->Assemble->Persist pipeline
    for each org number, bounded by an asyncio semaphore sized per
    docs/architecture.md's concurrency model.

    `universe`, if given, is Builderr's frozen company-universe manifest
    (src/pipeline/universe.py) keyed by org_number -- each company's matching
    entry (or None if not covered) is threaded to `process_one` as
    `universe_entry` so resolve() can skip the live registry call entirely.
    `cache`, if given, is shared across every company in the batch.

    Must: len(result) == len(org_numbers) always -- a stage failure produces
    a degraded profile, never a dropped one.
    """
    process_one = process_one or _default_process_one
    semaphore = asyncio.Semaphore(concurrency)

    def _abort_if_key_dead() -> None:
        if stop_if_key_dead and provider_health is not None and provider_health.disabled:
            raise DeadProviderAbort(provider_health.disabled)

    async def bounded(org_number: str) -> CompanyProfile:
        async with semaphore:
            _abort_if_key_dead()
            previous = snapshot_store.latest(org_number) if snapshot_store else None
            kwargs = {}
            if universe is not None:
                kwargs["universe_entry"] = universe.get(org_number)
            if client is not None:
                kwargs["client"] = client
            if cache is not None:
                kwargs["cache"] = cache
            # Only for the real pipeline -- a custom process_one injected by a
            # test keeps its own simpler signature.
            if provider_health is not None and process_one is _default_process_one:
                kwargs["provider_health"] = provider_health
            if nav_job_index is not None and process_one is _default_process_one:
                kwargs["nav_job_index"] = nav_job_index
            if exa_first_round_only and process_one is _default_process_one:
                kwargs["exa_first_round_only"] = True
            try:
                profile = await asyncio.to_thread(process_one, org_number, budget, previous, **kwargs)
            except Exception:
                logger.exception("run_batch: unhandled failure processing %s", org_number)
                profile = _degraded_profile(org_number, now)
            # Checked again after processing and before the snapshot write: the
            # company whose run killed the key was processed with degraded
            # discovery, so it must not enter snapshot history. Outside the
            # try/except above on purpose -- that handler turns crashes into
            # degraded profiles, and this must stop the run instead.
            _abort_if_key_dead()
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
    cache: Optional[ResponseCache] = None,
    budget_limits: Optional[BudgetLimits] = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    provider_health: Optional[ProviderHealth] = None,
    stop_if_key_dead: bool = False,
    use_nav_jobs: bool = False,
    exa_first_round_only: bool = False,
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
    # Shared across every chunk on purpose: an exhausted API key stays
    # exhausted for the whole run, so chunk 2 must not rediscover the same
    # 402 the hard way that chunk 1 already paid for.
    provider_health = provider_health if provider_health is not None else ProviderHealth()

    all_profiles: list[CompanyProfile] = []
    total_requests = 0
    total_spend = 0.0
    chunk_count = 0

    nav_job_index: Optional[NavJobIndex] = None

    for start in range(0, len(org_numbers), chunk_size):
        chunk = org_numbers[start : start + chunk_size]
        budget = BudgetGovernor(budget_limits)
        if use_nav_jobs and nav_job_index is None:
            # Once per run, not per chunk: the feed is the same for every
            # company, so a 1,000-company run pays for it once. Built on the
            # first chunk's budget so those requests count against a real
            # 2,000-request envelope rather than bypassing it.
            nav_client = client or httpx.Client()
            try:
                nav_job_index = build_nav_job_index(nav_client, budget, now=now)
            finally:
                if client is None:
                    nav_client.close()
        profiles = await run_batch(
            chunk,
            budget,
            concurrency=concurrency,
            process_one=process_one,
            snapshot_store=snapshot_store,
            universe=universe,
            client=client,
            cache=cache,
            now=now,
            provider_health=provider_health,
            stop_if_key_dead=stop_if_key_dead,
            nav_job_index=nav_job_index,
            exa_first_round_only=exa_first_round_only,
        )
        all_profiles.extend(profiles)
        total_requests += budget.requests_used
        total_spend += budget.spend_used
        chunk_count += 1

    return all_profiles, {
        "requests_used": total_requests,
        "spend_used_usd": total_spend,
        "chunk_count": chunk_count,
        # Surfaced so whoever runs the batch sees a exhausted/invalid API key
        # in the run report instead of silently getting worse coverage.
        "degraded_providers": provider_health.disabled,
        "nav_job_index": nav_job_index.report() if nav_job_index is not None else None,
        "search_spend_usd": provider_health.spent_usd,
    }
