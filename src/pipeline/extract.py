"""
Stage 3: Extract.

Contract: docs/component-specs.md -> "src/pipeline/extract.py"

Responsibility: turn raw HTML into structured, unverified facts. Prefer
structured data (schema.org/JSON-LD via extruct) over full-text extraction
(Trafilatura) -- cheaper and more reliable when available.

Activity scanning (hiring_signal / dated_activity RawFacts) runs independently
of the structured-vs-text branch above: a page can have JSON-LD organization
data (identity) while its careers/news content is still plain prose (see
fixtures/pages/923609016/official_site.html), so activity signals are always
scanned for regardless of which path produced the identity facts.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal, Optional

import extruct
import trafilatura

from src.pipeline.crawl import FetchedPage

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_WHITESPACE_RE = re.compile(r"\s+")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_HIRING_KEYWORDS_RE = re.compile(r"\b(hiring|career|careers|job opening|recruiting)\b", re.IGNORECASE)


@dataclass
class RawFact:
    field_name: str
    value: str
    source_url: str
    extraction_method: Literal["structured", "text"]
    extracted_at: datetime
    # SHA-256 of the exact page content this fact was extracted from --
    # signalpost-sources.md: "every claim records ... content hash".
    content_hash: Optional[str] = None


_BLOCK_BOUNDARY = "␞"  # sentinel, not real markup -- see docstring below


def _visible_lines(html: str) -> list[str]:
    """Visible text split into one "line" per paragraph/list-item/heading
    block. Uses a sentinel instead of the source's own newlines as the split
    point, so a sentence hand-wrapped across multiple physical lines in the
    HTML source (as real prose is) stays intact as a single scan-able line."""
    stripped = _SCRIPT_STYLE_RE.sub(" ", html)
    stripped = re.sub(r"</(li|p|h[1-6])\s*>", _BLOCK_BOUNDARY, stripped, flags=re.IGNORECASE)
    text = _TAG_RE.sub(" ", stripped)
    lines = [_WHITESPACE_RE.sub(" ", chunk).strip() for chunk in text.split(_BLOCK_BOUNDARY)]
    return [line for line in lines if line]


def _structured_facts(html: str, source_url: str, extracted_at: datetime) -> list[RawFact]:
    try:
        data = extruct.extract(html, syntaxes=["json-ld"], errors="ignore")
    except Exception:
        return []

    facts: list[RawFact] = []
    for obj in data.get("json-ld", []):
        if not isinstance(obj, dict):
            continue
        if obj.get("name"):
            facts.append(RawFact("organization_name", obj["name"], source_url, "structured", extracted_at))
        if obj.get("url"):
            facts.append(RawFact("official_site", obj["url"], source_url, "structured", extracted_at))
        addr = obj.get("address")
        if isinstance(addr, dict):
            addr_str = ", ".join(
                p for p in (addr.get("streetAddress"), addr.get("postalCode"), addr.get("addressLocality")) if p
            )
            if addr_str:
                facts.append(RawFact("registered_address", addr_str, source_url, "structured", extracted_at))
        employees = obj.get("employee")
        if isinstance(employees, list):
            for emp in employees:
                if isinstance(emp, dict) and emp.get("name"):
                    title = emp.get("jobTitle")
                    value = f"{emp['name']} ({title})" if title else emp["name"]
                    facts.append(RawFact("leader", value, source_url, "structured", extracted_at))
        same_as = obj.get("sameAs")
        if isinstance(same_as, list):
            for link in same_as:
                if isinstance(link, str) and link:
                    facts.append(RawFact("company_profile", link, source_url, "structured", extracted_at))
    return facts


def _text_fallback_fact(html: str, source_url: str, extracted_at: datetime) -> list[RawFact]:
    try:
        text = trafilatura.extract(html)
    except Exception:
        text = None
    if not text:
        return []
    return [RawFact("page_text", text, source_url, "text", extracted_at)]


def _activity_facts(html: str, source_url: str, extracted_at: datetime) -> list[RawFact]:
    facts: list[RawFact] = []
    for line in _visible_lines(html):
        if _HIRING_KEYWORDS_RE.search(line):
            facts.append(RawFact("hiring_signal", line, source_url, "text", extracted_at))
        elif _DATE_RE.search(line):
            facts.append(RawFact("dated_activity", line, source_url, "text", extracted_at))
    return facts


def extract(
    page: FetchedPage,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> list[RawFact]:
    """
    Extract RawFacts from `page`, trying extruct (structured) before
    Trafilatura (text) fallback.

    Must not:
      - Invent a field that wasn't actually present on the page.
    """
    extracted_at = now()
    facts = _structured_facts(page.raw_html, page.url, extracted_at)

    if not facts:
        facts = _text_fallback_fact(page.raw_html, page.url, extracted_at)

    facts += _activity_facts(page.raw_html, page.url, extracted_at)

    content_hash = hashlib.sha256(page.raw_html.encode("utf-8")).hexdigest()
    for fact in facts:
        fact.content_hash = content_hash
    return facts
