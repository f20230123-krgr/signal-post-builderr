"""
Outbound HTTP for the whole agent: one client factory, two guarantees.

1. Every request that really goes out is counted -- including each redirect
   hop and each retry. Builderr's evaluation contract limits a run to "2,000
   total outbound requests, including redirects and retries", but the stages
   record one request per logical fetch, so a page that redirects
   http -> https -> www, or is retried after a 503, was invisible to the
   budget governor. `wire_request_count()` is the real number; the governor
   (src/orchestrator/budget.py) enforces whichever is higher.

2. No request is sent to a private, loopback or link-local address, and only
   http/https are allowed. The agent follows URLs it finds on third-party
   pages (search results, feeds, social links, redirects); the evaluation
   contract requires a safe outbound URL policy, and this is it.

Both are httpx event hooks, which httpx calls for EVERY request it sends,
redirect hops included, so they cannot be bypassed by follow_redirects.
"""
from __future__ import annotations

import collections
import ipaddress
import socket
import threading
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

# httpx follows up to 20 redirects by default. A site whose robots.txt redirects
# to itself forever cost ~190 requests for one company (20 hops, retried, over
# several fetches) -- a tenth of a batch's limit. Five hops covers every real
# http -> https -> www -> canonical chain.
MAX_REDIRECTS = 5

_wire_lock = threading.Lock()
_wire_requests = 0
_wire_by_host: collections.Counter = collections.Counter()
_wire_redirect_hops = 0
_wire_server_errors = 0


def wire_request_count() -> int:
    """Requests sent by every client built with new_client() so far in this
    process. Compare two readings to measure a stretch of work."""
    with _wire_lock:
        return _wire_requests


def wire_request_breakdown(top_hosts: int = 15) -> dict:
    """Where the requests went: the total, the busiest hosts, how many were
    redirect hops (3xx answers that were followed) and how many came back as a
    5xx (the ones our retry loops repeat). Written to run-report.json so a
    request-limit problem can be diagnosed from a run rather than guessed at."""
    with _wire_lock:
        return {
            "total": _wire_requests,
            "redirect_hops": _wire_redirect_hops,
            "server_error_responses": _wire_server_errors,
            "by_host": dict(_wire_by_host.most_common(top_hosts)) if top_hosts else dict(_wire_by_host),
        }


def _count_request(request: httpx.Request) -> None:
    global _wire_requests
    with _wire_lock:
        _wire_requests += 1
        _wire_by_host[request.url.host] += 1


def _count_response(response: httpx.Response) -> None:
    global _wire_redirect_hops, _wire_server_errors
    with _wire_lock:
        if 300 <= response.status_code < 400 and "location" in response.headers:
            _wire_redirect_hops += 1
        elif response.status_code >= 500:
            _wire_server_errors += 1


class UnsafeOutboundUrl(httpx.TransportError):
    """Raised instead of sending a request the outbound policy refuses.

    An httpx.HTTPError on purpose: every fetch helper already turns those into
    a FAILED state for that one company, so a refused URL degrades a single
    fetch and never crashes a batch."""


# Names that only ever mean "inside": refused without a DNS lookup.
_INTERNAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa", ".intranet", ".corp")


def _is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global


class OutboundPolicy:
    """Decides whether one URL may be requested.

    A hostname is resolved and every address it maps to must be public. A name
    that doesn't resolve is let through: it can't reach an internal service,
    the request just fails, and refusing it would break every dead or guessed
    domain. Answers are cached per host, so a site crawled page by page is
    looked up once.

    Known limit: the check resolves the name before the request and the HTTP
    client resolves it again, so a hostile DNS server could answer differently
    the second time (DNS rebinding). That needs a resolver we control; it is
    documented rather than pretended away."""

    def __init__(self, resolver: Callable = socket.getaddrinfo):
        self._resolver = resolver
        self._verdicts: dict[str, bool] = {}
        self._lock = threading.Lock()

    def check(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            raise UnsafeOutboundUrl(f"refused outbound URL with scheme {parts.scheme!r}: {url}")
        host = (parts.hostname or "").strip().lower().rstrip(".")
        if not host:
            raise UnsafeOutboundUrl(f"refused outbound URL with no host: {url}")

        with self._lock:
            verdict = self._verdicts.get(host)
        if verdict is None:
            verdict = self._host_is_public(host)
            with self._lock:
                self._verdicts[host] = verdict
        if not verdict:
            raise UnsafeOutboundUrl(f"refused outbound URL to a non-public address: {host}")

    def _host_is_public(self, host: str) -> bool:
        if host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
            return False
        try:
            return _is_public_ip(ipaddress.ip_address(host))
        except ValueError:
            pass  # a name, not an IP literal

        try:
            answers = self._resolver(host, None)
        except (socket.gaierror, UnicodeError, OSError):
            return True
        for answer in answers:
            try:
                if not _is_public_ip(ipaddress.ip_address(answer[4][0].split("%")[0])):
                    return False
            except (ValueError, IndexError):
                return False
        return True


_default_policy = OutboundPolicy()


def new_client(policy: Optional[OutboundPolicy] = None, **kwargs) -> httpx.Client:
    """The only way the agent should build an httpx.Client.

    The policy check runs first, so a refused request is neither sent nor
    counted. Extra keyword arguments (e.g. `transport=` in tests) pass through."""
    active = policy or _default_policy

    def guard(request: httpx.Request) -> None:
        active.check(str(request.url))

    kwargs.setdefault("max_redirects", MAX_REDIRECTS)
    hooks = kwargs.pop("event_hooks", None) or {}
    request_hooks = [guard, _count_request, *hooks.get("request", [])]
    response_hooks = [_count_response, *hooks.get("response", [])]
    return httpx.Client(event_hooks={**hooks, "request": request_hooks, "response": response_hooks}, **kwargs)
