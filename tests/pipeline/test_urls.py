"""Tests for src/pipeline/urls.py -- URL normalization/dedup helper.

Evaluator feedback (2026-09): "Normalize and deduplicate URLs before
matching: remove tracking parameters, resolve redirects, normalize
hostnames and canonical profile URLs."
"""
from src.pipeline.urls import normalize_url, same_normalized_url


def test_strips_common_tracking_params():
    url = "https://example.com/about?utm_source=x&utm_campaign=y&gclid=z&fbclid=w"
    assert normalize_url(url) == "https://example.com/about"


def test_keeps_non_tracking_query_params():
    url = "https://example.com/jobs?dept=engineering&utm_source=x"
    assert normalize_url(url) == "https://example.com/jobs?dept=engineering"


def test_strips_fragment():
    assert normalize_url("https://example.com/about#team") == "https://example.com/about"


def test_lowercases_host_but_not_path():
    assert normalize_url("https://Example.COM/About") == "https://example.com/About"


def test_strips_www_prefix():
    assert normalize_url("https://www.example.com/") == "https://example.com/"


def test_removes_trailing_slash_except_root():
    assert normalize_url("https://example.com/about/") == "https://example.com/about"
    assert normalize_url("https://example.com/") == "https://example.com/"


def test_removes_default_ports():
    assert normalize_url("https://example.com:443/about") == "https://example.com/about"
    assert normalize_url("http://example.com:80/about") == "http://example.com/about"


def test_same_normalized_url_matches_equivalent_variants():
    a = "https://www.example.com/About/?utm_source=x#section"
    b = "https://example.com/About"
    assert same_normalized_url(a, b) is True


def test_same_normalized_url_rejects_different_paths():
    assert same_normalized_url("https://example.com/about", "https://example.com/careers") is False
