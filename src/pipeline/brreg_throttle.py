"""
Brønnøysund's "slow down": one shared pause for every registry request.

When the registry answers 429 Too Many Requests (or 503 with a Retry-After), every company's
registry data used to be lost for that call: each worker retried at once or gave up. Now the
answer sets one shared pause (the time the registry asks for, capped), every worker waits it
out before its next registry request, and the throttled request is retried a bounded number
of times. Measured 2026-10-17: two batches run at once got the registry to refuse requests and
75 of 100 companies lost their roles and accounts.

Every retry is a real request and is counted by the wire counter (src/pipeline/net.py) like
any other; nothing here changes the request budget.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import httpx

MAX_WAIT_SECONDS = 5.0
DEFAULT_WAIT_SECONDS = 1.0
MAX_THROTTLE_RETRIES = 2

_lock = threading.Lock()
_paused_until = 0.0


def is_throttle(response: Optional[httpx.Response]) -> bool:
    """429, or a 503 that says when to come back."""
    if response is None:
        return False
    return response.status_code == 429 or (response.status_code == 503 and "retry-after" in response.headers)


def note_throttle(response: httpx.Response, clock: Callable[[], float] = time.monotonic) -> float:
    """Start (or extend) the shared pause the registry asked for; returns its length."""
    global _paused_until
    try:
        wait = float(response.headers.get("retry-after", DEFAULT_WAIT_SECONDS))
    except ValueError:  # an HTTP date instead of seconds: wait the default
        wait = DEFAULT_WAIT_SECONDS
    wait = max(0.0, min(wait, MAX_WAIT_SECONDS))
    with _lock:
        _paused_until = max(_paused_until, clock() + wait)
    return wait


def wait_if_paused(sleep: Callable[[float], None], clock: Callable[[], float] = time.monotonic) -> None:
    """Wait out the shared pause, if one is running."""
    with _lock:
        remaining = _paused_until - clock()
    if remaining > 0:
        sleep(min(remaining, MAX_WAIT_SECONDS))


def reset() -> None:
    """Clear the pause (tests)."""
    global _paused_until
    with _lock:
        _paused_until = 0.0
