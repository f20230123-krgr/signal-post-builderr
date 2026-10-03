"""
Signalpost Atlas: the results viewer -- "UX & interaction" (the brief asks that
"a user should be able to find, compare and verify the information on desktop
and mobile").

One self-contained HTML file per batch (src/viewer.html is the template; no
server, no build step, no network). The page embeds the very envelopes that
envelopes.jsonl submits, so what a reviewer sees is exactly what was submitted:
the dated summary, every claim, and the evidence record behind each of them.

This module never invents content. It renders existing envelopes (built from
validated CompanyProfiles) and a plain-HTML fallback for readers without
JavaScript, so a legible report can't drift from what was actually verified.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.models.profile import CompanyProfile
from src.pipeline.envelope import to_envelope

_TEMPLATE = Path(__file__).with_name("viewer.html")
# Plain-HTML fallback shows this many claims per company before pointing at the
# interactive view; the viewer itself shows everything.
_FALLBACK_MAX_SOURCES = 6


def _esc(value: object) -> str:
    return html_lib.escape(str(value))


def _slim(envelope: dict) -> dict:
    """The part of an envelope the viewer needs, with null values dropped to keep
    the embedded payload small. Operations/run internals are not shown."""
    def strip(obj):
        if isinstance(obj, dict):
            return {k: strip(v) for k, v in obj.items() if v is not None}
        if isinstance(obj, list):
            return [strip(v) for v in obj]
        return obj

    keep = {k: envelope[k] for k in ("organisation_number", "claims", "evidence", "summary", "changes", "errors") if k in envelope}
    slim = strip(keep)
    # The summary's `text` is its sentences joined; the viewer rebuilds it, so
    # it isn't embedded twice.
    slim.get("summary", {}).pop("text", None)
    return slim


def _json_for_script(data: dict) -> str:
    """JSON safe to place inside a <script> element: '<' can never open a tag or
    close the script, and the two JS line separators are escaped."""
    return (
        json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def _fallback_section(envelope: dict) -> str:
    """Plain, JavaScript-free rendering of one company: dated summary, then the
    sources behind it as real links."""
    claims = envelope.get("claims", [])
    name = next((c["value"] for c in claims if c["field"] == "legal_name" and c.get("value")), None)
    title = _esc(name) if name else f"Org. {_esc(envelope['organisation_number'])}"
    summary = envelope.get("summary", {})
    seen: list[str] = []
    for ev in envelope.get("evidence", []):
        url = ev.get("source_url", "")
        if url.startswith(("http://", "https://")) and url not in seen:
            seen.append(url)
    links = "".join(f'<li><a href="{_esc(u)}">{_esc(u)}</a></li>' for u in seen[:_FALLBACK_MAX_SOURCES])
    unavailable = [c["field"] for c in claims if c.get("availability") != "available"]
    gaps = f"<p>Not available: {_esc(', '.join(unavailable))}.</p>" if unavailable else ""
    return (
        f"<section><h2>{title} <small>{_esc(envelope['organisation_number'])}</small></h2>"
        f"<p>{_esc(summary.get('text', ''))}</p>{gaps}"
        + (f"<ul>{links}</ul>" if links else "")
        + "</section>"
    )


def render_html_report(
    profiles: list[CompanyProfile],
    generated_at: datetime,
    envelopes: Optional[list[dict]] = None,
) -> str:
    """One self-contained, responsive viewer covering every profile in `profiles`.

    `envelopes` lets the caller pass the exact envelopes it submits; when omitted
    they are built from `profiles`. Never fabricates content."""
    run_id = generated_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    envs = envelopes if envelopes is not None else [to_envelope(p, run_id=run_id) for p in profiles]
    as_of = max((e["summary"]["as_of"] for e in envs if e.get("summary")), default=generated_at.date().isoformat())
    data = {
        "generated_at": generated_at.isoformat(),
        "run_id": run_id,
        "as_of": as_of,
        "envelopes": [_slim(e) for e in envs],
    }
    fallback = (
        "<h1>Signalpost Atlas</h1><p>This page needs JavaScript for search, comparison and evidence panels. "
        f"Plain summaries follow ({len(envs)} companies).</p>" + "".join(_fallback_section(e) for e in envs)
    )
    # The data goes in last so nothing inside it can be mistaken for a placeholder.
    parts = {"{{NOSCRIPT}}": fallback, "{{ATLAS_DATA}}": _json_for_script(data)}
    # One pass, so text inside a company name or the data can never be taken for a placeholder.
    return re.sub(r"\{\{(?:NOSCRIPT|ATLAS_DATA)\}\}", lambda m: parts[m.group(0)], _TEMPLATE.read_text(encoding="utf-8"))
