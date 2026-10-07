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


def social_profile_link(url: str) -> Optional[tuple[str, str]]:
    """(link to publish, de-duplication key) for a company profile, or None.

    The link keeps the form the company itself uses (path as written, so a
    trailing slash or a country subdomain survives) minus the query string and
    fragment, which only carry tracking. The key is the canonical form, so the
    same profile linked three ways is one claim."""
    key = canonical_social_profile_url(url)
    if key is None:
        return None
    parts = urlsplit(url.strip())
    link = f"{parts.scheme.lower()}://{parts.netloc.lower()}{parts.path}"
    if (
        "profile.php" in parts.path
        or parts.path.lower().startswith("/pg/")
        or (key.startswith("https://www.youtube.com/") and parts.path.rstrip("/") != urlsplit(key).path)
    ):
        # The account id lives in the query string; /pg/<name>/about and a YouTube channel's
        # /featured or /playlists are tabs of the account itself.
        link = key
    elif "linkedin.com" in parts.netloc.lower():
        # The company page as the site writes it, never one of its tabs (/about, /posts,
        # /mycompany, /admin/...): the first two path segments, and a trailing slash only
        # if the original had one after the name (or went on to a tab).
        raw = [seg for seg in parts.path.split("/") if seg]
        slash = "/" if len(raw) > 2 or parts.path.endswith("/") else ""
        link = f"{parts.scheme.lower()}://{parts.netloc.lower()}/{raw[0]}/{raw[1]}{slash}"
    return link, key


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


def plausibly_own_profile(canonical: str, legal_name: str, site_url: str) -> bool:
    """Whether a profile a company's site links can be published as the company's OWN.

    A site also links its parent group's accounts, an owner's personal brand, a supplier's
    channel embedded in an article. Hand-checked on Builderr's 100-company sample: requiring
    a named account to resemble the company's name or domain removed every such link and
    none of the 76 profiles Builderr's own crawl confirmed. An account known only by an id
    (a YouTube channel id, a numeric page id) cannot be compared by name and is kept."""
    account = _account_name(canonical)
    if not account or _ID_LIKE_RE.match(account) or "profile.php" in canonical:
        return True
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
