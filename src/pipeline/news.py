"""
Dated news from a company's own site.

Dated public activity (news, press releases, blog posts) is one of the four
external field families, and it comes from the company's own pages. Two gaps held
it back:

  * The old line scan only recognised ISO dates (2024-05-12). Real Norwegian
    sites write "12. mai 2024" or "12.05.2024", or keep the date in a
    <time datetime="..."> attribute that is not visible text at all.
  * A date alone is not an item. A bare "12. mai 2024" says nothing; the headline
    next to it is the fact.

`news_item_facts` pairs each date with the headline in the same article/list item
and publishes "Headline (YYYY-MM-DD)" with the headline quoted as the span and
the ISO date as the effective date. `find_news_links` reads the links a page
already contains (never guessed paths) to reach the site's news page.

Pure functions over HTML strings, no network.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Optional
from urllib.parse import urlsplit

from src.pipeline.careers import _anchors, _domain, _same_site, _text

if TYPE_CHECKING:  # extract.py imports crawl.py, which imports this module
    from src.pipeline.extract import RawFact

_MONTHS = {
    "januar": 1, "january": 1, "jan": 1, "februar": 2, "february": 2, "feb": 2, "mars": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "mai": 5, "may": 5, "juni": 6, "june": 6, "jun": 6, "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11, "desember": 12, "december": 12, "des": 12, "dec": 12,
}
_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_ISO_RE = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DMY_RE = re.compile(r"(?<!\d)(\d{1,2})\.\s?(\d{1,2})\.\s?(\d{4})(?!\d)")
_D_MONTH_Y_RE = re.compile(rf"(?<!\d)(\d{{1,2}})\.?\s+({_MONTH_RE})\.?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE)
_MONTH_D_Y_RE = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?!\d)", re.IGNORECASE)
_TIME_TAG_RE = re.compile(r"<time\b[^>]*\bdatetime\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
# An item starts at an <article>, an <li>, or a <div> whose class says it is a post / news
# item / card / teaser: many sites lay their news out as card grids of plain divs.
_BLOCK_START_RE = re.compile(
    r"""<(?:article|li)\b|<div\b[^>]*\bclass\s*=\s*["'][^"']*\b(?:news|nyhet\w*|post|article|card|teaser|entry|story|blog)\b[^"']*["']""",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"<h[1-4]\b[^>]*>(.*?)</h[1-4]>", re.IGNORECASE | re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)

MAX_NEWS_ITEMS_PER_PAGE = 10
MAX_NEWS_PAGES = 2
_MIN_TITLE_LEN = 8
_MAX_TITLE_LEN = 200
_FIRST_PLAUSIBLE_YEAR = 2000
_MAX_DAYS_AHEAD = 30


def _valid(year: int, month: int, day: int, today: date) -> Optional[str]:
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    # No dates from long ago that are really copyright years or sample text, and
    # none more than a month ahead: an event teaser is not news.
    if parsed.year < _FIRST_PLAUSIBLE_YEAR or (parsed - today).days > _MAX_DAYS_AHEAD:
        return None
    return parsed.isoformat()


def parse_date(text: str, today: Optional[date] = None) -> Optional[str]:
    """The first date in `text` as YYYY-MM-DD, from ISO, dd.mm.yyyy, "12. mai 2024"
    or "May 12, 2024" forms (Norwegian and English month names), else None."""
    today = today or datetime.now().date()
    candidates: list[tuple[int, Optional[str]]] = []
    for m in _ISO_RE.finditer(text):
        candidates.append((m.start(), _valid(int(m[1]), int(m[2]), int(m[3]), today)))
    for m in _DMY_RE.finditer(text):
        candidates.append((m.start(), _valid(int(m[3]), int(m[2]), int(m[1]), today)))
    for m in _D_MONTH_Y_RE.finditer(text):
        candidates.append((m.start(), _valid(int(m[3]), _MONTHS[m[2].lower()], int(m[1]), today)))
    for m in _MONTH_D_Y_RE.finditer(text):
        candidates.append((m.start(), _valid(int(m[3]), _MONTHS[m[1].lower()], int(m[2]), today)))
    for _, iso in sorted(candidates, key=lambda c: c[0]):
        if iso:
            return iso
    return None


_NEWS_TEXT_RE = re.compile(
    r"^(?:news|nyheter|aktuelt|siste\s+nytt|nytt|presse|press|pressemeldinger|press\s+releases?|newsroom|media|"
    r"blogg?|nyhetsarkiv|nyhetsrom|aktuelle\s+saker|artikler|insights|innsikt|fagartikler)$",
    re.IGNORECASE,
)
_NEWS_PATH_RE = re.compile(
    r"/(?:news|nyheter|aktuelt|presse|pressemeldinger|newsroom|blogg?|nyhetsarkiv|artikler|insights)(?:/|$)", re.IGNORECASE
)


def find_news_links(html: str, page_url: str, official_domain: str, limit: int = MAX_NEWS_PAGES) -> list[str]:
    """News-page URLs this page links to, same site only, best first (a news path
    before a bare anchor-text match). Never guesses a path."""
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for order, (url, text) in enumerate(_anchors(html, page_url)):
        if not _same_site(_domain(url), official_domain):
            continue
        path = urlsplit(url).path
        by_path = bool(_NEWS_PATH_RE.search(path))
        by_text = 0 < len(text) <= 30 and bool(_NEWS_TEXT_RE.match(text))
        if not (by_path or by_text) or path.lower().endswith((".pdf", ".jpg", ".png", ".zip", ".xml")):
            continue
        # A single article lives deeper than the listing (/nyheter/2024/some-story): skip it.
        segments = [s for s in path.split("/") if s]
        if by_path and len(segments) > 2:
            continue
        key = url.split("#")[0].rstrip("/")
        if key in seen or key == page_url.rstrip("/"):
            continue
        seen.add(key)
        scored.append((0 if by_path else 1, order, url))
    return [u for _, _, u in sorted(scored)[:limit]]


_SECTION_LABEL_RE = re.compile(
    r"^(press\s*room|presserom|press\s+releases?|pressemeldinger?|news(room)?|nyheter|aktuelt|blogg?|"
    r"share\s+and\s+analyst\s+information|investor\s+relations|read\s+more|les\s+mer|se\s+alle|view\s+all|all\s+news|"
    r"latest\s+news|siste\s+nytt|financial\s+(calendar|reports?)|reports?|downloads?|events?|arrangementer)$",
    re.IGNORECASE,
)
_ENTITY_RE = re.compile(r"&#?\w+;")


def is_headline(title: str) -> bool:
    """A headline says something: not a section label ("Press room"), and not a string of
    dates and punctuation ("2026-10-12 &ndash; 2026-10-14")."""
    if _SECTION_LABEL_RE.match(title.strip()):
        return False
    letters = re.sub(r"[^A-Za-zÆØÅæøåÄÖäöÜü]", "", _ENTITY_RE.sub(" ", title))
    return len(letters) >= 8


def _blocks(html: str) -> list[str]:
    """The page cut into article / list-item chunks."""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html)
    starts = [m.start() for m in _BLOCK_START_RE.finditer(cleaned)]
    return [cleaned[a:b] for a, b in zip(starts, starts[1:] + [len(cleaned)])]


def _title(block: str) -> Optional[str]:
    for match in _HEADING_RE.finditer(block):
        text = _text(match.group(1))
        if _MIN_TITLE_LEN <= len(text) <= _MAX_TITLE_LEN:
            return text
    for _, text in _anchors(block, "https://x.invalid/"):
        if _MIN_TITLE_LEN <= len(text) <= _MAX_TITLE_LEN and not parse_date(text):
            return text
    return None


def news_items(html: str, today: Optional[date] = None) -> list[tuple[str, str]]:
    """(headline, ISO date) for each dated article or list item on the page."""
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for block in _blocks(html):
        iso = None
        time_tag = _TIME_TAG_RE.search(block)
        if time_tag:
            iso = parse_date(time_tag.group(1), today)
        if iso is None:
            iso = parse_date(_text(block), today)
        title = _title(block) if iso else None
        if not iso or not title or title.lower() in seen or parse_date(title):
            continue
        if not is_headline(title) or iso == (today or datetime.now().date()).isoformat():
            continue
        seen.add(title.lower())
        items.append((title, iso))
        if len(items) >= MAX_NEWS_ITEMS_PER_PAGE:
            break
    if not items:
        today_iso = (today or datetime.now().date()).isoformat()
        items = [(t, d) for t, d in _time_tag_items(html, today) if is_headline(t) and d != today_iso][:MAX_NEWS_ITEMS_PER_PAGE]
    return items


_NEARBY_HEADING_RE = re.compile(r"<(?:h[1-4]|a)\b[^>]*>(.*?)</(?:h[1-4]|a)>", re.IGNORECASE | re.DOTALL)
_NEARBY_CHARS = 400


def _time_tag_items(html: str, today: Optional[date]) -> list[tuple[str, str]]:
    """Fallback for layouts with no article/list/card wrapper: each <time datetime> is paired
    with the nearest headline or link text right after it (or just before it)."""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", html)
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _TIME_TAG_RE.finditer(cleaned):
        iso = parse_date(match.group(1), today)
        if not iso:
            continue
        title = None
        for window in (cleaned[match.end() : match.end() + _NEARBY_CHARS], cleaned[max(0, match.start() - _NEARBY_CHARS) : match.start()]):
            titles = [_text(m.group(1)) for m in _NEARBY_HEADING_RE.finditer(window)]
            title = next((t for t in titles if _MIN_TITLE_LEN <= len(t) <= _MAX_TITLE_LEN and not parse_date(t)), None)
            if title:
                break
        if not title or title.lower() in seen:
            continue
        seen.add(title.lower())
        items.append((title, iso))
    return items


# --- WordPress: a structured post list, one request, no layout to guess ---------------------

_WORDPRESS_RE = re.compile(r"/wp-content/|/wp-includes/|<meta[^>]+generator[^>]+WordPress", re.IGNORECASE)
WP_POSTS_PATH = "/wp-json/wp/v2/posts?per_page=6&_fields=date,title,link"


def is_wordpress(html: str) -> bool:
    return bool(_WORDPRESS_RE.search(html))


def wp_posts_url(page_url: str) -> str:
    parts = urlsplit(page_url)
    return f"{parts.scheme}://{parts.netloc}{WP_POSTS_PATH}"


def wp_post_facts(
    body: str, page_url: str, extracted_at: datetime, context_name: Optional[str] = None
) -> list["RawFact"]:
    """dated_activity from WordPress's own REST post list ("Title (YYYY-MM-DD)"). The title is
    quoted as the JSON writes it; HTML entities in it are decoded for the published value."""
    import json
    from html import unescape

    from src.pipeline.extract import RawFact  # deferred: see the TYPE_CHECKING note above

    try:
        posts = json.loads(body)
    except ValueError:
        return []
    if not isinstance(posts, list):
        return []
    facts: list[RawFact] = []
    for post in posts[:MAX_NEWS_ITEMS_PER_PAGE]:
        if not isinstance(post, dict) or not isinstance(post.get("title"), dict):
            continue
        raw_title = post["title"].get("rendered")
        iso = parse_date(str(post.get("date") or ""), extracted_at.date())
        if not isinstance(raw_title, str) or not iso:
            continue
        title = _text(unescape(raw_title))
        if not is_headline(title) or iso == extracted_at.date().isoformat():
            continue
        facts.append(
            RawFact(
                "dated_activity", f"{title} ({iso})", page_url, "structured", extracted_at,
                context_name=context_name, evidence_span=raw_title, effective_date=iso,
            )
        )
    return facts


def news_item_facts(
    html: str, page_url: str, extracted_at: datetime, context_name: Optional[str] = None
) -> list["RawFact"]:
    """dated_activity RawFacts, one per dated headline: "Headline (YYYY-MM-DD)",
    the headline quoted as the span and the date as the effective date."""
    from src.pipeline.extract import RawFact  # deferred: see the TYPE_CHECKING note above

    return [
        RawFact(
            "dated_activity", f"{title} ({iso})", page_url, "text", extracted_at,
            context_name=context_name, evidence_span=title, effective_date=iso,
        )
        for title, iso in news_items(html, extracted_at.date())
    ]
