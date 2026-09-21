"""
Tests for src/pipeline/net.py -- the one place every outbound HTTP client is
built, so that (a) every real request on the wire is counted, including
redirect hops and retries, and (b) no request is ever sent to a private,
loopback or link-local address.

Builderr's evaluation contract counts "2,000 total outbound requests,
including redirects and retries" and requires the outbound URL policy to be
safe. No live network: transports are httpx.MockTransport and DNS is faked.
"""
import httpx
import pytest

from src.pipeline.net import (
    OutboundPolicy,
    UnsafeOutboundUrl,
    new_client,
    wire_request_count,
)


def _resolver(mapping):
    """Fake DNS: host -> list of IP strings; unknown hosts don't resolve."""
    import socket

    def resolve(host, port, *args, **kwargs):
        if host not in mapping:
            raise socket.gaierror(f"no such host {host}")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in mapping[host]]

    return resolve


def _policy(mapping=None):
    return OutboundPolicy(resolver=_resolver(mapping or {}))


# ---- wire counting -------------------------------------------------------


def test_every_request_is_counted_including_each_redirect_hop():
    """A page fetch that redirects http -> https -> www is THREE requests to
    Builderr's counter, not one."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"Location": "https://example.com/b"})
        if request.url.path == "/b":
            return httpx.Response(302, headers={"Location": "https://example.com/c"})
        return httpx.Response(200, text="ok")

    client = new_client(transport=httpx.MockTransport(handler), policy=_policy({"example.com": ["93.184.216.34"]}))
    before = wire_request_count()

    response = client.get("https://example.com/a", follow_redirects=True)

    assert response.status_code == 200
    assert wire_request_count() - before == 3


def test_a_retry_is_a_second_counted_request():
    client = new_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
        policy=_policy({"example.com": ["93.184.216.34"]}),
    )
    before = wire_request_count()

    client.get("https://example.com/")
    client.get("https://example.com/")  # the caller's retry

    assert wire_request_count() - before == 2


# ---- outbound URL policy -------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://127.0.0.1:8080/admin",
        "http://localhost/",
        "http://localhost:9200/_cat",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.9/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
        "http://[fd00::1]/",
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://100.64.0.1/",  # carrier-grade NAT
        "http://intranet.local/",
        "http://printer.internal/",
        "http://0.0.0.0/",
    ],
)
def test_private_loopback_and_link_local_targets_are_refused(url):
    with pytest.raises(UnsafeOutboundUrl):
        _policy().check(url)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "javascript:alert(1)", "//example.com/"])
def test_only_http_and_https_are_allowed(url):
    with pytest.raises(UnsafeOutboundUrl):
        _policy({"example.com": ["93.184.216.34"]}).check(url)


def test_a_hostname_that_resolves_to_a_private_address_is_refused():
    """The classic SSRF trick: a public-looking name that points inside."""
    policy = _policy({"evil.example": ["10.1.2.3"]})

    with pytest.raises(UnsafeOutboundUrl):
        policy.check("https://evil.example/")


def test_a_hostname_with_any_private_address_among_several_is_refused():
    policy = _policy({"mixed.example": ["93.184.216.34", "127.0.0.1"]})

    with pytest.raises(UnsafeOutboundUrl):
        policy.check("https://mixed.example/")


def test_public_addresses_and_hostnames_are_allowed():
    policy = _policy({"www.example.no": ["93.184.216.34"]})

    policy.check("https://www.example.no/om-oss")
    policy.check("http://93.184.216.34/")


def test_a_hostname_that_does_not_resolve_is_allowed_through():
    """Unresolvable names can't reach an internal service; the request itself
    will simply fail. Refusing them would break every dead/guessed domain."""
    _policy({}).check("https://no-such-company-site.no/")


def test_the_dns_answer_is_cached_per_host():
    calls = []
    import socket

    def resolve(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    policy = OutboundPolicy(resolver=resolve)
    for _ in range(5):
        policy.check("https://example.com/x")

    assert calls == ["example.com"]


def test_the_guard_stops_a_request_before_it_is_sent_and_it_is_not_counted():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url))
        return httpx.Response(200)

    client = new_client(transport=httpx.MockTransport(handler), policy=_policy())
    before = wire_request_count()

    with pytest.raises(UnsafeOutboundUrl):
        client.get("http://169.254.169.254/latest/meta-data/")

    assert sent == []
    assert wire_request_count() == before


def test_a_redirect_into_a_private_address_is_stopped_at_the_redirect():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.host)
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})

    client = new_client(transport=httpx.MockTransport(handler), policy=_policy({"example.com": ["93.184.216.34"]}))

    with pytest.raises(UnsafeOutboundUrl):
        client.get("https://example.com/", follow_redirects=True)

    assert sent == ["example.com"]  # the internal address was never requested


def test_a_refused_url_is_an_ordinary_httpx_error_so_existing_handlers_degrade_it():
    """Every fetch helper already turns httpx.HTTPError into FAILED for that one
    company; the guard must ride that path rather than crash a batch."""
    assert issubclass(UnsafeOutboundUrl, httpx.HTTPError)


# ---- where the requests go -----------------------------------------------


def test_the_breakdown_shows_requests_by_host_and_how_many_were_redirect_hops():
    from src.pipeline.net import wire_request_breakdown

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "old.example.com":
            return httpx.Response(301, headers={"Location": "https://www.example.com/x"})
        return httpx.Response(200, text="ok")

    client = new_client(
        transport=httpx.MockTransport(handler),
        policy=_policy({"old.example.com": ["93.184.216.34"], "www.example.com": ["93.184.216.34"]}),
    )
    before = wire_request_breakdown()

    client.get("https://old.example.com/x", follow_redirects=True)
    client.get("https://www.example.com/y")

    after = wire_request_breakdown()
    assert after["total"] - before["total"] == 3
    assert after["redirect_hops"] - before["redirect_hops"] == 1
    assert after["by_host"].get("old.example.com", 0) - before["by_host"].get("old.example.com", 0) == 1
    assert after["by_host"].get("www.example.com", 0) - before["by_host"].get("www.example.com", 0) == 2


# ---- a redirect loop must not eat the request budget ---------------------


def test_redirect_chains_are_capped_at_five_hops():
    """A real site's robots.txt redirected to itself forever. httpx's default
    of 20 hops, repeated by a retry, cost ~190 requests for ONE company --
    a tenth of a whole batch's limit."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": str(request.url) + "-again"})

    client = new_client(transport=httpx.MockTransport(handler), policy=_policy({"loop.example": ["93.184.216.34"]}))
    before = wire_request_count()

    with pytest.raises(httpx.TooManyRedirects):
        client.get("https://loop.example/robots.txt", follow_redirects=True)

    assert wire_request_count() - before <= 6
