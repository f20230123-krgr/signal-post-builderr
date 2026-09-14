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
# A genuine dated-activity headline/sentence is short prose. Real-world
# regression, found by independent review of a real 100-company batch: a
# shared directory-listing domain with no HTML block tags to split on
# produced a single ~8,000-character line (dozens of concatenated unrelated
# businesses' URLs/dates) that was published whole as one "dated_activity"
# claim. This caps free-text dated_activity lines at a generous length no
# real headline would ever approach -- defense in depth alongside blacklisting
# the specific offending domain (see discovery.py's AGGREGATOR_DOMAIN_BLACKLIST),
# so the next unblacklisted directory site can't reproduce the same failure.
_MAX_FREETEXT_LINE_LENGTH = 300

# schema.org JSON-LD types treated as job/dated-activity signals, per
# evaluator feedback ("JSON-LD Organization, sameAs, JobPosting and
# datePublished fields"). Kept separate from the Organization-identity
# handling above -- a page can have both an Organization block and one or
# more JobPosting/Article blocks.
_JOB_POSTING_TYPES = {"JobPosting"}
_DATED_ACTIVITY_TYPES = {"NewsArticle", "Article", "BlogPosting", "PressRelease"}

_META_OG_SITE_NAME_RE = re.compile(
    r'<meta\b[^>]*\bproperty\s*=\s*["\']og:site_name["\'][^>]*\bcontent\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE
)
# content before property is also valid HTML attribute order -- handle both.
_META_OG_SITE_NAME_RE_ALT = re.compile(
    r'<meta\b[^>]*\bcontent\s*=\s*["\']([^"\']*)["\'][^>]*\bproperty\s*=\s*["\']og:site_name["\']', re.IGNORECASE
)
_CANONICAL_LINK_RE = re.compile(
    r'<link\b[^>]*\brel\s*=\s*["\']canonical["\'][^>]*\bhref\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE
)
_CANONICAL_LINK_RE_ALT = re.compile(
    r'<link\b[^>]*\bhref\s*=\s*["\']([^"\']*)["\'][^>]*\brel\s*=\s*["\']canonical["\']', re.IGNORECASE
)


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
    # Carried through from FetchedPage.linked_from -- set only when this
    # page was reached by following an outbound link from a page on the
    # entity's own official domain (see crawl.py's `ats_domains`). Lets
    # verify() accept off-domain facts on link-chain provenance instead of
    # direct-domain provenance. None for every ordinary same-domain page.
    linked_from: Optional[str] = None
    # Real-world regression (real evaluator feedback): a shared/multi-tenant
    # domain (e.g. a Norwegian housing-cooperative property manager like
    # OBOS or USBL, which many small housing co-ops legitimately list as
    # their registered "website") has its OWN JSON-LD Organization block
    # with its OWN sameAs/employee data. Domain-provenance alone let that
    # data get attributed to whatever entity's official_site happened to be
    # that shared domain. context_name is the co-located JSON-LD object's
    # own `name` field (e.g. "OBOS BBL") -- verify() requires this to
    # name-match the entity when present, closing that gap. None when the
    # source JSON-LD had no name of its own to check.
    context_name: Optional[str] = None


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


# Content/page types that describe an ARTICLE OR PAGE, not the site's own
# organization identity -- used to EXCLUDE candidates in _page_organization_name
# rather than allow-listing organization types. Real-world regression, found
# by independent review: schema.org has dozens of Organization/LocalBusiness
# subtypes (AutomotiveBusiness, FinancialService, Store, ...) that Norwegian
# sites commonly use -- an allow-list is the same losing game as a domain
# blacklist, always one step behind. A deny-list of the much smaller, stable
# set of non-organization content types is safe to broaden: a missing or
# wrong context_name can only make verify() over-reject (lost coverage), never
# wrongly accept (lost precision) -- see RawFact.context_name's docstring.
_NON_ORGANIZATION_TYPES = _JOB_POSTING_TYPES | _DATED_ACTIVITY_TYPES | {
    "WebPage", "AboutPage", "ContactPage", "CollectionPage", "FAQPage",
    "BreadcrumbList", "ItemList", "Product", "Person",
}


def _flatten_json_ld(objects: list) -> list:
    """Unwrap "@graph"-wrapped JSON-LD blocks (a common real-world pattern,
    e.g. from SEO plugins) into their nested objects. Real-world regression,
    found by independent review: extruct returns a @graph wrapper as ONE
    opaque object with keys ["@context", "@graph"] -- @type/name/
    datePublished etc. all live on the objects NESTED inside the @graph
    array, not on the wrapper itself. Without this, EVERY structured fact
    type (organization_name, leader, company_profile, hiring_signal,
    dated_activity) is silently invisible on any page using this pattern."""
    flattened = []
    for obj in objects:
        if isinstance(obj, dict) and isinstance(obj.get("@graph"), list):
            flattened.extend(_flatten_json_ld(obj["@graph"]))
        else:
            flattened.append(obj)
    return flattened


def _page_organization_name(json_ld_objects: list) -> Optional[str]:
    """The page's own JSON-LD organization `name`, if any -- same shared/
    multi-tenant-domain protection `context_name` already gives
    leader/company_profile facts (see RawFact.context_name's docstring),
    extended to dated_activity. Real-world regression, found auditing a real
    1,000-company batch: a housing co-op (borettslag) with a property
    manager (USBL/OBOS) as its registered website inherited that manager's
    OWN "Om oss"/"Ledige stillinger"/"Presserom" pages as its dated_activity
    -- those CMS-generated pages each carry a generic Article-type JSON-LD
    block (page name + datePublished) alongside a separate Organization
    block identifying the actual site owner. Unlike leader/company_profile
    (whose identity comes from the SAME object as the data), a dated_activity
    signal's identity has to be found elsewhere on the page."""
    for obj in _flatten_json_ld(json_ld_objects):
        if not isinstance(obj, dict) or not obj.get("name"):
            continue
        obj_type = obj.get("@type")
        obj_types = set(obj_type if isinstance(obj_type, list) else [obj_type])
        if obj_types & _NON_ORGANIZATION_TYPES:
            continue
        return obj["name"]
    return None


def _page_organization_name_from_html(html: str) -> Optional[str]:
    try:
        data = extruct.extract(html, syntaxes=["json-ld"], errors="ignore")
        name = _page_organization_name(data.get("json-ld", []))
    except Exception:
        name = None
    if name:
        return name
    # Fallback for a site with no JSON-LD Organization block at all but a
    # real og:site_name meta tag -- same safe-direction reasoning as the
    # deny-list above: still worth trying before giving dated_activity up
    # as unprotected entirely.
    site_name = _META_OG_SITE_NAME_RE.search(html) or _META_OG_SITE_NAME_RE_ALT.search(html)
    return site_name.group(1) if site_name and site_name.group(1) else None


def structured_facts(html: str, source_url: str, extracted_at: datetime) -> list[RawFact]:
    try:
        data = extruct.extract(html, syntaxes=["json-ld"], errors="ignore")
    except Exception:
        return []

    json_ld_objects = _flatten_json_ld(data.get("json-ld", []))
    page_context_name = _page_organization_name(json_ld_objects)

    facts: list[RawFact] = []
    for obj in json_ld_objects:
        if not isinstance(obj, dict):
            continue

        obj_type = obj.get("@type")
        obj_types = obj_type if isinstance(obj_type, list) else [obj_type]

        if any(t in _JOB_POSTING_TYPES for t in obj_types) and obj.get("title"):
            date_posted = obj.get("datePosted")
            value = f"{obj['title']} (posted {date_posted})" if date_posted else obj["title"]
            hiring_org = obj.get("hiringOrganization")
            hiring_org_name = hiring_org.get("name") if isinstance(hiring_org, dict) else None
            facts.append(
                RawFact("hiring_signal", value, source_url, "structured", extracted_at, context_name=hiring_org_name)
            )

        if any(t in _DATED_ACTIVITY_TYPES for t in obj_types) and obj.get("datePublished"):
            headline = obj.get("headline") or obj.get("name") or "activity"
            facts.append(
                RawFact(
                    "dated_activity",
                    f"{headline} ({obj['datePublished']})",
                    source_url,
                    "structured",
                    extracted_at,
                    context_name=page_context_name,
                )
            )

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
        context_name = obj.get("name")
        employees = obj.get("employee")
        if isinstance(employees, list):
            for emp in employees:
                if isinstance(emp, dict) and emp.get("name"):
                    title = emp.get("jobTitle")
                    value = f"{emp['name']} ({title})" if title else emp["name"]
                    facts.append(RawFact("leader", value, source_url, "structured", extracted_at, context_name=context_name))
        same_as = obj.get("sameAs")
        if isinstance(same_as, list):
            for link in same_as:
                if isinstance(link, str) and link:
                    facts.append(RawFact("company_profile", link, source_url, "structured", extracted_at, context_name=context_name))
    return facts


def _meta_tag_facts(html: str, source_url: str, extracted_at: datetime) -> list[RawFact]:
    """OpenGraph og:site_name and <link rel="canonical">, per evaluator
    feedback ("OpenGraph metadata; canonical links"). Regex-based like the
    rest of this module's lightweight HTML scanning -- these are single,
    well-formed tags, not full document structure."""
    facts: list[RawFact] = []

    site_name = _META_OG_SITE_NAME_RE.search(html) or _META_OG_SITE_NAME_RE_ALT.search(html)
    if site_name and site_name.group(1):
        facts.append(RawFact("public_brand", site_name.group(1), source_url, "structured", extracted_at))

    canonical = _CANONICAL_LINK_RE.search(html) or _CANONICAL_LINK_RE_ALT.search(html)
    if canonical and canonical.group(1):
        facts.append(RawFact("official_site", canonical.group(1), source_url, "structured", extracted_at))

    return facts


def text_fallback_facts(html: str, source_url: str, extracted_at: datetime) -> list[RawFact]:
    try:
        text = trafilatura.extract(html)
    except Exception:
        text = None
    if not text:
        return []
    return [RawFact("page_text", text, source_url, "text", extracted_at)]


def _activity_facts(
    html: str, source_url: str, extracted_at: datetime, context_name: Optional[str] = None
) -> list[RawFact]:
    """dated_activity only. hiring_signal is deliberately NOT free-text
    scanned here -- real evaluator feedback: a bare careers-page heading and
    a business literally offering "career counselling" as its own service
    both matched the old keyword scan and were published as false hiring
    evidence. "Require an actual job ad before making that claim" -- the
    JSON-LD JobPosting path in structured_facts() is that actual job ad;
    free text mentioning the word "hiring"/"career" is not.

    `context_name` (the page's own JSON-LD Organization name, if any) is
    attached to every fact produced here for the same reason structured_facts
    attaches it to its own dated_activity facts -- see
    _page_organization_name's docstring."""
    facts: list[RawFact] = []
    for line in _visible_lines(html):
        if len(line) > _MAX_FREETEXT_LINE_LENGTH:
            continue
        if _DATE_RE.search(line):
            facts.append(RawFact("dated_activity", line, source_url, "text", extracted_at, context_name=context_name))
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
    facts = structured_facts(page.raw_html, page.url, extracted_at)

    if not facts:
        facts = text_fallback_facts(page.raw_html, page.url, extracted_at)

    page_context_name = _page_organization_name_from_html(page.raw_html)

    facts += _meta_tag_facts(page.raw_html, page.url, extracted_at)
    facts += _activity_facts(page.raw_html, page.url, extracted_at, context_name=page_context_name)

    content_hash = hashlib.sha256(page.raw_html.encode("utf-8")).hexdigest()
    for fact in facts:
        fact.content_hash = content_hash
        fact.linked_from = page.linked_from
    return facts
