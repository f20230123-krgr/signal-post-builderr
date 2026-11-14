"""
Export layer: CompanyProfile -> the submission envelope shape.

Contract: docs/component-specs.md -> "src/pipeline/envelope.py"

Added after reviewing the real challenge site. `src/models/profile.py`'s
nested `CompanyProfile` is our internal, Pydantic-validated model -- good for
enforcing invariants at assemble-time. But the Signalpost reference agent's
`OUTPUT_CONTRACT.md` defines the actual submission shape as a flat envelope:
a `claims` list (each referencing evidence by id) plus a separate,
deduplicated `evidence` list, alongside `run`/`changes`/`errors`/`operations`.
This module converts one to the other so the SUBMITTED JSONL matches what the
evaluator almost certainly parses, without throwing away the internal model's
correctness guarantees.

Also embeds src/synthesis.py's templated business-question answers under
"answers" -- real gap found chasing a "decision-useful synthesis" score of
0/10: those answers used to be consumed only by src/reporting.py's separate
report.html, which never leaves the machine that generated it. envelopes.jsonl
is the artifact actually submitted, so the answers must be embedded here too.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from src.models.profile import Claim, CompanyProfile, EvidenceState
from src.pipeline.registry_extras import ACCOUNT_METRIC_FIELDS
from src.synthesis import PART_LABELS, build_summary

_ERROR_STATES = {EvidenceState.FAILED, EvidenceState.BLOCKED}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _claim_entries(profile: CompanyProfile) -> list[tuple[str, Claim]]:
    """(field_name, Claim) pairs in a fixed order, matching the field names
    used throughout the pipeline (RawFact/ConfirmedFact field_name tags),
    with "official_site" renamed to "official_website" to match
    OUTPUT_CONTRACT.md's own example field name."""
    entries: list[tuple[str, Claim]] = [
        ("legal_name", profile.legal_identity.legal_name),
        ("public_brand", profile.legal_identity.public_brand),
        ("annual_accounts_latest", profile.annual_accounts.latest),
        ("official_website", profile.online_presence.official_site),
    ]
    # Free registry identity facts -- omitted entirely (not emitted as an
    # empty/unavailable claim) when this company isn't covered by the
    # universe manifest, so a missing one is never mistaken for a gap we
    # looked for and failed to fill.
    entries += [
        (field_name, claim)
        for field_name, claim in (
            ("industry", profile.legal_identity.industry),
            ("employee_count", profile.legal_identity.employee_count),
            ("legal_form", profile.legal_identity.legal_form),
            ("operating_status", profile.legal_identity.operating_status),
            ("founded_date", profile.legal_identity.founded_date),
            ("business_description", profile.legal_identity.business_description),
            ("site_description", profile.legal_identity.site_description),
        )
        if claim is not None
    ]
    entries += [("annual_accounts_history", c) for c in profile.annual_accounts.history]
    # Discrete figures per filed year, newest first, in a fixed field order.
    for name in ACCOUNT_METRIC_FIELDS:
        entries += [(name, c) for c in profile.annual_accounts.metrics.get(name, [])]
    entries += [("leader", c) for c in profile.leadership.leaders]
    entries += [("workplace", c) for c in profile.leadership.workplaces]
    entries += [("company_profile", c) for c in profile.online_presence.company_profiles]
    entries += [("hiring_signal", c) for c in profile.activity.hiring_signals]
    entries += [("dated_activity", c) for c in profile.activity.dated_activity]
    entries += _checked_and_not_found(profile)
    return [(name, _with_reason(name, claim, profile)) for name, claim in entries]


# --- what we looked for and did not find, said in the claims themselves -----------------
#
# The brief asks the summary to explain "the company, changes and unknowns", and Builderr's
# starter kit records a state for every area it checks. A family we searched and found empty
# used to be simply absent from the claims (named only in summary.unknowns), so the standard
# fields never said it had been looked for. Each now gets one not_available claim with a
# one-line reason (official feedback, Nov: "leave a field blank with a one-line reason").

def _site_state(profile: CompanyProfile) -> str:
    site = profile.online_presence.official_site
    if site.state == EvidenceState.AVAILABLE and site.value:
        return "confirmed"
    return "ambiguous" if site.state == EvidenceState.AMBIGUOUS else "none"


def _checked_and_not_found(profile: CompanyProfile) -> list[tuple[str, Claim]]:
    missing = []
    if not profile.online_presence.company_profiles:
        missing.append("company_profile")
    if not profile.activity.hiring_signals:
        missing.append("hiring_signal")
    # The registry record was read (its legal form is there) and carries no head count.
    if profile.legal_identity.employee_count is None and profile.legal_identity.legal_form is not None:
        missing.append("employee_count")
    return [(name, Claim(state=EvidenceState.NOT_AVAILABLE)) for name in missing]


_NO_SITE = {
    "none": "no website is registered with Brønnøysund or was found under the company's name",
    "ambiguous": "the registered website could not be confirmed as the company's own",
}


def _default_reason(field_name: str, profile: CompanyProfile) -> Optional[str]:
    site = _site_state(profile)
    if field_name == "company_profile":
        return "the company's website links no social profile of its own" if site == "confirmed" else (
            _NO_SITE[site] + ", so there are no company-owned profiles to read"
        )
    if field_name == "hiring_signal":
        return "no single job ad was found on the company's website" if site == "confirmed" else (
            _NO_SITE[site] + ", so there is no careers page to read"
        )
    if field_name == "employee_count":
        return "Brønnøysund's register gives no employee count for this company"
    if field_name == "official_website" and site == "none":
        return _NO_SITE["none"]
    if field_name == "public_brand":
        return "the company's website states no name other than the registered one" if site == "confirmed" else (
            "no confirmed website to state a trading name"
        )
    if field_name == "annual_accounts_latest" and profile.annual_accounts.latest.state == EvidenceState.NOT_AVAILABLE:
        return "no annual accounts are filed in Brønnøysund's accounts register"
    return None


def _with_reason(field_name: str, claim: Claim, profile: CompanyProfile) -> Claim:
    """`claim` with a one-line reason when it is blank and has none of its own."""
    if claim.state == EvidenceState.AVAILABLE or claim.reason:
        return claim
    reason = _default_reason(field_name, profile)
    return claim.model_copy(update={"reason": reason}) if reason else claim


# --- the summary, with its sources visible ------------------------------------------------
#
# The brief scores synthesis on whether the profile "explains what the company does, what
# changed and what remains unknown, with sources for its conclusions". The summary's text is
# written in labelled parts that follow those words, each sentence followed by the numbers of
# the sources it rests on ("[1][3]"), and the numbered sources are listed in plain words.

def source_label(url: str, source_class: Optional[str], linked_from: Optional[str] = None) -> str:
    """A source named in plain words: "Brønnøysund Register Centre: entity record",
    "Register of Company Accounts: FY2025 accounts", "Company website (example.no)",
    "NAV job board"."""
    import re as _re

    u = url or ""
    if "regnskapsregisteret" in u:
        year = _re.search(r"(?:år|%C3%A5r)=(\d{4})", u)
        return f"Register of Company Accounts: FY{year.group(1)} accounts" if year else "Register of Company Accounts (Brønnøysund)"
    if "data.brreg.no" in u:
        if "/roller" in u:
            return "Brønnøysund Register Centre: roles"
        if "underenheter" in u:
            return "Brønnøysund Register Centre: sub-units"
        if "oppdateringer" in u:
            return "Brønnøysund Register Centre: update log"
        return "Brønnøysund Register Centre: entity record"
    if u.startswith("signalpost-company-universe"):
        return "Builderr company list (Brønnøysund 2025 snapshot)"
    if "arbeidsplassen.nav.no" in u:
        return "NAV job board (arbeidsplassen.nav.no)"
    host = urlsplit(u).netloc.lower().removeprefix("www.") if "://" in u else u
    if linked_from:
        return f"Page linked from the company's website ({host})"
    # Everything else read from a website is from the company's own, verified site.
    return f"Company website ({host})" if "://" in u else host


def _numbered_sources(groups: list[list[str]], evidence_by_id: dict[str, dict]) -> tuple[list[dict], list[list[int]]]:
    """Number the distinct sources behind each group of evidence ids, in order of first use:
    (the numbered list, each group's source numbers)."""
    numbers: dict[str, int] = {}
    listed: list[dict] = []
    per_group: list[list[int]] = []
    for ids in groups:
        mine: list[int] = []
        for evidence_id in ids:
            entry = evidence_by_id.get(evidence_id)
            if not entry:
                continue
            url = entry.get("source_url") or ""
            if url not in numbers:
                numbers[url] = len(numbers) + 1
                listed.append({
                    "n": numbers[url], "source": source_label(url, entry.get("source_class"), entry.get("linked_from")), "url": url,
                    "retrieved_at": entry.get("retrieved_at"),
                })
            if numbers[url] not in mine:
                mine.append(numbers[url])
        per_group.append(sorted(mine))
    return listed, per_group


def _marks(numbers: list[int]) -> str:
    return "".join(f"[{n}]" for n in numbers)


def _labelled_text(as_of: str, parts: list[tuple[str, str]]) -> str:
    """"As of D. What it does: ... [1] Money: ... [3] ..." from (part, cited text) pairs."""
    out = [f"As of {as_of}."]
    current = None
    for part, text in parts:
        if part != current:
            out.append(f"{PART_LABELS.get(part, part)}:")
            current = part
        out.append(text)
    return " ".join(out)


def _markdown(as_of: str, parts: list[tuple[str, str]], sources: list[dict]) -> str:
    lines = [f"**Summary as of {as_of}**", ""]
    current, buffer = None, []
    for part, text in parts + [("", "")]:
        if part != current:
            if buffer:
                lines += [f"**{PART_LABELS.get(current, current)}.** " + " ".join(buffer), ""]
            current, buffer = part, []
        if text:
            buffer.append(text)
    if sources:
        lines.append("**Sources**")
        lines += [
            f"{s['n']}. {s['source']}: {s['url']}" + (f" (retrieved {str(s['retrieved_at'])[:10]})" if s.get("retrieved_at") else "")
            for s in sources
        ]
    return "\n".join(lines).strip()


def to_envelope(
    profile: CompanyProfile,
    run_id: str,
    operations: Optional[dict] = None,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
) -> dict:
    """
    Build one submission envelope for `profile`, matching the reference
    agent's OUTPUT_CONTRACT.md shape exactly (claims/evidence/run/changes/
    errors/operations).

    Evidence is deduplicated by (source_url, content_hash) -- claims that
    came from the same page/response (e.g. two activity signals scraped off
    one page) share a single evidence entry, referenced by id, rather than
    repeating identical evidence for every claim.
    """
    ts = profile.run_timestamp
    started_at = started_at or ts
    completed_at = completed_at or ts
    operations = operations or {"requests": 0, "runtime_ms": 0, "third_party_cost_usd": 0.0}

    evidence_ids_by_key: dict[tuple[str, Optional[str], Optional[str]], str] = {}
    evidence_list: list[dict] = []
    evidence_id_by_claim: dict[int, str] = {}
    claims: list[dict] = []
    errors: list[dict] = []

    for field_name, claim in _claim_entries(profile):
        evidence_ids: list[str] = []
        if claim.state == EvidenceState.AVAILABLE and claim.source:
            # The span is the text that supports THIS claim, so it is part of
            # the key: two different claims from one page used to share one
            # evidence entry, leaving the second claim citing the first one's
            # span. Identical claims (same source, hash and span) still share.
            span = claim.evidence_span or claim.value
            key = (claim.source, claim.content_hash, span)
            evidence_id = evidence_ids_by_key.get(key)
            if evidence_id is None:
                evidence_id = f"ev-{len(evidence_list) + 1}"
                evidence_ids_by_key[key] = evidence_id
                evidence_list.append(
                    {
                        "id": evidence_id,
                        "source_url": claim.source,
                        "source_class": claim.source_class,
                        "retrieved_at": _iso(claim.retrieved_at) if claim.retrieved_at else None,
                        "content_sha256": claim.content_hash,
                        "claim_span": span,
                        "reporting_period": claim.reporting_period,
                        "effective_at": claim.effective_date,
                        "linked_from": claim.linked_from,
                    }
                )
            evidence_ids = [evidence_id]
            evidence_id_by_claim[id(claim)] = evidence_id

        claims.append(
            {
                "field": field_name,
                "value": claim.value,
                "availability": claim.state.value,
                "confidence": round(claim.match_confidence / 100, 4) if claim.match_confidence is not None else None,
                # "Keep the source, retrieval date and relevant reporting period
                # for every claim": the period used to stop at our internal
                # model and never reached the submitted artifact.
                "reporting_period": claim.reporting_period,
                "effective_at": claim.effective_date,
                "evidence_ids": evidence_ids,
                # Why the field is blank when we cannot tell (None otherwise).
                "reason": claim.reason,
            }
        )

        if claim.state in _ERROR_STATES:
            errors.append({"field": field_name, "state": claim.state.value, **({"reason": claim.reason} if claim.reason else {})})

    summary = build_summary(profile)

    def evidence_ids_of(claims: list) -> list[str]:
        return sorted(
            {evidence_id_by_claim[id(c)] for c in claims if id(c) in evidence_id_by_claim},
            key=lambda e: int(e.split("-")[1]),
        )

    evidence_by_id = {e["id"]: e for e in evidence_list}
    sentence_ids = [evidence_ids_of(s.claims) for s in summary.sentences]
    signal_ids = [evidence_ids_of(s.claims) for s in summary.signals]
    sources, numbered = _numbered_sources(sentence_ids + signal_ids, evidence_by_id)
    sentence_numbers, signal_numbers = numbered[: len(sentence_ids)], numbered[len(sentence_ids):]

    # The labelled text: each sentence with its source numbers, the signals, then each gap
    # with its reason.
    parts: list[tuple[str, str]] = []
    first = f"As of {summary.as_of}: "
    for s, numbers in zip(summary.sentences, sentence_numbers):
        if s.part == "unknown":
            continue
        text = s.text[len(first):] if s.text.startswith(first) else s.text
        parts.append((s.part, (text + " " + _marks(numbers)).strip()))
    if summary.signals:
        parts.append(("signals", "; ".join(
            (sig.text.rstrip(".") + " " + _marks(numbers)).strip() for sig, numbers in zip(summary.signals, signal_numbers)
        ) + "."))
    gaps = [f"can't tell {item}" for item in summary.cant_tell] + [
        f"{d['item']} ({d['reason']})" if d.get("reason") else d["item"] for d in summary.unknown_detail
    ]
    if gaps:
        parts.append(("unknown", "; ".join(gaps) + "."))
    elif parts:
        parts.append(("unknown", "nothing we looked for is missing."))
    unknown_detail = [
        {"item": item, "state": "ambiguous", "reason": None} for item in summary.cant_tell
    ] + [{"item": d["item"], "state": "not_available", "reason": d.get("reason") or None} for d in summary.unknown_detail]

    return {
        "organisation_number": profile.org_number,
        "run": {
            "run_id": run_id,
            "started_at": _iso(started_at),
            "completed_at": _iso(completed_at),
            "terminal_status": "completed",
        },
        "claims": claims,
        "evidence": evidence_list,
        "changes": list(profile.refresh_metadata.material_changes),
        "errors": errors,
        "operations": operations,
        # Decision-useful synthesis: ONE short dated narrative per company
        # (not a Q&A list -- official feedback asked for "a shorter dated
        # summary instead of a list"). Each sentence names the claim fields it
        # rests on and the evidence ids that support it; gaps are named under
        # "unknowns" rather than guessed. envelopes.jsonl is the submitted
        # artifact, so the summary has to live here, not only in the report.
        "summary": {
            "as_of": summary.as_of,
            "headline": summary.headline,
            # Labelled parts (what it does, money, people and presence, what changed, key
            # signals, what remains unknown), each sentence followed by its source numbers.
            "text": _labelled_text(summary.as_of, parts),
            # The same sentences without labels or source numbers.
            "plain_text": summary.text,
            "sentences": [
                {
                    "text": s.text,
                    "part": s.part,
                    "fields": s.fields,
                    "evidence_ids": ids,
                    "sources": numbers,
                }
                for s, ids, numbers in zip(summary.sentences, sentence_ids, sentence_numbers)
            ],
            "signals": [
                {"text": s.text, "fields": s.fields, "evidence_ids": ids, "sources": numbers}
                for s, ids, numbers in zip(summary.signals, signal_ids, signal_numbers)
            ],
            "sources": sources,
            "markdown": _markdown(summary.as_of, parts, sources),
            "changes": summary.changes,
            "unknowns": summary.unknowns,
            "unknown_detail": unknown_detail,
            "cant_tell": summary.cant_tell,
            "confidence": {"level": summary.confidence[0], "why": summary.confidence[1]},
        },
    }
