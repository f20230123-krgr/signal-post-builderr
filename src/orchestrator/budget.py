"""
The single source of truth for remaining requests, spend, and wall-clock time
in the current batch.

Contract: docs/component-specs.md -> "src/orchestrator/budget.py"

Every stage that spends a gated resource (a request, a dollar of API spend,
time) must check in here first. This is what protects the 45-min / 2,000-
request / $10 limits in code, not just in a doc someone has to remember.

Request counting has two sources. Stages record one request per LOGICAL fetch
(`record_request`). Builderr, though, counts "2,000 total outbound requests,
including redirects and retries", so the governor can also be given a
`wire_counter` -- the process-wide count of requests that really went out
(src/pipeline/net.py) -- and enforces whichever of the two is higher.
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
    # Requests held back from the limit. Concurrent workers can each start one
    # last fetch (with redirects) after the can_spend_request() check passes,
    # so a run that stops exactly at the limit can land over it. Zero by
    # default so the limit means what it says; the CLI sets a real margin.
    request_safety_margin: int = 0


class BudgetGovernor:
    def __init__(
        self,
        limits: BudgetLimits | None = None,
        clock: Callable[[], float] = time.monotonic,
        wire_counter: Callable[[], int] | None = None,
        wire_baseline: int | None = None,
    ):
        self.limits = limits or BudgetLimits()
        self._clock = clock
        self._requests_used = 0
        # `wire_baseline` lets the first governor of a run be charged for
        # requests made before it existed (the startup key check, the universe
        # download); by default only requests made from now on count.
        self._wire_counter = wire_counter
        self._wire_baseline = (
            wire_baseline if wire_baseline is not None else (wire_counter() if wire_counter else 0)
        )
        self._spend_used = 0.0
        self._start_time = self._clock()

    def _elapsed_seconds(self) -> float:
        return self._clock() - self._start_time

    def _time_exhausted(self) -> bool:
        return self._elapsed_seconds() >= self.limits.max_wall_clock_seconds

    def _effective_requests(self) -> int:
        if self._wire_counter is None:
            return self._requests_used
        return max(self._requests_used, self._wire_counter() - self._wire_baseline)

    def can_spend_request(self) -> bool:
        if self._time_exhausted():
            return False
        return self._effective_requests() < self.limits.max_requests - self.limits.request_safety_margin

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
        return self._effective_requests()

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
        requests_ratio = self._effective_requests() / self.limits.max_requests
        spend_ratio = self._spend_used / self.limits.max_spend_usd
        time_ratio = self._elapsed_seconds() / self.limits.max_wall_clock_seconds
        return max(requests_ratio, spend_ratio, time_ratio) >= DEGRADE_THRESHOLD
