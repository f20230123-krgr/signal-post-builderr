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
from src.pipeline.spans import node_span
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
    ("Org.nr. 918 965 556"), or None when the page never states it.

    Taken from the one text node that shows the number, as the source writes it (a quote
    joined across tags is not in the page source; official C300 feedback). Falls back to the
    visible line only when no single node holds it."""
    def shows_it(text: str) -> bool:
        return any("".join(m.groups()) == org_number for m in _ORG_NUMBER_RE.finditer(text))

    sep = r"(?:\s|&nbsp;|&#160;|&#xa0;|\.)?"
    anchor = re.compile(sep.join((org_number[:3], org_number[3:6], org_number[6:])), re.IGNORECASE)
    node = node_span(html, shows_it, anchor)
    if node:
        return node
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


_TITLE_PART_RE = re.compile(r"\s+[–—|·:-]\s+")


def name_span(html: str, legal_name: str) -> Optional[str]:
    """The page's own name for the company, verbatim: the best-matching of its
    site name / JSON-LD name and its title or heading, provided that it actually
    reads like the company's name."""
    candidates = [c for c in (_page_organization_name_from_html(html), page_heading(html)) if c]
    if candidates:
        best = max(candidates, key=lambda c: name_similarity(legal_name, c))
        if name_similarity(legal_name, best) >= MIN_NAME_SPAN_SIMILARITY:
            return best
    # A title is often "Brand – tagline" or "Page – Brand" ("ipeople – Det handler om å finne de
    # riktige hodene"). A part counts only when it holds every distinctive word of the name: a
    # fuzzy score would let "Bergen" stand for BERGEN BIL AS.
    for title in candidates:
        for part in _TITLE_PART_RE.split(title):
            if _holds_the_whole_name(part.strip(), legal_name):
                return part.strip()
    return None


_FORM_WORDS = {"as", "asa", "sa", "ans", "da", "ba", "enk", "nuf", "norge", "norway", "norsk"}


def _words(text: str) -> list[str]:
    folded = text.lower().translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"}))
    return re.findall(r"[a-z0-9]+", folded)


def _holds_the_whole_name(part: str, legal_name: str) -> bool:
    core = [w for w in _words(legal_name) if w not in _FORM_WORDS]
    words = _words(part)
    if not core or not words:
        return False
    return "".join(core) == "".join(w for w in words if w not in _FORM_WORDS) or all(w in words for w in core)


# Official feedback (Nov): wrong websites "were mostly a parent or a builder" (olsenbygg.com
# lists Kaldvellfjorden Eiendom AS as a project; appelx.no redirects to a site showing a
# different org number). "Check that the page shows the company's own name or number."
NOT_READABLE = "the website could not be read"
NOT_SHOWN = "the website does not show this company's name or organisation number"


def other_org_reason(number: str) -> str:
    return f"the website shows another organisation's number ({number[:3]} {number[3:6]} {number[6:]}), not this company's"


# The pages where a site names its own owner: the home page and its contact/about pages. A
# name on a deeper page (a builder's project page, a customer story) is somebody else's.
_OWNER_PAGE_RE = re.compile(
    r"^/?(?:[a-z]{2}(?:-[a-z]{2})?(?:/|$))?(?:(?:kontakt|contact|om-?oss|om|about|about-us|firma|selskapet|company|impressum)[\w-]*/?)?$",
    re.IGNORECASE,
)
# An organisation number the page states as one ("Org.nr. 999 296 432", "Org: NO 990 859 671 MVA").
_STATED_ORG_RE = re.compile(
    r"org(?:anisasjons)?\.?\s*-?\s*(?:nr|nummer|no)?\.?\s*:?\s*(?:NO\s*)?(\d{3})[\s. ]?(\d{3})[\s. ]?(\d{3})",
    re.IGNORECASE,
)


def _valid_org_number(number: str) -> bool:
    """Brønnøysund's modulus-11 check digit, so a phone or account number is not read as one."""
    if len(number) != 9 or not number.isdigit():
        return False
    total = sum(int(d) * w for d, w in zip(number[:8], (3, 2, 7, 6, 5, 4, 3, 2)))
    check = 11 - total % 11
    return (0 if check == 11 else check) == int(number[8]) and check != 10


def stated_org_numbers(html: str) -> set[str]:
    text = re.sub(r"<[^>]+>", " ", html).replace("&nbsp;", " ").replace("&#160;", " ")
    return {"".join(m.groups()) for m in _STATED_ORG_RE.finditer(text) if _valid_org_number("".join(m.groups()))}


def _is_owner_page(url: str) -> bool:
    return bool(_OWNER_PAGE_RE.match(urlsplit(url).path or "/"))


def official_site_evidence(
    pages: list[FetchedPage], entity: ResolvedEntity, now: Optional[datetime] = None
) -> Optional[ConfirmedFact]:
    return site_ownership(pages, entity, now)[0]


def site_ownership(
    pages: list[FetchedPage], entity: ResolvedEntity, now: Optional[datetime] = None
) -> tuple[Optional[ConfirmedFact], Optional[str]]:
    """The proof that the registered website is this company's (its org number on the site,
    or its own name as the site's name on the home or contact page), or None and why not."""
    if not entity.official_site_candidate or not entity.legal_name:
        return None, None
    official_domain = _domain(entity.official_site_candidate)
    # Pages we REQUESTED on the registered domain; where they landed may differ.
    requested = [
        p for p in pages
        if p.fetch_state == EvidenceState.AVAILABLE and p.raw_html and p.linked_from is None
        and _same_site(p.url, official_domain)
    ]
    if not requested:
        return None, NOT_READABLE

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
            if not _is_owner_page(page.final_url or page.url):
                continue
            found = name_span(page.raw_html, entity.legal_name)
            if found:
                evidence_page, span = page, found
                break
    if evidence_page is None or span is None:
        # A site that neither names this company nor states its number, but states another
        # organisation's, is that organisation's site. (A group's own site naming the group
        # while printing its operating subsidiary's number, afgruppen.no, is proven above.)
        others = set().union(*(stated_org_numbers(p.raw_html) for p in own)) - {entity.org_number}
        if others:
            return None, other_org_reason(sorted(others)[0])
        return None, NOT_SHOWN

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
    ), None
