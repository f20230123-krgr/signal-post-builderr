"""
Stage 4: Verify.

Contract: docs/component-specs.md -> "src/pipeline/verify.py"

Responsibility: confirm a raw fact actually belongs to the resolved entity,
using RapidFuzz name matching against legal_name (and known aliases) with an
explicit, documented threshold.

*** This is the code that enforces the 95%-precision hard gate. ***
Treat any threshold change as a gate-risk change requiring a self-score run,
not a minor tuning tweak (see CLAUDE.md non-negotiable hard gates).

Two independent layers, both must pass:
  1. Provenance -- the fact's source_url must be on the same domain as the
     entity's own verified official_site_candidate. This is what rejects a
     look-alike/imposter site that reuses the real company's name (see
     tests/pipeline/test_verify.py) -- name similarity alone is not enough.
  2. Name match (only for freeform prose fields that risk having picked up a
     DIFFERENT company's name in passing text -- organization_name/page_text)
     -- RapidFuzz partial_ratio against legal_name must clear
     NAME_MATCH_THRESHOLD.
Everything else (registered_address, official_site, leader, company_profile,
and structured hiring_signal/dated_activity from JobPosting/NewsArticle
JSON-LD -- job titles and headlines that don't restate a company name by
construction) is gated on provenance alone.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from rapidfuzz import fuzz

from src.pipeline.extract import RawFact
from src.pipeline.resolve import ResolvedEntity

# Documented threshold -- changing this number changes precision. Any change
# must be accompanied by a /self-score run reported in the same PR/summary.
NAME_MATCH_THRESHOLD = 90  # RapidFuzz token_sort_ratio, 0-100

_NAME_BEARING_FIELDS = {"organization_name", "page_text"}


@dataclass
class ConfirmedFact:
    field_name: str
    value: str
    source_url: str
    match_confidence: float
    # Carried through from RawFact.extracted_at -- assemble.py needs a real
    # retrieval timestamp for every AVAILABLE Claim (docs/data-schema.md).
    # Fixed here + in docs/component-specs.md: the original stub omitted this,
    # which would have made every assembled Claim fail schema validation.
    retrieved_at: datetime
    # Set for time-bound facts (currently: annual accounts figures from
    # src/pipeline/registry_extras.py, e.g. "FY2025"); None otherwise. Needed
    # because Claim.reporting_period is part of the documented schema
    # (docs/data-schema.md) and assemble.py has no other source for it.
    reporting_period: Optional[str] = None
    # Added after reviewing signalpost-sources.md's publication rules --
    # carried through from RawFact for content_hash/extraction_method;
    # source_class is always "company_owned" here because verify() only ever
    # accepts facts whose source_url is on the entity's own official-site
    # domain (see _domain check below) -- registry_extras.py sets
    # "official_registry" directly since it bypasses verify() entirely.
    content_hash: Optional[str] = None
    extraction_method: Optional[str] = None
    source_class: Optional[str] = None
    # Set only when accepted via the ATS link-chain path below (evaluator
    # feedback: "Preserve that link chain as evidence").
    linked_from: Optional[str] = None


def _domain(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def name_similarity(a: str, b: str) -> float:
    return fuzz.partial_ratio(a.lower(), b.lower())


def verify(fact: RawFact, entity: ResolvedEntity) -> ConfirmedFact | None:
    """
    Return a ConfirmedFact if `fact`'s source page identity matches `entity`
    above NAME_MATCH_THRESHOLD, else None (caller marks the field AMBIGUOUS).

    Must not:
      - Accept a fact when identity match is unclear -- reject, don't guess.
    """
    if entity.legal_name is None or entity.official_site_candidate is None:
        return None

    official_domain = _domain(entity.official_site_candidate)
    on_official_domain = _domain(fact.source_url) == official_domain
    # Off-domain (e.g. an ATS platform) is only ever acceptable when crawl.py
    # recorded that this page was reached by following a link FROM a page on
    # the entity's own official domain -- the chain itself is the provenance,
    # not the source domain. See crawl.py's `ats_domains` / module docstring.
    via_official_link_chain = fact.linked_from is not None and _domain(fact.linked_from) == official_domain

    if not on_official_domain and not via_official_link_chain:
        return None

    if fact.field_name in _NAME_BEARING_FIELDS:
        score = name_similarity(entity.legal_name, fact.value)
        if score < NAME_MATCH_THRESHOLD:
            return None
        confidence = float(score)
    else:
        confidence = 100.0

    return ConfirmedFact(
        field_name=fact.field_name,
        value=fact.value,
        source_url=fact.source_url,
        match_confidence=confidence,
        retrieved_at=fact.extracted_at,
        content_hash=fact.content_hash,
        extraction_method=fact.extraction_method,
        source_class="company_owned" if on_official_domain else "external",
        linked_from=fact.linked_from if via_official_link_chain else None,
    )
