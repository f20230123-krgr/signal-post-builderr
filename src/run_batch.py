"""
One-command CLI entrypoint. This is the exact command referenced in the
submission requirement: "a one-command run instruction."

Usage:
    python -m src.run_batch --input batch.jsonl --out results/

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
import time
from datetime import datetime, timezone
from pathlib import Path

from src.orchestrator.runner import run_in_chunks
from src.pipeline.envelope import to_envelope
from src.pipeline.universe import load_universe
from src.reporting import render_html_report
from src.storage.cache import ResponseCache
from src.storage.snapshots import SnapshotStore

DEFAULT_UNIVERSE_PATH = Path("signalpost-company-universe-2025.jsonl.gz")


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
    args = parser.parse_args()

    org_numbers = [
        line.strip() for line in args.input.read_text().splitlines() if line.strip()
    ]
    args.out.mkdir(parents=True, exist_ok=True)

    universe_path = args.universe or (DEFAULT_UNIVERSE_PATH if DEFAULT_UNIVERSE_PATH.exists() else None)
    universe = load_universe(universe_path) if universe_path else None

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
    profiles, aggregate = asyncio.run(
        run_in_chunks(
            org_numbers,
            chunk_size=args.chunk_size,
            snapshot_store=snapshot_store,
            universe=universe,
            cache=cache,
        )
    )
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
        "requests_used": aggregate["requests_used"],
        "spend_used_usd": aggregate["spend_used_usd"],
        "wall_clock_seconds": round(wall_clock_seconds, 3),
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


if __name__ == "__main__":
    main()
