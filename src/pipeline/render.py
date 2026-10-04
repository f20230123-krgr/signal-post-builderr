"""
Optional headless-browser rendering, for pages whose content only exists after scripts run.

Builderr's agent playbook: "Crawl static HTML first. Escalate to a browser only when a
deterministic completeness check fails." Many corporate sites draw their navigation,
footer icons, news lists and vacancy listings with JavaScript; the raw HTML a plain HTTP
fetch sees is an empty shell, so careers links, social profiles and headlines on those
sites are invisible to a static crawler. This module renders such a page once and hands
back the finished DOM, which the normal extractors then read exactly as they read static
HTML.

Guarantees, because a browser is the one component that can quietly spend a whole run:

  * It is optional. If Playwright or its Chromium build is missing, `start()` returns
    False and every caller falls back to static HTML. Nothing else changes.
  * Every request the browser makes is counted in the same request counter as HTTP
    requests (src/pipeline/net.py) -- Builderr counts all of them -- and one page may make
    at most MAX_REQUESTS_PER_PAGE. Images, media, fonts and stylesheets are never fetched:
    they cannot change the text.
  * Every request passes the outbound URL policy (public hosts, http/https only).
  * One worker thread owns the browser (Playwright's sync API is not thread-safe); callers
    queue their pages and wait, so concurrency elsewhere is unaffected.
  * A per-page time limit, and an overall cap on renders, so a slow site cannot stall a batch.
"""
from __future__ import annotations

import logging
import queue
import threading
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

from src.pipeline import net

logger = logging.getLogger(__name__)

USER_AGENT = net.USER_AGENT
MAX_REQUESTS_PER_PAGE = 30
PAGE_TIMEOUT_SECONDS = 25
SETTLE_MILLISECONDS = 900
# Resource types that cannot change the text of a page.
BLOCKED_RESOURCE_TYPES = {"image", "media", "font", "stylesheet", "other", "manifest", "texttrack"}


@dataclass
class RenderResult:
    html: str
    final_url: str
    requests_made: int


class BrowserRenderer:
    def __init__(self, max_renders: int = 10_000, page_timeout: float = PAGE_TIMEOUT_SECONDS):
        self._max_renders = max_renders
        self._page_timeout = page_timeout
        self._jobs: "queue.Queue[Optional[tuple[str, queue.Queue]]]" = queue.Queue()
        self._ready = threading.Event()
        self._available = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.renders = 0
        self.requests = 0
        self.failures = 0

    # --- lifecycle -------------------------------------------------------------------------

    def start(self) -> bool:
        """Launch the browser worker. True when rendering is available."""
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="browser-renderer", daemon=True)
                self._thread.start()
        self._ready.wait(timeout=60)
        return self._available

    @property
    def available(self) -> bool:
        return self._available

    def close(self) -> None:
        if self._thread is not None:
            self._jobs.put(None)

    # --- use -------------------------------------------------------------------------------

    def render(self, url: str) -> Optional[RenderResult]:
        """The page at `url` after its scripts have run, or None (unavailable, over the cap,
        timed out, or the URL refused by the outbound policy)."""
        if not self._available:
            return None
        with self._lock:
            if self.renders >= self._max_renders:
                return None
            self.renders += 1
        try:
            net._default_policy.check(url)
        except net.UnsafeOutboundUrl:
            return None
        reply: "queue.Queue" = queue.Queue(maxsize=1)
        self._jobs.put((url, reply))
        try:
            outcome = reply.get(timeout=self._page_timeout + 30)
        except queue.Empty:
            self.failures += 1
            return None
        if isinstance(outcome, Exception) or outcome is None:
            self.failures += 1
            return None
        self.requests += outcome.requests_made
        return outcome

    def report(self) -> dict:
        return {"available": self._available, "renders": self.renders, "requests": self.requests, "failures": self.failures}

    # --- the worker ------------------------------------------------------------------------

    def _worker(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.info("browser rendering: playwright is not installed; static HTML only")
            self._ready.set()
            return
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                self._available = True
                self._ready.set()
                while True:
                    job = self._jobs.get()
                    if job is None:
                        break
                    url, reply = job
                    try:
                        reply.put(self._render_one(browser, url))
                    except Exception as exc:  # a broken page must not kill the worker
                        reply.put(exc)
                browser.close()
        except Exception as exc:  # no Chromium build, sandbox refusal, ...
            logger.info("browser rendering unavailable (%s); static HTML only", exc)
            self._ready.set()

    def _render_one(self, browser, url: str) -> Optional[RenderResult]:
        context = browser.new_context(user_agent=USER_AGENT, java_script_enabled=True, ignore_https_errors=False)
        counted = {"n": 0}

        def handle(route) -> None:
            request = route.request
            if request.resource_type in BLOCKED_RESOURCE_TYPES:
                route.abort()
                return
            try:
                net._default_policy.check(request.url)
            except net.UnsafeOutboundUrl:
                route.abort()
                return
            if counted["n"] >= MAX_REQUESTS_PER_PAGE:
                route.abort()
                return
            counted["n"] += 1
            net.add_external_request(urlsplit(request.url).hostname or "")
            route.continue_()

        try:
            page = context.new_page()
            page.route("**/*", handle)
            page.set_default_timeout(self._page_timeout * 1000)
            page.goto(url, wait_until="domcontentloaded", timeout=self._page_timeout * 1000)
            try:
                page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass  # chatty sites never go idle; what has loaded is what we read
            page.wait_for_timeout(SETTLE_MILLISECONDS)
            return RenderResult(page.content(), page.url, counted["n"])
        finally:
            context.close()


_shared: Optional[BrowserRenderer] = None
_shared_lock = threading.Lock()


def shared_renderer() -> BrowserRenderer:
    """The process-wide renderer (the browser is started once, on first use)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = BrowserRenderer()
        return _shared


_active: Optional[BrowserRenderer] = None


def set_active_renderer(renderer: Optional[BrowserRenderer]) -> None:
    """Choose the renderer the pipeline uses (None = static HTML only). Set once by the CLI."""
    global _active
    _active = renderer


def active_renderer() -> Optional[BrowserRenderer]:
    return _active if _active is not None and _active.available else None
