"""Tests for src/storage/cache.py -- see docs/component-specs.md."""
from concurrent.futures import ThreadPoolExecutor

from src.storage.cache import ResponseCache


def test_cache_hit_skips_network_call_and_budget_decrement(tmp_path):
    cache = ResponseCache(tmp_path / "responses.sqlite3")
    cache.put("https://example.com/", "2026-09", "<html>cached</html>")

    requests_made = []
    budget_decrements = []

    def fetch_with_cache(url, date_bucket):
        hit = cache.get(url, date_bucket)
        if hit is not None:
            return hit
        requests_made.append(url)
        budget_decrements.append(url)
        html = "<html>live</html>"
        cache.put(url, date_bucket, html)
        return html

    result = fetch_with_cache("https://example.com/", "2026-09")

    assert result == "<html>cached</html>"
    assert requests_made == []
    assert budget_decrements == []


def test_cache_miss_performs_call_and_stores_result(tmp_path):
    cache = ResponseCache(tmp_path / "responses.sqlite3")

    assert cache.get("https://example.com/", "2026-09") is None

    cache.put("https://example.com/", "2026-09", "<html>fresh</html>")

    assert cache.get("https://example.com/", "2026-09") == "<html>fresh</html>"


def test_date_bucket_expiry_works(tmp_path):
    cache = ResponseCache(tmp_path / "responses.sqlite3")
    cache.put("https://example.com/", "2026-09", "<html>september</html>")

    # Same URL, different (later) date bucket -- must be a miss so refresh can
    # detect real changes rather than caching forever (docs/component-specs.md).
    assert cache.get("https://example.com/", "2026-10") is None
    assert cache.get("https://example.com/", "2026-09") == "<html>september</html>"


def test_cache_persists_across_instances(tmp_path):
    db_path = tmp_path / "responses.sqlite3"
    ResponseCache(db_path).put("https://example.com/", "2026-09", "<html>durable</html>")

    reopened = ResponseCache(db_path)
    assert reopened.get("https://example.com/", "2026-09") == "<html>durable</html>"


def test_shared_across_real_threads_without_crashing(tmp_path):
    """Real-world regression: src/orchestrator/runner.py dispatches each
    company via asyncio.to_thread, sharing ONE ResponseCache across many real
    OS threads. Raw sqlite3.Connection objects reject use from a thread other
    than the one that created them -- this must not happen here."""
    cache = ResponseCache(tmp_path / "responses.sqlite3")

    def worker(i: int) -> str | None:
        url = f"https://example.com/{i % 5}"
        cache.put(url, "2026-09", f"<html>{i}</html>")
        return cache.get(url, "2026-09")

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(worker, range(50)))

    assert all(r is not None for r in results)
    assert cache.get("https://example.com/0", "2026-09") is not None
