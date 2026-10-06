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
    search_keys: Optional[dict[str, str]] = None,
) -> str:
    """One self-contained, responsive viewer covering every profile in `profiles`.

    `envelopes` lets the caller pass the exact envelopes it submits; when omitted
    they are built from `profiles`. Never fabricates content."""
    run_id = generated_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    envs = envelopes if envelopes is not None else [to_envelope(p, run_id=run_id) for p in profiles]
    data = _viewer_data(envs, generated_at, run_id, search_keys)
    fallback = _fallback_html(envs)
    # The data goes in last so nothing inside it can be mistaken for a placeholder.
    parts = {"{{NOSCRIPT}}": fallback, "{{ATLAS_DATA}}": _json_for_script(data)}
    # One pass, so text inside a company name or the data can never be taken for a placeholder.
    return re.sub(r"\{\{(?:NOSCRIPT|ATLAS_DATA)\}\}", lambda m: parts[m.group(0)], _TEMPLATE.read_text(encoding="utf-8"))


def _viewer_data(
    envs: list[dict], generated_at: datetime, run_id: str, search_keys: Optional[dict[str, str]],
    title: Optional[str] = None, links: Optional[list[dict]] = None, downloads: Optional[list[dict]] = None,
) -> dict:
    as_of = max((e["summary"]["as_of"] for e in envs if e.get("summary")), default=generated_at.date().isoformat())
    return {
        "generated_at": generated_at.isoformat(),
        "run_id": run_id,
        "as_of": as_of,
        "search_keys": search_keys or {},
        # Shown in the header: what this run is, and where the other runs live.
        "title": title,
        "links": links or [],
        # The raw files this page was built from, served beside it, so a reviewer can
        # download exactly what the agent emitted.
        "downloads": downloads or [],
        "envelopes": [_slim(e) for e in envs],
    }


def _fallback_html(envs: list[dict], limit: Optional[int] = None) -> str:
    shown = envs if limit is None else envs[:limit]
    more = "" if limit is None or len(envs) <= limit else f" Showing the first {limit} of {len(envs)}."
    return (
        "<h1>Signalpost Atlas</h1><p>This page needs JavaScript for search, comparison and evidence panels. "
        f"Plain summaries follow ({len(shown)} companies).{more}</p>" + "".join(_fallback_section(e) for e in shown)
    )


def write_site(
    out_dir: Path,
    envelopes: list[dict],
    generated_at: datetime,
    search_keys: Optional[dict[str, str]] = None,
    run_id: Optional[str] = None,
    fallback_limit: int = 25,
    title: Optional[str] = None,
    links: Optional[list[dict]] = None,
    downloads: Optional[list[dict]] = None,
) -> dict[str, int]:
    """The hosted form of the viewer: `index.html` (the page) and `data.json` (the
    envelopes it shows), written side by side. A static host such as GitHub Pages
    compresses data.json on the way out, and the browser keeps it, so the page opens at
    once and the data follows; the embedded single-file form (render_html_report) stays
    for offline use. The plain-HTML fallback for readers without JavaScript covers the
    first `fallback_limit` companies, to keep the page itself small."""
    out_dir.mkdir(parents=True, exist_ok=True)
    run_id = run_id or generated_at.strftime("%Y-%m-%dT%H-%M-%SZ")
    data = _viewer_data(envelopes, generated_at, run_id, search_keys, title, links, downloads)
    template = _TEMPLATE.read_text(encoding="utf-8")
    parts = {"{{NOSCRIPT}}": _fallback_html(envelopes, fallback_limit), "{{ATLAS_DATA}}": ""}
    html = re.sub(r"\{\{(?:NOSCRIPT|ATLAS_DATA)\}\}", lambda m: parts[m.group(0)], template)
    (out_dir / "index.html").write_text(html, encoding="utf-8")
    (out_dir / "data.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return {"companies": len(envelopes), "index_bytes": len(html.encode("utf-8")), "data_bytes": (out_dir / "data.json").stat().st_size}
