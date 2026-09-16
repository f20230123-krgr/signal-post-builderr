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
# Events date themselves with startDate rather than datePublished -- kept as
# their own set so the two date properties stay explicitly separated.
_EVENT_TYPES = {"Event", "BusinessEvent", "EducationEvent", "ExhibitionEvent"}

_META_OG_SITE_NAME_RE = re.compile(
    r'<meta\b[^>]*\bproperty\s*=\s*["\']og:site_name["\'][^>]*\bcontent\s*=\s*["\']([^"\']*)["\']', re.IGNORECASE
)
# content before property is also valid HTML attribute order -- handle both.
_META_OG_SITE_NAME_RE_ALT = re.compile(
    r'<meta\b[^>]*\bcontent\s*=\s*["\']([^"\']*)["\'][^>]*\bproperty\s*=\s*["\']og:site_name["\']', re.IGNORECASE
)
_META_APP_NAME_RE = re.compile(
    r'<meta\b[^>]*\bname\s*=\s*["\'](?:application-name|apple-mobile-web-app-title)["\']'
    r'[^>]*\bcontent\s*=\s*["\']([^"\']*)["\']',
    re.IGNORECASE,
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
_NON_ORGANIZATION_TYPES = _JOB_POSTING_TYPES | _DATED_ACTIVITY_TYPES | _EVENT_TYPES | {
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
        if not isinstance(obj, dict):
            continue
        name = _as_str(obj.get("name"))
        if not name:
            continue
        obj_type = obj.get("@type")
        obj_types = set(obj_type if isinstance(obj_type, list) else [obj_type])
        if obj_types & _NON_ORGANIZATION_TYPES:
            continue
        return name
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


def _as_str(value: object) -> Optional[str]:
    """A JSON-LD text-typed property should be a string, but schema.org
    legally allows several of these (name, title, headline, jobTitle...) to
    be a LIST of alternates instead (real-world regression: crashed a real
    1,000-company batch with "AttributeError: 'list' object has no
    attribute 'lower'" deep inside name-matching, since a list value flowed
    all the way from here into a RawFact and then into RapidFuzz). Treat
    anything that isn't a non-empty string as absent -- same "reject, don't
    guess" discipline already used for sameAs links -- rather than crash or
    silently pick one of several asserted alternate values."""
    return value if isinstance(value, str) and value else None


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

        title = _as_str(obj.get("title"))
        if any(t in _JOB_POSTING_TYPES for t in obj_types) and title:
            date_posted = obj.get("datePosted")
            value = f"{title} (posted {date_posted})" if date_posted else title
            hiring_org = obj.get("hiringOrganization")
            hiring_org_name = _as_str(hiring_org.get("name")) if isinstance(hiring_org, dict) else None
            facts.append(
                RawFact("hiring_signal", value, source_url, "structured", extracted_at, context_name=hiring_org_name)
            )

        obj_name = _as_str(obj.get("name"))
        headline = _as_str(obj.get("headline")) or obj_name or "activity"
        # schema.org puts the date on a different property per type:
        # Article-family objects use datePublished, Event uses startDate.
        # Only checking datePublished silently skipped every event a company
        # publishes on its own site -- real dated activity, already on a page
        # we fetch anyway. An undated event is still not dated activity: the
        # date IS the claim, so it's dropped rather than guessed.
        activity_date = None
        if any(t in _DATED_ACTIVITY_TYPES for t in obj_types):
            activity_date = obj.get("datePublished")
        elif any(t in _EVENT_TYPES for t in obj_types):
            activity_date = obj.get("startDate")

        if activity_date:
            facts.append(
                RawFact(
                    "dated_activity",
                    f"{headline} ({activity_date})",
                    source_url,
                    "structured",
                    extracted_at,
                    context_name=page_context_name,
                )
            )

        if obj_name:
            facts.append(RawFact("organization_name", obj_name, source_url, "structured", extracted_at))
        site_url = _as_str(obj.get("url"))
        if site_url:
            facts.append(RawFact("official_site", site_url, source_url, "structured", extracted_at))
        addr = obj.get("address")
        if isinstance(addr, dict):
            addr_str = ", ".join(
                p for p in (addr.get("streetAddress"), addr.get("postalCode"), addr.get("addressLocality")) if p
            )
            if addr_str:
                facts.append(RawFact("registered_address", addr_str, source_url, "structured", extracted_at))
        context_name = obj_name
        employees = obj.get("employee")
        if isinstance(employees, list):
            for emp in employees:
                emp_name = _as_str(emp.get("name")) if isinstance(emp, dict) else None
                if emp_name:
                    emp_title = _as_str(emp.get("jobTitle"))
                    value = f"{emp_name} ({emp_title})" if emp_title else emp_name
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
    else:
        # Plenty of real sites ship no og:site_name but do declare
        # application-name / apple-mobile-web-app-title. Both are single,
        # structured, self-declared brand tags on the entity's own verified
        # domain -- same provenance as og:site_name, no extra request. Only
        # consulted when og:site_name is absent: it's the more explicit
        # declaration and must not be shadowed by a weaker tag.
        fallback = _META_APP_NAME_RE.search(html)
        if fallback and fallback.group(1).strip():
            facts.append(RawFact("public_brand", fallback.group(1).strip(), source_url, "structured", extracted_at))

    canonical = _CANONICAL_LINK_RE.search(html) or _CANONICAL_LINK_RE_ALT.search(html)
    if canonical and canonical.group(1):
        facts.append(RawFact("official_site", canonical.group(1), source_url, "structured", extracted_at))

    return facts


# Company-owned social profiles, extracted from plain markup rather than only
# from JSON-LD `sameAs`. Real-world gap: company_profile was available for
# just ~4% of companies in a full 1,000-company batch, because most real sites
# link their profiles from a footer <a href> and publish no structured data at
# all. Provenance is unchanged and is what carries the precision here -- these
# links are only ever read from the entity's own verified domain (verify.py
# gates on that, and the housing-manager filter still applies to
# company_profile specifically), so this adds reach, not identity risk.
_SOCIAL_PROFILE_HOST_RE = re.compile(
    r"^https?://(?:[\w-]+\.)*(linkedin\.com|facebook\.com|instagram\.com|twitter\.com|x\.com|youtube\.com|tiktok\.com)/",
    re.IGNORECASE,
)
# A share/intent/plugin URL is the page telling a visitor how to repost it --
# not the company declaring a profile it owns. Publishing one as a
# "company-owned profile" would be a wrong claim, not merely a thin one.
_SOCIAL_NON_PROFILE_RE = re.compile(
    r"/(?:sharer|share|shareArticle|intent|dialog|plugins|widgets|embed)\b|[?&]u=|[?&]url=",
    re.IGNORECASE,
)
_ANCHOR_HREF_RE = re.compile(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)

MAX_SOCIAL_PROFILE_LINKS = 8


def social_profile_facts(
    html: str, source_url: str, extracted_at: datetime, context_name: Optional[str] = None
) -> list[RawFact]:
    """company_profile facts from plain <a href> social links on the page.

    Deduped by URL and capped: a site-wide template repeats the same links on
    every page, and one page can list many, so without both this section
    would fill with duplicates of a single footer (same discipline as
    assemble.py's claim dedup)."""
    seen: set[str] = set()
    facts: list[RawFact] = []

    for href in _ANCHOR_HREF_RE.findall(html):
        link = href.strip()
        if link in seen:
            continue
        if not _SOCIAL_PROFILE_HOST_RE.match(link):
            continue
        if _SOCIAL_NON_PROFILE_RE.search(link):
            continue
        seen.add(link)
        facts.append(
            RawFact("company_profile", link, source_url, "structured", extracted_at, context_name=context_name)
        )
        if len(facts) >= MAX_SOCIAL_PROFILE_LINKS:
            break

    return facts


# A declared RSS/Atom feed is the richest dated_activity a company publishes
# about itself: real headlines with real publication dates, structured, on its
# own domain. Registry update events (registry_extras) give company-level
# coverage everywhere including the ~89% with no site at all; this adds real
# editorial depth for the companies that actually publish. Capped like every
# other list section -- a feed can carry hundreds of entries.
MAX_FEED_ACTIVITY_ENTRIES = 5

_FEED_ITEM_RE = re.compile(r"<(?:item|entry)\b[^>]*>(.*?)</(?:item|entry)>", re.IGNORECASE | re.DOTALL)
_FEED_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_FEED_DATE_RE = re.compile(
    r"<(?:pubDate|published|updated|dc:date)\b[^>]*>(.*?)</(?:pubDate|published|updated|dc:date)>",
    re.IGNORECASE | re.DOTALL,
)
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)


def _feed_text(raw: str) -> str:
    unwrapped = _CDATA_RE.sub(r"\1", raw)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unwrapped)).strip()


def looks_like_feed(content: str) -> bool:
    head = content[:2000].lower()
    return "<rss" in head or "<feed" in head or "<rdf:rdf" in head


def feed_activity_facts(content: str, source_url: str, extracted_at: datetime) -> list[RawFact]:
    """dated_activity from an RSS/Atom feed's entries.

    An entry with no date is skipped rather than published undated -- the
    date IS the claim, same discipline as the JSON-LD Event handling."""
    if not looks_like_feed(content):
        return []

    facts: list[RawFact] = []
    for raw_item in _FEED_ITEM_RE.findall(content):
        date_match = _FEED_DATE_RE.search(raw_item)
        if not date_match:
            continue
        title_match = _FEED_TITLE_RE.search(raw_item)
        title = _feed_text(title_match.group(1)) if title_match else "activity"
        published = _feed_text(date_match.group(1))
        if not title or not published:
            continue
        facts.append(
            RawFact("dated_activity", f"{title} ({published})", source_url, "structured", extracted_at)
        )
        if len(facts) >= MAX_FEED_ACTIVITY_ENTRIES:
            break

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

    # A feed is XML, not HTML -- running extruct/Trafilatura over it produces
    # nothing useful at best and garbage text at worst, so route it to the
    # feed parser instead of the HTML path.
    if looks_like_feed(page.raw_html):
        feed_facts = feed_activity_facts(page.raw_html, page.url, extracted_at)
        content_hash = hashlib.sha256(page.raw_html.encode("utf-8")).hexdigest()
        for fact in feed_facts:
            fact.content_hash = content_hash
            fact.linked_from = page.linked_from
        return feed_facts

    facts = structured_facts(page.raw_html, page.url, extracted_at)

    if not facts:
        facts = text_fallback_facts(page.raw_html, page.url, extracted_at)

    page_context_name = _page_organization_name_from_html(page.raw_html)

    facts += _meta_tag_facts(page.raw_html, page.url, extracted_at)
    facts += _activity_facts(page.raw_html, page.url, extracted_at, context_name=page_context_name)
    facts += social_profile_facts(page.raw_html, page.url, extracted_at, context_name=page_context_name)

    content_hash = hashlib.sha256(page.raw_html.encode("utf-8")).hexdigest()
    for fact in facts:
        fact.content_hash = content_hash
        fact.linked_from = page.linked_from
    return facts
