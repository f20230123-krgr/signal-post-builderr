"""
Select a deterministic sample of organisation numbers from Builderr's frozen
company-universe file, for the >=1,000-company submission corpus.

Usage:
    python -m src.select_entry_batch \
        --universe signalpost-company-universe-2025.jsonl.gz \
        --count 1000 \
        --output entry-companies.jsonl
"""
from __future__ import annotations

import argparse
from pathlib import Path

from src.pipeline.selection import select_companies
from src.pipeline.universe import load_universe


def main() -> None:
    parser = argparse.ArgumentParser(description="Select N companies from Builderr's frozen universe file.")
    parser.add_argument("--universe", required=True, type=Path)
    parser.add_argument("--count", type=int, default=1000, help="At least 1000 for a valid submission")
    parser.add_argument("--seed", type=int, default=20260823)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    universe = load_universe(args.universe)
    selected = select_companies(universe, args.count, args.seed)
    args.output.write_text("\n".join(selected) + "\n", encoding="utf-8")
    print(f"Selected {len(selected)} of {len(universe)} companies (seed={args.seed}) -> {args.output}")


if __name__ == "__main__":
    main()
