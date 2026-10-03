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
        "marketplace", "gaming", "stories", "reel", "reels", "video", "videos", "public", "people", "l.php", "flx", "ads",
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
        if first == "p" and len(segments) >= 2:  # Facebook's page URL form /p/<name>-<id>
            return f"https://www.facebook.com/p/{segments[1]}"
        if first in _RESERVED["facebook.com"] or not _HANDLE_RE.match(segments[0]):
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
    if "profile.php" in parts.path:
        link = key  # the account id lives in the query string
    return link, key
