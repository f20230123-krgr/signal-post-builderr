"""
Evidence for a company's official website, taken from the website itself.

For the ~11% of companies whose website is registered in Brønnøysund, the
website claim used to be sourced from the registry snapshot alone: its evidence
was a data file (not a URL) and its supporting span was just the URL repeated.
Nothing on the company's own site backed it. The brief's own output example shows
the website claim evidenced by the site, quoting where it names the company
("Example AS, organisation number 123 456 789").

`official_site_evidence` builds that: after the crawl has fetched the site, it
returns one `official_site_evidence` fact whose

  * value is the site's address as it actually resolves (the canonical link, or
    where the request landed after redirects, e.g. abax.com -> abax.com/en-gb),
  * source is the page that carries the proof, with its retrieval time and hash,
  * span is a verbatim excerpt: the line naming the company's organisation
    number if any fetched page has one, otherwise the page's own name for the
    company (site name, JSON-LD name, title or heading).

The registry stays the identity anchor (the site was registered by the company);
this only adds evidence from the site. If the site cannot be fetched, redirects
to another domain, or yields no readable text, it returns None and the registry
claim stands exactly as before.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from src.models.profile import EvidenceState
from src.pipeline.careers import page_heading
from src.pipeline.crawl import FetchedPage
from src.pipeline.extract import _CANONICAL_LINK_RE, _CANONICAL_LINK_RE_ALT, _page_organization_name_from_html, _visible_lines
from src.pipeline.resolve import ResolvedEntity
from src.pipeline.verify import ConfirmedFact, name_similarity

FIELD_NAME = "official_site_evidence"

_ORG_NUMBER_RE = re.compile(r"(?<!\d)(\d{3})[ . ]?(\d{3})[ . ]?(\d{3})(?!\d)")
_SPAN_BEFORE = 70
_SPAN_AFTER = 40


def _domain(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _same_site(url: str, official_domain: str) -> bool:
    domain = _domain(url)
    return domain == official_domain or domain.endswith("." + official_domain)


def _canonical_link(html: str) -> Optional[str]:
    match = _CANONICAL_LINK_RE.search(html) or _CANONICAL_LINK_RE_ALT.search(html)
    return match.group(1).strip() if match and match.group(1).strip() else None


def org_number_span(html: str, org_number: str) -> Optional[str]:
    """The text around this organisation number, quoted as it reads on the page
    ("Org.nr. 918 965 556"), or None when the page never states it."""
    for line in _visible_lines(html):
        for match in _ORG_NUMBER_RE.finditer(line):
            if "".join(match.groups()) != org_number:
                continue
            start = max(0, match.start() - _SPAN_BEFORE)
            if start:  # begin at a word boundary, not mid-word
                space = line.find(" ", start)
                start = space + 1 if 0 <= space < match.start() else start
            return line[start : match.end() + _SPAN_AFTER].strip()
    return None


# A page name has to read like the company to count as evidence of it. Below this
# it is a slogan or somebody else's page (a property manager's title on a
# housing co-op's registered address), and the registry claim stands instead.
MIN_NAME_SPAN_SIMILARITY = 80


def name_span(html: str, legal_name: str) -> Optional[str]:
    """The page's own name for the company, verbatim: the best-matching of its
    site name / JSON-LD name and its title or heading, provided that it actually
    reads like the company's name."""
    candidates = [c for c in (_page_organization_name_from_html(html), page_heading(html)) if c]
    if not candidates:
        return None
    best = max(candidates, key=lambda c: name_similarity(legal_name, c))
    return best if name_similarity(legal_name, best) >= MIN_NAME_SPAN_SIMILARITY else None


def official_site_evidence(
    pages: list[FetchedPage], entity: ResolvedEntity, now: Optional[datetime] = None
) -> Optional[ConfirmedFact]:
    if not entity.official_site_candidate or not entity.legal_name:
        return None
    official_domain = _domain(entity.official_site_candidate)
    # Pages we REQUESTED on the registered domain; where they landed may differ.
    requested = [
        p for p in pages
        if p.fetch_state == EvidenceState.AVAILABLE and p.raw_html and p.linked_from is None
        and _same_site(p.url, official_domain)
    ]
    if not requested:
        return None

    homepage = requested[0]
    landed = homepage.final_url or homepage.url
    # A registered address that redirects to another domain (lundbeck.no -> lundbeck.com)
    # is the same company's site at its new address, and that is the address Builderr's
    # own crawl records. It is accepted only on the strength of the page we land on: it
    # must itself show the org number or a name that reads like the company, and only
    # pages from that landing domain count as evidence.
    moved = not _same_site(landed, official_domain)
    landed_domain = _domain(landed)
    own = [p for p in requested if not moved or _domain(p.final_url or p.url) == landed_domain]
    canonical = _canonical_link(homepage.raw_html)
    base_domain = landed_domain if moved else official_domain
    value = canonical if canonical and _same_site(canonical, base_domain) else landed
    if moved and urlsplit(value).query:
        value = landed.split("?")[0]

    evidence_page, span = None, None
    for page in own:
        found = org_number_span(page.raw_html, entity.org_number)
        if found:
            evidence_page, span = page, found
            break
    if span is None:
        for page in own:
            found = name_span(page.raw_html, entity.legal_name)
            if found:
                evidence_page, span = page, found
                break
    if evidence_page is None or span is None:
        return None

    return ConfirmedFact(
        field_name=FIELD_NAME,
        value=value,
        source_url=evidence_page.final_url or evidence_page.url,
        match_confidence=100.0,
        retrieved_at=evidence_page.fetched_at,
        content_hash=hashlib.sha256(evidence_page.raw_html.encode("utf-8")).hexdigest(),
        extraction_method="text",
        source_class="company_owned",
        evidence_span=span,
    )
