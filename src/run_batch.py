"""
One-command CLI entrypoint. This is the exact command referenced in the
submission requirement: "a one-command run instruction."

Usage:
    python -m src.run_batch --input batch.jsonl --out results/

    Add --exa-first-round-only and/or --max-search-spend USD to limit the
    RUNNER'S OWN spend (e.g. when generating a submission corpus on a personal
    key). Neither is on by default, so an evaluator's one-command run uses
    Exa on every search round, still bounded by the per-batch limits.

    Add --stop-if-key-dead for local testing: if an Exa/Parallel key is out of
    credits or rejected (at startup or mid-run), print a warning and exit
    instead of finishing a run you'd throw away. Never use it for the graded
    run -- a stopped run produces fewer profiles than the brief requires.

Writes into --out:
    envelopes.jsonl  -- one submission envelope per input (docs: envelope.py)
    manifest.txt     -- the exact organisation-number manifest processed
    run-report.json  -- machine-readable run report (requests/spend/runtime)
    snapshots/       -- append-only CompanyProfile history (docs: snapshots.py)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from src.orchestrator.budget import BudgetLimits
from src.orchestrator.runner import DeadProviderAbort, run_in_chunks
from src.pipeline.discovery import PROVIDER_COST_PER_SEARCH_USD, ProviderHealth, check_provider_keys
from src.pipeline.envelope import to_envelope
from src.pipeline.net import new_client, wire_request_breakdown, wire_request_count
from src.pipeline.universe import ensure_universe, load_universe
from src.reporting import render_html_report
from src.storage.cache import ResponseCache
from src.storage.snapshots import SnapshotStore

DEFAULT_UNIVERSE_PATH = Path("signalpost-company-universe-2025.jsonl.gz")

# Requests held back from the 2,000-per-run limit. Builderr counts every
# redirect hop and retry; concurrent workers can each start one last fetch
# after the budget check passes, so stopping exactly at the limit could land
# over it. 10 workers x (a fetch and a couple of redirects) fits well inside 60.
REQUEST_SAFETY_MARGIN = 60


def prepare_universe(
    explicit_path: Optional[Path], default_path: Path, client: httpx.Client
) -> tuple[Optional[Path], int]:
    """Which universe manifest to use, fetching it when a clean checkout has none.

    An explicit --universe is always used as given. Otherwise the default file
    is used if present and downloaded (hash-verified) if not. Returns
    (path_or_None, requests_used); None means "carry on with live lookups"."""
    if explicit_path is not None:
        return explicit_path, 0
    return ensure_universe(default_path, client)


def _print_dead_key_banner(dead: dict[str, str], headline: str, closing: list[str]) -> None:
    print("!" * 72)
    print(headline)
    for provider, reason in dead.items():
        print(f"  - {provider}: {reason}")
    print("")
    for line in closing:
        print(f"  {line}")
    print("!" * 72)


def startup_key_check(
    stop_if_key_dead: bool,
    client: Optional[httpx.Client] = None,
    spend_cap_usd: Optional[float] = None,
) -> tuple[ProviderHealth, int]:
    """Check every configured discovery key once, before any company runs.

    Returns (provider_health, requests_used). A dead key is disabled up front,
    so the run doesn't even make the first doomed call. With
    `stop_if_key_dead`, a dead key prints a warning and exits with status 1
    before anything is processed or written; without it, the warning is
    printed and the run continues (the graded-run path)."""
    owns_client = client is None
    client = client or new_client()
    try:
        dead, requests_used = check_provider_keys(
            client,
            exa_api_key=os.environ.get("EXA_API_KEY"),
            parallel_api_key=os.environ.get("PARALLEL_API_KEY"),
        )
    finally:
        if owns_client:
            client.close()

    if dead and stop_if_key_dead:
        _print_dead_key_banner(
            dead,
            "STOPPED: a discovery API key is unusable -- nothing was processed.",
            ["Top up or replace the key, then re-run.",
             "(--stop-if-key-dead is set; without it the run would continue with",
             "reduced website-discovery coverage.)"],
        )
        sys.exit(1)

    # Disabled only on the continue path: disabling logs "remaining tiers will
    # be used instead", which would contradict a STOPPED banner.
    health = ProviderHealth(spend_cap_usd=spend_cap_usd)
    # The startup check itself is one paid Exa search when that key is set.
    if os.environ.get("EXA_API_KEY"):
        health.reserve_spend("exa", PROVIDER_COST_PER_SEARCH_USD["exa"])
    for provider, reason in dead.items():
        health.disable(provider, reason)

    if dead:
        _print_dead_key_banner(
            dead,
            "WARNING: a discovery API key is unusable -- continuing without it.",
            ["Every profile will still be valid, but website-discovery coverage",
             "will be materially lower. Top up or replace the key to recover it."],
        )
    return health, requests_used


def main() -> None:
    parser = argparse.ArgumentParser(description="Run a Signalpost company batch.")
    parser.add_argument("--input", required=True, type=Path, help="JSONL of org numbers")
    parser.add_argument("--out", required=True, type=Path, help="Output directory")
    parser.add_argument(
        "--universe",
        type=Path,
        default=None,
        help="Builderr's frozen company-universe manifest (.jsonl/.jsonl.gz). "
        f"Defaults to ./{DEFAULT_UNIVERSE_PATH} if present -- see src/pipeline/universe.py.",
    )
    parser.add_argument("--run-id", default=None, help="Defaults to a UTC timestamp")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=100,
        help="Companies per fresh budget envelope -- 100 matches the locked "
        "daily-evaluation batch size (docs/problem-statement.md).",
    )
    parser.add_argument(
        "--stop-if-key-dead",
        action="store_true",
        help="Local testing only: exit with a warning if an Exa/Parallel key is out of "
        "credits or rejected, at startup or mid-run, instead of finishing the run. "
        "Do not use for the graded run -- a stopped run produces too few profiles.",
    )
    parser.add_argument(
        "--exa-first-round-only",
        action="store_true",
        help="Limit your own spend: use Exa only for the company-name search; fallback "
        "search rounds use the free providers. Off by default.",
    )
    parser.add_argument(
        "--max-search-spend",
        type=float,
        default=None,
        help="Limit your own spend: hard cap in USD on paid search calls across the WHOLE run. "
        "When reached, the paid provider is switched off and the run continues on the free "
        "ones. Off by default (the per-batch $10 limit always applies).",
    )
    args = parser.parse_args()

    # Before anything is created or written, so a stop leaves no output behind.
    provider_health, _ = startup_key_check(
        args.stop_if_key_dead, spend_cap_usd=args.max_search_spend
    )

    org_numbers = [
        line.strip() for line in args.input.read_text().splitlines() if line.strip()
    ]
    args.out.mkdir(parents=True, exist_ok=True)

    # A clean checkout has no universe file: fetch it (hash-verified) or, if that
    # fails, carry on with live registry lookups -- see src/pipeline/universe.py.
    startup_client = new_client()
    try:
        universe_path, _ = prepare_universe(args.universe, DEFAULT_UNIVERSE_PATH, startup_client)
    finally:
        startup_client.close()
    universe = load_universe(universe_path) if universe_path else None
    # Everything sent so far (key check, universe download) belongs to this
    # evaluated run, so the first chunk's request budget is charged for it.
    startup_wire_requests = wire_request_count()

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    # SnapshotStore lives under --out so every run is append-only and the
    # next run's refresh diffing has real prior history to compare against --
    # this is what makes "idempotent refresh" actually happen end-to-end, not
    # just pass in isolated unit tests.
    snapshot_store = SnapshotStore(args.out / "snapshots")
    # Durable across the whole run (and future runs against the same --out)
    # so a repeat fetch of the same (url, date) never re-spends budget --
    # see crawl.py's `cache` param / docs/component-specs.md "cache hits free".
    cache = ResponseCache(args.out / "cache.sqlite3")

    wall_clock_start = time.perf_counter()
    try:
        profiles, aggregate = asyncio.run(
            run_in_chunks(
                org_numbers,
                chunk_size=args.chunk_size,
                snapshot_store=snapshot_store,
                universe=universe,
                cache=cache,
                provider_health=provider_health,
                stop_if_key_dead=args.stop_if_key_dead,
                use_nav_jobs=True,
                exa_first_round_only=args.exa_first_round_only,
                budget_limits=BudgetLimits(request_safety_margin=REQUEST_SAFETY_MARGIN),
                # Builderr counts "outbound requests, including redirects and
                # retries": enforce the real number, not one per logical fetch.
                wire_counter=wire_request_count,
                startup_wire_requests=startup_wire_requests,
            )
        )
    except DeadProviderAbort as abort:
        # No envelopes, report or manifest are written. Snapshots for companies
        # that finished while every key still worked are kept: they're valid,
        # and snapshot history is append-only by design.
        _print_dead_key_banner(
            abort.dead_providers,
            "STOPPED: a discovery API key became unusable mid-run.",
            ["No envelopes or run report were written for this run.",
             "Top up or replace the key, then re-run."],
        )
        sys.exit(1)
    wall_clock_seconds = time.perf_counter() - wall_clock_start

    # Per-company operations (requests/cost/runtime) aren't separately
    # attributed under concurrency yet -- BudgetGovernor is intentionally one
    # shared counter per chunk (that's what makes the 2,000-request cap
    # enforceable at all). Each envelope's `operations` therefore reports
    # zeros rather than a misleading guess; accurate aggregate totals are in
    # run-report.json below. Known gap, not silently glossed over.
    envelopes = [to_envelope(profile, run_id=run_id) for profile in profiles]
    (args.out / "envelopes.jsonl").write_text(
        "\n".join(json.dumps(e) for e in envelopes) + "\n", encoding="utf-8"
    )

    (args.out / "manifest.txt").write_text("\n".join(org_numbers) + "\n", encoding="utf-8")

    run_report = {
        "run_id": run_id,
        "input_count": len(org_numbers),
        "profile_count": len(profiles),
        "universe_used": universe_path is not None,
        "chunk_size": args.chunk_size,
        "chunk_count": aggregate["chunk_count"],
        # Real outbound requests as Builderr counts them (redirect hops and
        # retries included), startup key check and universe download included.
        "requests_used": aggregate["requests_used"],
        # Independent whole-process count of every request actually sent.
        "outbound_requests_measured": wire_request_count(),
        "outbound_requests_breakdown": wire_request_breakdown(),
        "request_safety_margin": REQUEST_SAFETY_MARGIN,
        # Real, recorded cost: paid search calls (including the startup key
        # check) priced per PROVIDER_COST_PER_SEARCH_USD.
        "spend_used_usd": round(aggregate.get("search_spend_usd", aggregate["spend_used_usd"]), 4),
        "spend_limits": {
            "exa_first_round_only": args.exa_first_round_only,
            "max_search_spend_usd": args.max_search_spend,
        },
        "wall_clock_seconds": round(wall_clock_seconds, 3),
        # Empty on a healthy run. Non-empty means a discovery provider's key
        # was exhausted or rejected mid-run and the chain fell back to weaker
        # tiers -- the run still completes and every profile is still valid,
        # but website-discovery coverage is materially lower than it should
        # be. Measured: the same 100-company batch found 19 official websites
        # with a healthy Exa key and 10-11 without.
        "degraded_providers": aggregate.get("degraded_providers", {}),
        # NAV hiring-signal index for this run: list pages read (hard-capped),
        # requests spent, active ads indexed, and whether the page cap cut off
        # the newest ads.
        "nav_job_index": aggregate.get("nav_job_index"),
    }
    (args.out / "run-report.json").write_text(json.dumps(run_report, indent=2), encoding="utf-8")

    # Decision-useful synthesis + a legible desktop/mobile results view --
    # see src/synthesis.py / src/reporting.py.
    html_report = render_html_report(profiles, generated_at=datetime.now(timezone.utc))
    (args.out / "report.html").write_text(html_report, encoding="utf-8")

    print(f"Produced {len(profiles)} profiles for {len(org_numbers)} inputs.")
    print(f"  envelopes: {args.out / 'envelopes.jsonl'}")
    print(f"  manifest:  {args.out / 'manifest.txt'}")
    print(f"  report:    {args.out / 'run-report.json'}")
    print(f"  html view: {args.out / 'report.html'}")

    # Loud, last, and impossible to scroll past: a quiet key exhaustion looks
    # exactly like "this batch just had fewer discoverable websites", which
    # is how it went unnoticed twice during development.
    degraded = run_report["degraded_providers"]
    if degraded:
        print()
        _print_dead_key_banner(
            degraded,
            "WARNING: a discovery provider failed permanently during this run.",
            ["The run completed and every profile is valid, but website-discovery",
             "coverage is materially lower than it should be. Top up or replace the",
             "API key and re-run to recover it."],
        )


if __name__ == "__main__":
    main()
