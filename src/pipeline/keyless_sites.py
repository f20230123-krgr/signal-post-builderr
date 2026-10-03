"""
Website candidates that need no search key.

All four external field families (website, social profiles, dated news, hiring)
start from the company's own site, so a company with no registered website is
thin everywhere. The paid search providers (Exa, Parallel) are optional and the
official scoring run cannot be assumed to have them, so this module proposes
candidates from sources every run has:

  1. NAV's job ad for the company itself, whose employer record can carry the
     company's homepage (exact organisation number only -- never a sub-unit's).
  2. The e-mail address the company registered in Brønnøysund, when its domain is
     the company's own and not a mailbox provider (gmail.com, online.no, ...).
  3. A few domains derived from the company's name (examplename.no), tried only
     when the name resolves in DNS.

A candidate is NEVER evidence on its own. Each goes through exactly the same
`verify_discovered_site` identity gate as a search hit: the page must publish the
company's organisation number, or carry its name, or (new here) show the phone
number or street address the company registered. A candidate that fails is
dropped, so a wrong guess costs a request, not a wrong-company publication.
"""
from __future__ import annotations

import re
import socket
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

# Mailbox providers and ISPs: an address there says nothing about the company's
# own domain.
GENERIC_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "hotmail.com", "hotmail.no", "outlook.com", "outlook.no", "live.com", "live.no",
    "icloud.com", "me.com", "mac.com", "yahoo.com", "yahoo.no", "online.no", "frisurf.no", "broadpark.no",
    "getmail.no", "start.no", "c2i.net", "lyse.net", "tele2.no", "telenor.com", "telenor.no", "msn.com",
    "protonmail.com", "proton.me", "bbhosted.com", "altibox.no", "webmail.no", "chello.no", "combitel.no",
    "kolumbus.no", "sensewave.com", "bredband.no", "hotmail.co.uk", "mail.com", "aol.com", "powertech.no",
    "hotmail.se", "hotmail.dk", "yahoo.se", "gmx.com", "gmx.net", "mail.ru", "yandex.com", "no.pwc.com",
}

_LEGAL_SUFFIXES = {"as", "asa", "ans", "da", "sa", "ks", "nuf", "enk", "ba", "sf", "iks", "stiftelse", "a/s"}
_GENERIC_NAME_WORDS = {"norge", "norway", "holding", "invest", "eiendom", "group", "gruppen", "and", "og", "the"}
_PHONE_ON_PAGE_RE = re.compile(r"(?<!\d)(?:\+?47[ .\-]?)?((?:\d[ .\-]?){7}\d)(?!\d)")
MAX_NAME_CANDIDATES = 3
MAX_CANDIDATES_VERIFIED = 3


@dataclass
class SiteHints:
    """What the registry record says about how to reach the company, used both to
    propose a candidate (email) and to corroborate one (phone, street, postcode)."""

    email: Optional[str] = None
    phones: list[str] = field(default_factory=list)
    street: Optional[str] = None
    postcode: Optional[str] = None
    poststed: Optional[str] = None
    nav_homepages: list[str] = field(default_factory=list)


def hints_from_registry_record(body: dict) -> SiteHints:
    """SiteHints from a Brønnøysund `enheter` record."""
    hints = SiteHints()
    email = body.get("epostadresse")
    if isinstance(email, str) and "@" in email:
        hints.email = email.strip().lower()
    for key in ("telefon", "mobil"):
        value = body.get(key)
        if isinstance(value, str) and len(re.sub(r"\D", "", value)) >= 8:
            hints.phones.append(re.sub(r"\D", "", value)[-8:])
    address = body.get("forretningsadresse") or body.get("postadresse") or {}
    if isinstance(address, dict):
        lines = address.get("adresse")
        if isinstance(lines, list) and lines and isinstance(lines[0], str):
            hints.street = lines[0].strip() or None
        postcode = address.get("postnummer")
        if isinstance(postcode, str) and re.fullmatch(r"\d{4}", postcode.strip()):
            hints.postcode = postcode.strip()
        poststed = address.get("poststed")
        if isinstance(poststed, str) and poststed.strip():
            hints.poststed = poststed.strip()
    return hints


def email_domain_candidate(email: Optional[str]) -> Optional[str]:
    """`https://<domain>` for a registered e-mail on the company's own domain."""
    if not email or "@" not in email:
        return None
    domain = email.rsplit("@", 1)[1].strip().lower().strip(".")
    if not domain or "." not in domain or domain in GENERIC_EMAIL_DOMAINS:
        return None
    parts = domain.split(".")
    if len(parts) > 2 and parts[0] in {"mail", "post", "smtp", "epost", "mx"}:
        domain = ".".join(parts[1:])
    return f"https://{domain}"


def _fold(text: str) -> str:
    text = text.replace("æ", "ae").replace("ø", "o").replace("å", "a")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9-]", "", text)


def name_domain_candidates(legal_name: str, limit: int = MAX_NAME_CANDIDATES) -> list[str]:
    """Domains a company named `legal_name` might plausibly own, best first.

    The joined name ("sandneselektriske.no"), the hyphenated name, and the first
    word when it is distinctive enough to stand alone. .no first, then .com. These
    are guesses to be checked in DNS and then verified on the page, never trusted."""
    cleaned = re.sub(r"[.,!?'\"()&+/]", " ", legal_name.lower())
    words = [w for w in cleaned.split() if w and w not in _LEGAL_SUFFIXES]
    if not words:
        return []
    slugs: list[str] = []
    for joined in ("".join(words), "-".join(words)):
        slug = _fold(joined).strip("-")
        if 3 <= len(slug) <= 40 and slug not in slugs:
            slugs.append(slug)
    first = _fold(words[0])
    if len(words) > 1 and len(first) >= 4 and first not in _GENERIC_NAME_WORDS and first not in slugs:
        slugs.append(first)
    domains = [f"{s}.no" for s in slugs] + [f"{s}.com" for s in slugs[:1]]
    return domains[:limit]


def dns_resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except (OSError, UnicodeError):
        return False


@dataclass(frozen=True)
class Candidate:
    """A site to check and how it was proposed. A name-derived guess ("guess") has
    nothing but the name behind it, so it must be proven by the company's org
    number or registered contact details, never by a name match alone: a different
    company can own the obvious domain."""

    url: str
    kind: str  # "nav" | "email" | "guess"

    @property
    def needs_proof(self) -> bool:
        return self.kind == "guess"


_HOUSING_WORDS = ("borettslag", "boligsameie", "sameie", "brl", "bolig")


def keyless_candidates(
    legal_name: str, hints: SiteHints, resolves: Optional[Callable[[str], bool]] = None
) -> list[Candidate]:
    """Candidates in the order they should be tried, de-duplicated, each resolving
    in DNS. At most MAX_CANDIDATES_VERIFIED are returned.

    A housing co-op registers its property manager's address as its own e-mail, so
    its e-mail domain is the manager's site, not its own: no e-mail candidate for
    those."""
    resolves = resolves or dns_resolves  # looked up per call so tests can replace it
    ordered: list[Candidate] = [Candidate(u if "://" in u else f"https://{u}", "nav") for u in hints.nav_homepages]
    from_email = email_domain_candidate(hints.email)
    if from_email and not any(w in legal_name.lower() for w in _HOUSING_WORDS):
        ordered.append(Candidate(from_email, "email"))
    ordered += [Candidate(f"https://{d}", "guess") for d in name_domain_candidates(legal_name)]

    seen: set[str] = set()
    out: list[Candidate] = []
    for candidate in ordered:
        host = urlsplit(candidate.url).netloc.lower()
        host_key = host[4:] if host.startswith("www.") else host
        if not host_key or host_key in seen:
            continue
        seen.add(host_key)
        if resolves(host_key):
            out.append(candidate)
        if len(out) >= MAX_CANDIDATES_VERIFIED:
            break
    return out


def contact_span(text: str, hints: SiteHints) -> Optional[str]:
    """The text on the page that shows the phone number or the street address (with
    its postcode) the company registered, quoted as the page writes it; None when
    the page shows neither. Either is specific to this company; a bare postcode or
    a short number is not enough."""
    flattened = " ".join(re.sub(r"<[^>]+>", " ", text).split())
    if hints.phones:
        # Whole eight-digit numbers only (optionally +47 and spaced), never eight
        # digits lifted out of the middle of a longer run such as an org number.
        for match in _PHONE_ON_PAGE_RE.finditer(flattened):
            if re.sub(r"\D", "", match.group(1)) in hints.phones:
                return flattened[max(0, match.start() - 40) : match.end() + 40].strip()
    if hints.street and hints.postcode:
        street = " ".join(hints.street.split())
        at = flattened.lower().find(street.lower())
        if at >= 0 and hints.postcode in flattened:
            return flattened[max(0, at - 20) : at + len(street) + 40].strip()
    return None


def page_confirms_contact(text: str, hints: SiteHints) -> bool:
    return contact_span(text, hints) is not None


def page_shows_location(text: str, hints: SiteHints) -> bool:
    """True when the page shows the registered postcode followed by its town
    ("0183 Oslo"). Weaker than the phone or street address, so on its own it never
    proves identity; it is what lets a name match stand for a name-derived guess,
    because a same-named company abroad will not show a Norwegian postcode and town."""
    if not (hints.postcode and hints.poststed):
        return False
    flattened = " ".join(re.sub(r"<[^>]+>", " ", text).split())
    return re.search(rf"(?<!\d){hints.postcode}\s*,?\s+{re.escape(hints.poststed)}", flattened, re.IGNORECASE) is not None
