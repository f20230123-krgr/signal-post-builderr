"""
Selects the >=1,000-company submission corpus from Builderr's frozen
universe file (src/pipeline/universe.py).

Contract: docs/component-specs.md -> "src/pipeline/selection.py"

Deterministic (seeded) so the exact same manifest can be regenerated
byte-for-byte on a clean checkout -- part of "reproducible setup" (hard gate).
Sorting org numbers before sampling means the result never depends on the
universe dict's insertion order, only on (universe contents, count, seed).
"""
from __future__ import annotations

import random


def select_companies(universe: dict[str, dict], count: int, seed: int) -> list[str]:
    """Return `count` org numbers sampled from `universe`, sorted. If `count`
    covers the whole universe (or more), returns every org number."""
    org_numbers = sorted(universe.keys())
    if count >= len(org_numbers):
        return org_numbers
    rng = random.Random(seed)
    return sorted(rng.sample(org_numbers, count))
