"""
The single source of truth for remaining requests, spend, and wall-clock time
in the current batch.

Contract: docs/component-specs.md -> "src/orchestrator/budget.py"

Every stage that spends a gated resource (a request, a dollar of API spend,
time) must check in here first. This is what protects the 45-min / 2,000-
request / $10 hard gates in code, not just in a doc someone has to remember.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

# Fraction of any single budget dimension consumed before should_degrade()
# starts telling callers to stop pulling new data (see docs/architecture.md
# "graceful degrade path").
DEGRADE_THRESHOLD = 0.9


@dataclass
class BudgetLimits:
    max_requests: int = 2000
    max_spend_usd: float = 10.0
    max_wall_clock_seconds: float = 45 * 60


class BudgetGovernor:
    def __init__(
        self,
        limits: BudgetLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.limits = limits or BudgetLimits()
        self._clock = clock
        self._requests_used = 0
        self._spend_used = 0.0
        self._start_time = self._clock()

    def _elapsed_seconds(self) -> float:
        return self._clock() - self._start_time

    def _time_exhausted(self) -> bool:
        return self._elapsed_seconds() >= self.limits.max_wall_clock_seconds

    def can_spend_request(self) -> bool:
        if self._time_exhausted():
            return False
        return self._requests_used < self.limits.max_requests

    def record_request(self) -> None:
        self._requests_used += 1

    def can_spend_usd(self, amount: float) -> bool:
        if self._time_exhausted():
            return False
        return self._spend_used + amount <= self.limits.max_spend_usd

    def record_spend(self, amount: float) -> None:
        self._spend_used += amount

    @property
    def requests_used(self) -> int:
        """Read-only, for run-report reporting (src/run_batch.py) -- never
        used by stages to gate spend, that's can_spend_request()'s job."""
        return self._requests_used

    @property
    def spend_used(self) -> float:
        return self._spend_used

    @property
    def elapsed_seconds(self) -> float:
        return self._elapsed_seconds()

    def should_degrade(self) -> bool:
        """
        True when any budget is close enough to exhausted that callers should
        stop pulling new data and move straight to writing out whatever it has
        already, correctly flagged NOT_AVAILABLE/BLOCKED, per docs/architecture.md.
        """
        requests_ratio = self._requests_used / self.limits.max_requests
        spend_ratio = self._spend_used / self.limits.max_spend_usd
        time_ratio = self._elapsed_seconds() / self.limits.max_wall_clock_seconds
        return max(requests_ratio, spend_ratio, time_ratio) >= DEGRADE_THRESHOLD
