"""
Verbatim evidence spans from a page's own source text.

A claim's span must be text a checker finds in the source exactly as written (official C300
feedback: the strictest miss type was "a claim was present but lacked a clear supporting
receipt"). Text joined across several HTML elements ("Organisasjonsnummer 999 296 432"
assembled from a label and a value in two tags) never appears in the page source as one
string, so a span is always taken from ONE text node: the characters between two tags,
exactly as the page writes them (entities included).
"""
from __future__ import annotations

import html as html_lib
import re
from typing import Callable, Optional

_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TEXT_NODE_RE = re.compile(r">([^<>]+)<")
MAX_SPAN = 200


def text_nodes(page_html: str) -> list[str]:
    """Every non-blank text node of the page, as written in its source (scripts and styles
    excluded)."""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", page_html)
    return [m.group(1) for m in _TEXT_NODE_RE.finditer(cleaned) if m.group(1).strip()]


def plain(raw: str) -> str:
    """A text node as a reader sees it: entities decoded, whitespace collapsed."""
    return re.sub(r"\s+", " ", html_lib.unescape(raw)).strip()


def node_span(page_html: str, test: Callable[[str], bool], anchor: Optional[re.Pattern] = None) -> Optional[str]:
    """The first text node whose plain text satisfies `test`, quoted verbatim. A node longer
    than MAX_SPAN is cut to a window around `anchor` (a pattern matched in the raw node) so
    the span stays short and still verbatim."""
    for raw in text_nodes(page_html):
        if not test(plain(raw)):
            continue
        quote = raw.strip()
        if len(quote) <= MAX_SPAN:
            return quote
        match = anchor.search(quote) if anchor else None
        if match:
            start = max(0, match.start() - MAX_SPAN // 2)
            return quote[start:start + MAX_SPAN]
        return quote[:MAX_SPAN]
    return None


def span_for_text(page_html: str, text: str) -> Optional[str]:
    """A verbatim span for `text` assembled from the page (a card's title read across several
    elements): the node that IS the text, else the longest node that is part of it."""
    target = plain(text).lower()
    best = None
    for raw in text_nodes(page_html):
        node = plain(raw).lower()
        if not node or len(node) < 3:
            continue
        if node == target:
            return raw.strip()
        if node in target and (best is None or len(node) > len(plain(best))):
            best = raw.strip()
    return best
