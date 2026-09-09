"""
URL normalization/dedup helper.

Evaluator feedback (2026-09): "Normalize and deduplicate URLs before
matching: remove tracking parameters, resolve redirects, normalize
hostnames and canonical profile URLs, and bind every result back to the
correct organization number/domain."

Redirect resolution itself already happens in crawl.py (httpx.Client is
called with follow_redirects=True) -- this module handles the other half:
making two URLs that point at "the same page" compare equal so candidate
lists can be deduplicated and evidence isn't split across cosmetic variants.
"""
from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query params that vary per-visit/per-campaign and never change page
# identity -- stripped before comparing or fetching. Prefix-matched so
# "utm_source", "utm_campaign", etc. are all caught by "utm_".
_TRACKING_PREFIXES = ("utm_",)
_TRACKING_EXACT = {"gclid", "fbclid", "msclkid", "mc_cid", "mc_eid", "ref", "ref_src"}

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _is_tracking_param(key: str) -> bool:
    lowered = key.lower()
    return lowered in _TRACKING_EXACT or any(lowered.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url(url: str) -> str:
    """Canonicalize `url`: lowercase scheme/host, drop a leading "www.",
    drop the default port for the scheme, strip tracking query params and
    any fragment, and drop a trailing "/" (except on the bare root path)."""
    parts = urlsplit(url)

    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    port = parts.port
    netloc = host
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    if path == "":
        path = "/"

    kept_params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking_param(k)]
    query = urlencode(kept_params)

    return urlunsplit((scheme, netloc, path, query, ""))


def same_normalized_url(a: str, b: str) -> bool:
    """True if `a` and `b` normalize to the same URL."""
    return normalize_url(a) == normalize_url(b)
