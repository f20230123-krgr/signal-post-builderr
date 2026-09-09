"""
Durable, disk-backed HTTP response cache across runs.

Contract: docs/component-specs.md -> "src/storage/cache.py"

Must: cache hits never count against the request budget (brief: "cache hits
free"). Cache key includes URL + a coarse date bucket so refresh can still
detect real changes rather than caching forever.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Optional, Union


class ResponseCache:
    def __init__(self, db_path: Union[str, Path] = ".cache/responses.sqlite3"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # One shared connection guarded by a lock, not one connection per
        # caller. Real-world regression: src/orchestrator/runner.py shares
        # ONE ResponseCache across many real OS threads via
        # asyncio.to_thread, and a raw sqlite3.Connection rejects use from
        # any thread but the one that created it. check_same_thread=False
        # lifts that restriction; the lock is what makes doing so actually
        # safe (SQLite's C API is not safe for unsynchronized concurrent use
        # of a single connection).
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS responses (
                    url TEXT NOT NULL,
                    date_bucket TEXT NOT NULL,
                    raw_html TEXT NOT NULL,
                    PRIMARY KEY (url, date_bucket)
                )
                """
            )
            self._conn.commit()

    def get(self, url: str, date_bucket: str) -> Optional[str]:
        """Return cached raw HTML for (url, date_bucket), or None on miss."""
        with self._lock:
            row = self._conn.execute(
                "SELECT raw_html FROM responses WHERE url = ? AND date_bucket = ?",
                (url, date_bucket),
            ).fetchone()
            return row[0] if row else None

    def put(self, url: str, date_bucket: str, raw_html: str) -> None:
        """Store raw HTML for (url, date_bucket)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO responses (url, date_bucket, raw_html) VALUES (?, ?, ?)
                ON CONFLICT (url, date_bucket) DO UPDATE SET raw_html = excluded.raw_html
                """,
                (url, date_bucket, raw_html),
            )
            self._conn.commit()
