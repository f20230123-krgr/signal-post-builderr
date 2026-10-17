"""
Hiring signals from a company's own careers page.

Official feedback on the 3rd submission: the pool's hiring facts are careers-page
URLs on company sites (e.g. a company's /careers page), and our hiring coverage
was 0.0%. Two things caused it: the crawler stopped guessing /careers and /jobs
paths (a request-saving trim), and extraction only accepted JSON-LD JobPosting
blocks, which most small sites never publish.

Earlier feedback pulls the other way and still stands: a bare "Careers" heading
(or a business that sells career counselling) is not evidence of hiring.
"Require an actual job ad before making that claim." So this module does both:

  1. FIND the page without guessing: `find_careers_links` reads the links a page
     already contains (same site, its subdomains, or a known applicant-tracking
     platform) -- no blind path probing, so a site with no careers link costs
     nothing.
  2. PROVE it: `careers_page_facts` publishes a hiring signal only when the page
     itself lists at least `MIN_ROLE_LINKS` distinct job-detail links with
     titles, and says nothing when the page states there are no openings. The
     evidence span is one listed role title, quoted as it appears on the page.

Pure functions over HTML strings: no network, so they are testable offline.
"""
from __future__ import annotations

import re
from datetime import datetime
from html import unescape
from typing import TYPE_CHECKING, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

if TYPE_CHECKING:  # extract.py imports crawl.py, which imports this module
    from src.pipeline.extract import RawFact

# Every careers-page claim value starts with this; verify() relies on it to
# apply the shared-domain rule to exactly these claims.
CAREERS_VALUE_PREFIX = "Careers page"
# A page that is one job ad (reached from the careers page) but carries no JobPosting data.
JOB_AD_VALUE_PREFIX = "Job ad"
# One open role, listed on the company's careers page: "Open role: <title> - <direct url>".
ROLE_VALUE_PREFIX = "Open role"
MAX_ROLE_CLAIMS = 15

# What the VISIBLE TEXT of a careers link says. Anchored on purpose: an anchor
# that merely contains "jobb" ("Slik jobber vi med baerekraft") or "bli med"
# ("Bli medlem") is not a careers link.
CAREERS_WORD_RE = re.compile(
    r"^(?:(?:job\s*)?(?:careers?|karriere|jobs?|jobb|stillinger|vacancies|job\s+vacancies|open\s+positions)"
    r"|ledige\s+stillinger|ledige\s+jobber|jobb(?:e)?\s+(?:hos|i|med|for)\s+\S+(?:\s+\S+)?"
    r"|work\s+(?:with|at|for)\s+\S+(?:\s+\S+)?|join\s+(?:us|our\s+team|the\s+team)"
    r"|jobb\s*(?:&|og)\s*karriere|karriere\s*(?:&|og)\s*jobb|kom\s+og\s+jobb\s+(?:hos|for)\s+\S+)$",
    re.IGNORECASE,
)
# A careers PAGE's own title/heading: looser than a link's text, since a title
# reads "Careers | Acme" or "Ledige stillinger - Acme AS".
CAREERS_HEADING_RE = re.compile(
    r"careers?|karriere|\bjobs?\b|jobb|ledige\s+stillinger|stillinger|vacanc|open\s+positions|work\s+(with|at|for)|join\s+(us|our)",
    re.IGNORECASE,
)
_CAREERS_PATH_RE = re.compile(
    r"/(careers?|karriere|jobs?|jobb|jobber|stillinger|ledige-stillinger|jobbe-hos-oss|jobb-hos-oss|vacancies|job-vacancies)(/|$|\?)",
    re.IGNORECASE,
)
_CAREERS_HOST_RE = re.compile(r"^(careers?|karriere|jobs?|jobb)\.", re.IGNORECASE)
# A link to ONE job: a job-ish path segment followed by a further segment, or a
# numeric/id style detail URL.
#
# Deliberately strict. A bare /jobb/<slug> or /careers/<slug> is as likely a team
# introduction ("Møt Anna og Lars"), a testimonial or a benefits page as a job ad, and
# publishing those as "open roles" would be a wrong claim. A link counts only when its URL
# says it is an ad: an ad-ish word (stilling, ledig, vacancy, position, opening, annonse,
# apply) followed by a segment, a job/career path followed by a numeric id
# (/jobs/7116722-senior-consultant), or an id parameter.
_JOB_DETAIL_RE = re.compile(
    r"/[\w-]*(stilling|position|vacanc|ledig|opening|annonse|apply)[\w-]*/[^/?#]+"
    r"|/(jobs?|jobb|jobber|careers?|karriere)/\d{4,}[^/?#]*"
    r"|[?&](jobid|job_id|adid|positionid|id)=\d+|/\d{5,}(?=[-_/?#.]|$)",
    re.IGNORECASE,
)
# Pages of an applicant-tracking system that are the company's back office or a login, not
# a public vacancies page (a Teamtailor dashboard URL was published as a "careers page").
_NOT_PUBLIC_RE = re.compile(r"/(dashboard|admin|login|logon|sign[_-]?in|sign[_-]?up|auth|account|settings|embed)(/|$|\?)", re.IGNORECASE)
_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.IGNORECASE | re.DOTALL)
_HREF_ATTR_RE = re.compile(r'\bhref\s*=\s*["\']([^"\']+)["\']', re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_NO_OPENINGS_RE = re.compile(
    r"ingen\s+(ledige|åpne)\s+(stillinger|jobber)|ingen\s+ledige|har\s+ingen\s+\w*\s*stillinger|"
    r"(?:no|don'?t\s+have|do\s+not\s+have|have\s+no|currently\s+no)\s+(?:any\s+)?(?:open|current|available)?\s*"
    r"(?:positions|vacancies|openings|jobs)|no\s+vacancies|there\s+are\s+no\s+(?:open\s+)?(?:positions|vacancies|openings)|"
    r"not\s+(?:currently\s+)?(?:hiring|recruiting)",
    re.IGNORECASE,
)
# Link texts that are navigation or calls to action, not the title of a role.
_NAV_TEXT_RE = re.compile(
    r"^(se\s+alle|vis\s+alle|view\s+all|see\s+(all|available|open|our)|all\s+(jobs|positions)|read\s+more|les\s+mer|apply(\s+now)?|"
    r"søk(\s+nå)?|søk\s+her|send\s+(inn\s+)?søknad|open\s+application|åpen\s+søknad|more|mer|next|neste|previous|"
    r"forrige|back|tilbake|home|hjem|careers?|karriere|jobs?|jobb|stillinger|ledige\s+stillinger|"
    r"contact|kontakt|about|om\s+oss|login|logg\s+inn)\b",
    re.IGNORECASE,
)
# Footer and legal links that sit on every page, careers pages and job ads included.
_BOILERPLATE_TEXT_RE = re.compile(
    r"vilkår|personvern|privacy|cookie|terms|informasjonskapsl|tilgjengelighet|accessibility|"
    r"sitemap|nettkart|kontakt|contact|facebook|linkedin|instagram|youtube|twitter",
    re.IGNORECASE,
)
MIN_ROLE_LINKS = 2
MAX_CAREERS_LINKS = 2
_MAX_TITLE_LEN = 110
_MIN_TITLE_LEN = 4


def _text(fragment: str) -> str:
    return _WS_RE.sub(" ", unescape(_TAG_RE.sub(" ", fragment))).strip()


def _domain(url: str) -> str:
    try:
        netloc = urlsplit(url).netloc.lower()
    except ValueError:  # malformed link (unclosed IPv6 bracket)
        return ""
    return netloc[4:] if netloc.startswith("www.") else netloc


def _same_site(domain: str, official_domain: str) -> bool:
    return domain == official_domain or domain.endswith("." + official_domain)


def _on_any(domain: str, domains: set[str]) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def _anchors(html: str, base_url: str) -> list[tuple[str, str]]:
    """(absolute url, visible text) for every anchor, scripts/styles removed."""
    out: list[tuple[str, str]] = []
    for attrs, inner in _ANCHOR_RE.findall(_SCRIPT_STYLE_RE.sub(" ", html)):
        href = _HREF_ATTR_RE.search(attrs)
        if not href:
            continue
        raw = unescape(href.group(1).strip())
        if raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            out.append((urljoin(base_url, raw), _text(inner)))
        except ValueError:  # malformed href: skip it, don't fail the page
            continue
    return out


def is_careers_url(url: str, ats_domains: Optional[set[str]] = None) -> bool:
    """True for a URL that looks like a careers/jobs page or sits on an ATS host."""
    parts = urlsplit(url)
    domain = _domain(url)
    if _NOT_PUBLIC_RE.search(parts.path):
        return False
    if ats_domains and _on_any(domain, ats_domains):
        return True
    return bool(_CAREERS_HOST_RE.match(domain) or _CAREERS_PATH_RE.search(parts.path + ("?" if parts.query else "")))


def find_careers_links(
    html: str,
    page_url: str,
    official_domain: str,
    ats_domains: Optional[set[str]] = None,
    limit: int = MAX_CAREERS_LINKS,
) -> list[str]:
    """Careers-page URLs this page links to, best first, without guessing paths.

    A link qualifies when its text or its URL says careers/jobs AND it stays on
    the company's own site (including subdomains such as karriere.example.no) or
    goes to a known applicant-tracking platform. Individual job pages are not
    returned here: the list page that links to them is what we want."""
    ats_domains = ats_domains or set()
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, (url, text) in enumerate(_anchors(html, page_url)):
        domain = _domain(url)
        if not (_same_site(domain, official_domain) or _on_any(domain, ats_domains)):
            continue
        looks = bool(_CAREERS_PATH_RE.search(urlsplit(url).path)) or bool(_CAREERS_HOST_RE.match(domain)) or (
            0 < len(text) <= 40 and bool(CAREERS_WORD_RE.search(text))
        )
        if not looks or urlsplit(url).path.lower().endswith((".pdf", ".jpg", ".png", ".zip")):
            continue
        key = url.split("#")[0].rstrip("/")
        if key in seen or key == page_url.rstrip("/"):
            continue
        seen.add(key)
        # Prefer a company-owned careers path over a bare anchor-text match.
        rank = 0 if _CAREERS_PATH_RE.search(urlsplit(url).path) or _CAREERS_HOST_RE.match(domain) else 1
        scored.append((rank, order, url))
    return [u for _, _, u in sorted(scored)[:limit]]


def _base_domain(domain: str) -> str:
    return ".".join(domain.split(".")[-2:])


_INFO_TEXT_RE = re.compile(
    r"^(?:kontakt(?:\s+oss)?|contact(?:\s+us)?|om\s+oss|about(?:\s+us)?|om\s+\S+(?:\s+\S+)?|about\s+\S+(?:\s+\S+)?|selskapet|the\s+company)$",
    re.IGNORECASE,
)
_INFO_PATH_RE = re.compile(r"/(kontakt(?:-oss)?|contact(?:-us)?|om-oss|about(?:-us)?|selskapet)(?:/|$)", re.IGNORECASE)
MAX_INFO_LINKS = 2


def find_info_links(html: str, page_url: str, official_domain: str, limit: int = MAX_INFO_LINKS) -> list[str]:
    """Contact / about pages this page links to (same site), best first. These are where a
    company states its organisation number and address: the evidence for its website claim.
    Read from the page's own links instead of requesting four guessed paths on every site."""
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, (url, text) in enumerate(_anchors(html, page_url)):
        if not _same_site(_domain(url), official_domain):
            continue
        path = urlsplit(url).path
        by_path = bool(_INFO_PATH_RE.search(path)) and len([s for s in path.split("/") if s]) <= 2
        by_text = 0 < len(text) <= 30 and bool(_INFO_TEXT_RE.match(text))
        if not (by_path or by_text) or path.lower().endswith((".pdf", ".jpg", ".png", ".zip", ".xml")):
            continue
        key = url.split("#")[0].rstrip("/")
        if key in seen or key == page_url.rstrip("/"):
            continue
        seen.add(key)
        # A contact page before an about page: it is where the legal name and number sit.
        rank = 0 if re.search(r"kontakt|contact", path + " " + text, re.IGNORECASE) else 1
        scored.append((rank, order, url))
    return [u for _, _, u in sorted(scored)[:limit]]


def role_links(
    html: str, page_url: str, ats_domains: Optional[set[str]] = None
) -> list[tuple[str, str]]:
    """Distinct (title, url) pairs for individual job ads listed on the page.

    Only links that stay on the page's own site, go to a known applicant-tracking
    platform, or are an individual ad on a public job board (FINN, NAV's
    arbeidsplassen, Jobbnorge) count: the company's own careers page listing them is
    what ties them to the company. Footer and legal links never count.

    The title is the link's own text, or the heading inside the link when the link is a
    whole card (title plus teaser), or the heading just above it when the link only says
    "Read more" / "Les mer på finn.no"."""
    page_key = page_url.split("#")[0].rstrip("/")
    page_base = _base_domain(_domain(page_url))
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()
    roles: list[tuple[str, str]] = []
    for url, text, inner, cleaned, start in _anchor_items(html, page_url):
        key = url.split("#")[0].rstrip("/")
        if key == page_key or key in seen_urls:
            continue
        board_ad = bool(_JOB_BOARD_AD_RE.search(url))
        domain = _domain(url)
        if _base_domain(domain) != page_base and not (ats_domains and _on_any(domain, ats_domains)) and not board_ad:
            continue
        if not (_JOB_DETAIL_RE.search(url) or board_ad) or _NOT_A_ROLE_PATH_RE.search(url):
            continue
        title = _role_title(text, inner, cleaned, start)
        if title is None or _NAV_TEXT_RE.match(title) or _BOILERPLATE_TEXT_RE.search(title) or _AD_SECTION_RE.match(title):
            continue
        title_key = title.lower()
        if title_key in seen_titles:
            continue
        seen_titles.add(title_key)
        seen_urls.add(key)
        roles.append((title, url))
    return roles


# An individual ad on a public job board, linked from the company's own careers page.
_JOB_BOARD_AD_RE = re.compile(
    r"finn\.no/job/(?:[a-z]+/)?ad(?:\.html\?finnkode=|/)\d+"
    r"|arbeidsplassen\.nav\.no/stillinger/stilling/[0-9a-f-]{36}"
    r"|jobbnorge\.no/(?:ledige-stillinger/stilling|vacancies/job|en/available-jobs/job)/\d+",
    re.IGNORECASE,
)
# Link texts that point at an ad without naming it: the title is the heading above.
_GENERIC_LINK_RE = re.compile(
    r"^(les\s+mer|read\s+more|se\s+(stilling(en)?|annonsen?|mer)|mer\s+info(rmasjon)?|more\s+info|søk(\s+her|\s+nå)?|"
    r"apply(\s+(now|here))?|vis\s+stilling|til\s+annonsen|les\s+om(\s+stillingen)?)\b|finn\.no$",
    re.IGNORECASE,
)
_HEADING_INSIDE_RE = re.compile(r"<(h[1-6]|strong|b)\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_HEADING_BEFORE_RE = re.compile(r"<(h[1-6])\b[^>]*>(.*?)</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_SPLIT_RE = re.compile(r"<(?:br|/?p|/?div|/?span|/?h[1-6]|/?li)\b[^>]*>", re.IGNORECASE)
_LOOKBACK_CHARS = 1500
# Pages of a careers site that are not ads: staff profiles (karriere.dips.com/no/people/<id>-<name>
# was published as eight "roles"), departments, locations, posts.
_NOT_A_ROLE_PATH_RE = re.compile(r"/(people|employees|ansatte|team|departments|avdelinger|locations|lokasjoner|blog|posts|news|nyheter|stories|events)(/|$)", re.IGNORECASE)
# Section headings inside a job ad ("Vi kan tilby", "Arbeidsoppgaver"), never a role's title.
_AD_SECTION_RE = re.compile(
    r"^(vi\s+(kan\s+)?tilbyr?|vi\s+søker|arbeidsoppgaver|ansvar(sområder)?|kvalifikasjoner|ønskede\s+kvalifikasjoner|"
    r"personlige\s+egenskaper|om\s+(oss|stillingen|selskapet)|hvem\s+er\s+vi|søknad(sfrist)?|kontakt(person)?|"
    r"we\s+offer|responsibilities|qualifications|about\s+(us|the\s+role)|how\s+to\s+apply|om\s+arbeidsgiver(en)?|"
    r"(send\s+(en\s+|inn\s+)?)?åpen\s+søknad|open\s+application|vil\s+du\s+vite\s+mer|søk(e)?\s+på\s+stillingen)\b|:$",
    re.IGNORECASE,
)


def _anchor_items(html: str, base_url: str) -> list[tuple[str, str, str, str, int]]:
    """(absolute url, visible text, inner html, cleaned page html, anchor position)."""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html)
    out = []
    for match in _ANCHOR_RE.finditer(cleaned):
        attrs, inner = match.group(1), match.group(2)
        href = _HREF_ATTR_RE.search(attrs)
        if not href:
            continue
        raw = unescape(href.group(1).strip())
        if raw.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            out.append((urljoin(base_url, raw), _text(inner), inner, cleaned, match.start()))
        except ValueError:  # malformed href: skip it, don't fail the page
            continue
    return out


def _fits(title: str) -> bool:
    return _MIN_TITLE_LEN <= len(title) <= _MAX_TITLE_LEN


def _role_title(text: str, inner: str, cleaned: str, start: int) -> Optional[str]:
    heading = _HEADING_INSIDE_RE.search(inner)
    if heading and _fits(_text(heading.group(2))):
        return _text(heading.group(2))
    if not text or _GENERIC_LINK_RE.search(text):
        before = list(_HEADING_BEFORE_RE.finditer(cleaned[max(0, start - _LOOKBACK_CHARS):start]))
        if before and _fits(_text(before[-1].group(2))):
            return _text(before[-1].group(2))
        return None
    if _fits(text):
        return text
    for piece in _BLOCK_SPLIT_RE.split(inner):  # a whole card: its first line is the title
        first = _text(piece)
        if first:
            return first if _fits(first) else None
    return None


_TITLE_RE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_H1_RE = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_ERROR_PAGE_RE = re.compile(
    r"\b404\b|not\s+found|finner\s+ikke|finnes\s+ikke|page\s+(?:does\s+not|doesn.t)\s+exist|ikke\s+funnet",
    re.IGNORECASE,
)


def page_heading(html: str) -> Optional[str]:
    """The page's <title> text, else its first <h1>, exactly as written."""
    for pattern in (_TITLE_RE, _H1_RE):
        match = pattern.search(_SCRIPT_STYLE_RE.sub(" ", html))
        if match:
            text = _text(match.group(1))
            if text:
                return text
    return None


def careers_page_facts(
    html: str,
    page_url: str,
    extracted_at: datetime,
    context_name: Optional[str] = None,
    ats_domains: Optional[set[str]] = None,
) -> list[RawFact]:
    """A hiring_signal RawFact for a careers page, in two honest tiers.

    Tier A -- the page lists at least `MIN_ROLE_LINKS` distinct job-detail links
    with titles: "Careers page lists N open roles, e.g. "<title>": <url>", with
    that title quoted as the span.

    Tier B -- the page is plainly a careers page (a careers URL or applicant-
    tracking host whose own title or heading says so) but its listings are not
    in the static HTML, which is common (listings loaded by script): the claim
    is only that the careers page exists -- "Careers page: <url>" -- and never
    that a role is open. The span is the page's own title.

    Either way nothing is published when the page says there are no openings, is
    an error page, or carries no readable title."""
    from src.pipeline.extract import RawFact  # deferred: see the TYPE_CHECKING note above

    visible = _text(_SCRIPT_STYLE_RE.sub(" ", html))
    if _NO_OPENINGS_RE.search(visible):
        return []
    heading = page_heading(html)
    if heading is None or _ERROR_PAGE_RE.search(heading):
        return []

    roles = role_links(html, page_url, ats_domains)
    # One ad on a job board or applicant-tracking platform, listed by the company's own careers
    # page, is as clear as two on its own site.
    off_site = [r for r in roles if _base_domain(_domain(r[1])) != _base_domain(_domain(page_url))]
    if len(roles) >= MIN_ROLE_LINKS or off_site:
        page = clean_page_url(page_url)
        # Spans are one text node as the page writes it: a card's title read across several
        # elements is not in the source as one string (official C300 feedback on receipts).
        from src.pipeline.spans import span_for_text

        facts = [RawFact(
            "hiring_signal", f"{CAREERS_VALUE_PREFIX} lists {len(roles)} open role{'s' if len(roles) != 1 else ''}: {page}",
            page_url, "text", extracted_at, context_name=context_name,
            evidence_span=span_for_text(html, roles[0][0]) or roles[0][0],
        )]
        # Each role on its own: title and direct link, sourced to the company's careers page
        # that lists it (the company match).
        for title, url in roles[:MAX_ROLE_CLAIMS]:
            facts.append(RawFact(
                "hiring_signal", f"{ROLE_VALUE_PREFIX}: {title} - {clean_page_url(url)}", page_url, "text", extracted_at,
                context_name=context_name, evidence_span=span_for_text(html, title) or title,
            ))
        return facts

    # A single job ad is not "a careers page". With its own JobPosting data the role is
    # published from that (title and posting date); without it, the claim says what it is.
    if _JOB_POSTING_RE.search(html):
        return []
    on_ats = bool(ats_domains and _on_any(_domain(page_url), ats_domains))
    if is_careers_url(page_url, ats_domains) and (on_ats or CAREERS_HEADING_RE.search(heading)):
        prefix = JOB_AD_VALUE_PREFIX if _JOB_DETAIL_RE.search(page_url) else CAREERS_VALUE_PREFIX
        return [RawFact(
            "hiring_signal", f"{prefix}: {clean_page_url(page_url)}", page_url, "text", extracted_at,
            context_name=context_name, evidence_span=heading,
        )]
    return []


_JOB_POSTING_RE = re.compile(r'"@type"\s*:\s*"JobPosting"', re.IGNORECASE)
# Query parameters that only track a visit; a parameter that names the page (?id=42) stays.
_TRACKING_PARAM_RE = re.compile(r"^(utm_\w+|gclid|fbclid|mc_\w+|ref|source|src|trk|link_source\w*)$", re.IGNORECASE)
_LANGUAGE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:[-_][a-z]{2})?$", re.IGNORECASE)


def clean_page_url(url: str) -> str:
    """`url` without its fragment, tracking parameters or bare flags (`?JobPosting`)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    params = [p for p in parts.query.split("&") if p and "=" in p and not _TRACKING_PARAM_RE.match(p.split("=", 1)[0])]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "&".join(params), ""))


def careers_page_key(url: str) -> str:
    """The careers page regardless of language copy: /da/career/vacancies/ and
    /career/vacancies are one page. Host without www, path without a leading language
    segment or trailing slash, lower case."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    segments = [s for s in parts.path.split("/") if s]
    if segments and _LANGUAGE_SEGMENT_RE.match(segments[0]) and len(segments) > 1:
        segments = segments[1:]
    return parts.netloc.lower().removeprefix("www.") + "/" + "/".join(segments).lower()
