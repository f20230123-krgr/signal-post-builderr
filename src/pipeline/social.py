"""
Company-owned social profile URLs, in one canonical form.

A company's footer links to its own Facebook page, LinkedIn company page,
Instagram account and so on. The same footer also links, or embeds, things that
are NOT the company's profile and must never be published as one: a single
Instagram post (/p/...), a staff member's personal LinkedIn (/in/...), a share
button, a video, a tracking-laden copy of the real link. Official feedback was
explicit: reject post URLs, publish only links confirmed in the saved source.

`canonical_social_profile_url` returns the profile URL in a clean form (host
without country/mobile prefixes, no query string or fragment, no trailing
sub-page) or None when the link is not a company profile. De-duplicating on the
canonical form also collapses `no.linkedin.com/company/x?trk=...` and
`linkedin.com/company/x/` into one claim.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import unquote, urlsplit

_PLATFORMS = ("linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com", "tiktok.com")
# Path segments that are features of a platform, not an account name.
_RESERVED = {
    "facebook.com": {
        "sharer", "sharer.php", "share", "share.php", "dialog", "plugins", "tr", "events", "groups", "watch", "hashtag",
        "photo", "photos", "photo.php", "permalink.php", "story.php", "login", "login.php", "policies", "help", "home",
        "marketplace", "gaming", "stories", "reel", "reels", "video", "videos", "public", "l.php", "flx", "ads",
        "privacy", "terms", "cookies", "legal", "settings", "business", "about", "recover", "checkpoint", "r.php",
    },
    "instagram.com": {"p", "reel", "reels", "explore", "stories", "tv", "accounts", "direct", "about", "legal", "web"},
    "twitter.com": {"intent", "share", "home", "search", "hashtag", "i", "explore", "login", "settings", "privacy", "tos", "widgets.js"},
    "x.com": {"intent", "share", "home", "search", "hashtag", "i", "explore", "login", "settings", "privacy", "tos"},
    "tiktok.com": {"discover", "tag", "video", "explore", "foryou", "login", "legal", "about"},
}
_HANDLE_RE = re.compile(r"^[\w.\-%]{2,100}$")


def _platform(host: str) -> Optional[str]:
    host = host.lower().split(":", 1)[0]
    return next((p for p in _PLATFORMS if host == p or host.endswith("." + p)), None)


def canonical_social_profile_url(url: str) -> Optional[str]:
    """The company-profile URL in canonical form, or None when `url` is not one."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:  # malformed link (unclosed IPv6 bracket)
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    platform = _platform(parts.netloc)
    if platform is None:
        return None
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if not segments:
        return None
    first = segments[0].lower()

    if platform == "linkedin.com":
        # Only a company (or school/showcase) page. /in/<person> is a personal profile.
        if first in ("company", "school", "showcase") and len(segments) >= 2 and _HANDLE_RE.match(segments[1]):
            return f"https://www.linkedin.com/{first}/{segments[1]}"
        return None

    if platform == "facebook.com":
        if first == "profile.php":
            ident = re.search(r"(?:^|&)id=(\d{5,})", parts.query)
            return f"https://www.facebook.com/profile.php?id={ident.group(1)}" if ident else None
        if first == "pages" and len(segments) >= 3:  # /pages/<name>/<id>
            return f"https://www.facebook.com/pages/{segments[1]}/{segments[2]}"
        if first == "people" and len(segments) >= 3 and segments[2].isdigit():  # /people/<name>/<id>
            return f"https://www.facebook.com/people/{segments[1]}/{segments[2]}"
        if first == "p" and len(segments) >= 2:  # Facebook's page URL form /p/<name>-<id>
            return f"https://www.facebook.com/p/{segments[1]}"
        if first == "pg" and len(segments) >= 2 and _HANDLE_RE.match(segments[1]):  # old form /pg/<name>/about
            return f"https://www.facebook.com/{segments[1]}"
        if first in _RESERVED["facebook.com"] or not _HANDLE_RE.match(segments[0]):
            return None
        # A bare number or "<page>_<post>" is a post or photo from an embedded feed (seven of
        # them on one company's homepage), not the page; a page by id is /profile.php?id=.
        if re.fullmatch(r"[\d_]+", segments[0]):
            return None
        return f"https://www.facebook.com/{segments[0]}"

    if platform == "instagram.com":
        if first in _RESERVED["instagram.com"] or not _HANDLE_RE.match(segments[0]):
            return None
        return f"https://www.instagram.com/{segments[0]}/"

    if platform in ("twitter.com", "x.com"):
        if first in _RESERVED[platform] or not _HANDLE_RE.match(segments[0]):
            return None
        return f"https://{platform}/{segments[0]}"

    if platform == "tiktok.com":
        if first.startswith("@") and len(first) > 2:
            return f"https://www.tiktok.com/{segments[0]}"
        return None

    if platform == "youtube.com":
        if first in ("channel", "c", "user") and len(segments) >= 2:
            return f"https://www.youtube.com/{first}/{segments[1]}"
        if first.startswith("@") and len(first) > 2:
            return f"https://www.youtube.com/{segments[0]}"
        return None  # /watch, /embed, /playlist, /shorts: a video, not a channel

    return None


def published_form(canonical: str) -> str:
    """The profile written the way Builderr's reference data writes it: https, the platform's
    bare host (no www., country or mobile prefix; twitter.com as x.com) and no trailing slash
    ("https://facebook.com/afgruppen"). Only 2 of our 90 links on Builderr's 100-company sample
    matched its links letter for letter while 65 were the same profile written another way."""
    parts = urlsplit(canonical)
    host = parts.netloc.lower().removeprefix("www.")
    if host == "twitter.com":
        host = "x.com"
    query = f"?{parts.query}" if parts.query else ""
    return f"https://{host}{parts.path.rstrip('/')}{query}"


def social_profile_link(url: str) -> Optional[tuple[str, str]]:
    """(link to publish, de-duplication key) for a company profile, or None.

    The key is the canonical form, so the same profile linked three ways is one claim; the
    link is that profile in Builderr's own spelling (published_form). The evidence keeps the
    link exactly as the page writes it."""
    # A link read from a feed's text can carry the CDATA close ("/steinerskolenioslo/]]").
    url = re.sub(r"[\]\[)>'\"]+$", "", url.strip())
    key = canonical_social_profile_url(url)
    if key is None:
        return None
    return published_form(key), key


# --- profiles that live in a page's script data rather than in an <a> tag ------------------
#
# Sites that draw their footer with JavaScript usually still ship the profile addresses as
# data in the page ({"facebook":"https:\/\/www.facebook.com\/acme"} inside a framework's
# state blob). Anchors are trusted because the company chose to link them; a URL found in
# script text could just as well belong to a partner or a widget, so it is accepted only
# when its account name resembles the company or its domain.

_SCRIPT_SOCIAL_RE = re.compile(
    r"""https?:(?:\\?/){2}(?:[\w-]+\.)*(?:linkedin|facebook|instagram|twitter|x|youtube|tiktok)\.com(?:\\?/)(?:[^\s"'<>)\\]|\\/)+""",
    re.IGNORECASE,
)
_GENERIC_NAME_TOKENS = {
    "holding", "gruppen", "group", "norge", "norway", "invest", "eiendom", "service", "services", "partner", "partners",
    "company", "limited", "scandinavia", "nordic", "international", "solutions", "systems",
}
_LEGAL_FORM_TOKENS = {"as", "asa", "ans", "da", "sa", "enk", "nuf", "ba", "ks", "iks"}


def identity_tokens(legal_name: str, site_url: str) -> set[str]:
    """Lower-case, letters-and-digits-only forms an account name for this company may take:
    its distinctive name words, the whole name joined, and its domain label."""
    words = [re.sub(r"[^a-z0-9æøå]", "", w) for w in legal_name.lower().split()]
    words = [w for w in words if w and w not in _LEGAL_FORM_TOKENS]
    tokens = {w for w in words if len(w) >= 5 and w not in _GENERIC_NAME_TOKENS}
    if words:
        tokens.add("".join(words))
    host = urlsplit(site_url if "://" in site_url else f"https://{site_url}").netloc.lower().removeprefix("www.")
    label = host.split(".")[0] if host else ""
    if len(label) >= 4:
        tokens.add(re.sub(r"[^a-z0-9æøå]", "", label))
    return {t for t in tokens if len(t) >= 4}


def _account_name(canonical: str) -> str:
    segments = [s for s in urlsplit(canonical).path.split("/") if s]
    if not segments:
        return ""
    name = segments[1] if segments[0].lower() in ("company", "school", "showcase", "channel", "c", "user", "pages", "p", "people") and len(segments) > 1 else segments[0]
    return re.sub(r"[^a-z0-9æøå]", "", name.lower().lstrip("@"))


def resembles_company(canonical: str, tokens: set[str]) -> bool:
    name = _account_name(canonical)
    return len(name) >= 4 and any(t in name or name in t for t in tokens)


_ID_LIKE_RE = re.compile(r"^(?:uc[\w-]{15,}|\d+|profilephpid\d+)$")


_FOREIGN_FORM_RE = re.compile(
    r"(?:^|[\W_])(?:ltd|limited|gmbh|ab|oy|oyj|aps|a[-_.]s|inc|llc|bv|plc|srl|sarl|spa|ag|corp|pty)(?:$|[\W_])", re.IGNORECASE
)


# A group or parent's account ("autoin-gruppen", "strommengroup") linked from a member company's
# site is the group's, unless the company is itself the group.
_GROUP_WORD_RE = re.compile(r"gruppen|group|konsern|holding", re.IGNORECASE)


_LEGAL_FORM_WORDS = {"as", "asa", "sa", "ans", "da", "ba", "enk", "nuf", "norge", "norway"}


def _is_the_group(account: str, legal_name: str) -> bool:
    """The company is the group itself: its name is exactly the brand before the group word
    (KITRON ASA -> "kitrongroup", CONSTO AS -> "constokonsern"), not the brand plus more
    ("AUTOIN BILSKADE AS" -> "autoin-gruppen" is the group's, not this company's)."""
    words = [w for w in re.sub(r"[^a-z0-9æøå ]", " ", _fold(legal_name)).split() if w not in _LEGAL_FORM_WORDS]
    brand = _GROUP_WORD_RE.sub("", _fold(account))
    return bool(words) and brand == "".join(words)


def _fold(text: str) -> str:
    return text.lower().translate(str.maketrans({"æ": "ae", "ø": "o", "å": "a"}))


def _branch_town(account: str, legal_name: str, site_url: str, home: str) -> Optional[str]:
    """The town a branch account is named after ("uniteperformancelarvik" -> "larvik"), when
    that town is not part of the company's own name, address or domain."""
    from src.pipeline.places import MUNICIPALITIES

    own = _fold(f"{legal_name} {site_url} {home}")
    folded = _fold(account)
    for town in MUNICIPALITIES:
        plain = _fold(town)
        if folded.endswith(plain) and len(folded) > len(plain) + 3 and plain not in re.sub(r"[^a-z0-9]", "", own):
            return town
    return None


def plausibly_own_profile(canonical: str, legal_name: str, site_url: str, home: str = "") -> bool:
    """Whether a profile a company's site links can be published as the company's OWN.

    A site also links its parent group's accounts, an owner's personal brand, a supplier's
    channel embedded in an article. Hand-checked on Builderr's 100-company sample: requiring
    a named account to resemble the company's name or domain removed every such link and
    none of the 76 profiles Builderr's own crawl confirmed. An account known only by an id
    (a YouTube channel id, a numeric page id) cannot be compared by name and is kept."""
    account = _account_name(canonical)
    if not account or _ID_LIKE_RE.match(account) or "profile.php" in canonical:
        return True
    # An account named with another country's company form ("epsco-ltd" linked from EPSCO
    # NORGE AS's site) is the foreign parent or sister company's, not this Norwegian one's
    # (official audit, 23 Oct: social accounts belonging to other companies).
    segments = [s for s in urlsplit(canonical).path.split("/") if s]
    raw_account = segments[-1] if segments else ""
    if _FOREIGN_FORM_RE.search(raw_account) and not _FOREIGN_FORM_RE.search(legal_name):
        return False
    if _GROUP_WORD_RE.search(account) and not _GROUP_WORD_RE.search(legal_name) and not _is_the_group(account, legal_name):
        return False
    # A chain's branch ("uniteperformancelarvik", NKL's "...Trondheim" page) is not the company.
    if _branch_town(account, legal_name, site_url, home):
        return False
    if resembles_company(canonical, identity_tokens(legal_name, site_url)):
        return True
    # A short domain label ("nsp" for nsp.no) is too short to be a token on its own, but an
    # account that starts with it ("nspnorge") is plainly the same company.
    host = urlsplit(site_url if "://" in site_url else f"https://{site_url}").netloc.lower().removeprefix("www.")
    label = re.sub(r"[^a-z0-9æøå]", "", host.split(".")[0]) if host else ""
    return len(label) >= 3 and account.startswith(label)


def script_social_candidates(html: str, tokens: set[str]) -> list[tuple[str, str, str]]:
    """(link to publish, de-duplication key, the text as found) for each company profile
    address in the page's raw text whose account name resembles the company."""
    found: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for match in _SCRIPT_SOCIAL_RE.finditer(html):
        raw = match.group(0)
        link = raw.replace("\\/", "/")
        result = social_profile_link(link)
        if result is None or result[1] in seen or not resembles_company(result[1], tokens):
            continue
        seen.add(result[1])
        found.append((result[0], result[1], raw))
    return found
