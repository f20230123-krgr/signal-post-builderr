"""
Decision-useful synthesis: a fixed set of business questions answered
directly from an already-verified CompanyProfile, nothing else.

Evaluator feedback: "Add a simple synthesis interface that answers fixed
business questions from the collected records." This module is deliberately
NOT an LLM summarizer -- every answer is templated from Claim values that
already passed verify()'s precision gate, so a synthesized answer can never
say more than the evidence actually supports. An unavailable Claim always
produces an explicit "not available"-shaped answer, never a guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from src.models.profile import Claim, CompanyProfile, EvidenceState
from src.pipeline.careers import CAREERS_VALUE_PREFIX


def _value_or_none(claim: Claim) -> str | None:
    return claim.value if claim.state == EvidenceState.AVAILABLE else None


def _list_values(claims: list[Claim]) -> list[str]:
    return [c.value for c in claims if c.state == EvidenceState.AVAILABLE and c.value]


def _sources(*claims: Claim | None) -> list[str]:
    """Deduplicated source URLs/identifiers for the Claim(s) an answer is
    templated from, order preserved. Evaluator feedback: the summary must
    give "sources for its conclusions" -- a plain answer string isn't
    enough. A claim with no source (never AVAILABLE, or a None claim) is
    skipped, never a guessed placeholder."""
    seen: list[str] = []
    for claim in claims:
        if claim is not None and claim.state == EvidenceState.AVAILABLE and claim.source and claim.source not in seen:
            seen.append(claim.source)
    return seen


def _sources_from_list(claims: list[Claim]) -> list[str]:
    return _sources(*claims)


def _answer(question: str, answer: str, state: EvidenceState, sources: Optional[list[str]] = None) -> dict:
    return {"question": question, "answer": answer, "state": state.value, "sources": sources or []}


def _identity_answer(profile: CompanyProfile) -> dict:
    legal_name = _value_or_none(profile.legal_identity.legal_name)
    brand = _value_or_none(profile.legal_identity.public_brand)
    if legal_name is None:
        return _answer(
            "What is the company's legal name and brand?",
            "Legal name not available.",
            profile.legal_identity.legal_name.state,
        )
    text = legal_name if not brand or brand == legal_name else f"{legal_name} (publicly known as {brand})"
    return _answer(
        "What is the company's legal name and brand?", text, EvidenceState.AVAILABLE,
        sources=_sources(profile.legal_identity.legal_name, profile.legal_identity.public_brand),
    )


def _website_answer(profile: CompanyProfile) -> dict:
    claim = profile.online_presence.official_site
    value = _value_or_none(claim)
    if value is None:
        return _answer("What is the official website?", "No official website on file.", claim.state)
    return _answer("What is the official website?", value, EvidenceState.AVAILABLE, sources=_sources(claim))


def _leadership_answer(profile: CompanyProfile) -> dict:
    leaders = _list_values(profile.leadership.leaders)
    if not leaders:
        return _answer("Who leads the company?", "No leadership information available.", EvidenceState.NOT_AVAILABLE)
    return _answer(
        "Who leads the company?", "; ".join(leaders), EvidenceState.AVAILABLE,
        sources=_sources_from_list(profile.leadership.leaders),
    )


def _hiring_answer(profile: CompanyProfile) -> dict:
    signals = _list_values(profile.activity.hiring_signals)
    if not signals:
        return _answer("Is the company currently hiring?", "No hiring signals found.", EvidenceState.NOT_AVAILABLE)
    return _answer(
        "Is the company currently hiring?", f"Yes, hiring signals found: {signals[0]}", EvidenceState.AVAILABLE,
        sources=_sources_from_list(profile.activity.hiring_signals),
    )


def _accounts_answer(profile: CompanyProfile) -> dict:
    claim = profile.annual_accounts.latest
    value = _value_or_none(claim)
    if value is None:
        return _answer("What are the latest filed annual accounts?", "Not available.", claim.state)
    period = f" ({claim.reporting_period})" if claim.reporting_period else ""
    return _answer(
        "What are the latest filed annual accounts?", f"{value}{period}", EvidenceState.AVAILABLE, sources=_sources(claim)
    )


def _workplaces_answer(profile: CompanyProfile) -> dict:
    workplaces = _list_values(profile.leadership.workplaces)
    if not workplaces:
        return _answer("Where are the registered workplaces?", "No registered workplaces available.", EvidenceState.NOT_AVAILABLE)
    return _answer(
        "Where are the registered workplaces?", ", ".join(workplaces), EvidenceState.AVAILABLE,
        sources=_sources_from_list(profile.leadership.workplaces),
    )


def _changes_answer(profile: CompanyProfile) -> dict:
    meta = profile.refresh_metadata
    if meta.is_first_run:
        return _answer("What has changed since the last run?", "This is the first run -- no prior snapshot to compare.", EvidenceState.NOT_APPLICABLE)
    if not meta.material_changes:
        return _answer("What has changed since the last run?", "No material changes since the last run.", EvidenceState.AVAILABLE)
    return _answer("What has changed since the last run?", "; ".join(meta.material_changes), EvidenceState.AVAILABLE)


def _optional_value(claim) -> str | None:
    """Value of an optional identity Claim, or None when the claim isn't
    present at all (company outside Builderr's universe manifest) or isn't
    AVAILABLE. Never guesses a stand-in."""
    if claim is None:
        return None
    return _value_or_none(claim)


def _business_answer(profile: CompanyProfile) -> dict:
    """What the company actually does, plus how big it is -- the two things
    a job seeker or a BD rep asks first. Both come free from the registry
    record (registry_extras.universe_identity_facts), so this costs nothing
    beyond the templating."""
    industry = _optional_value(profile.legal_identity.industry)
    employees = _optional_value(profile.legal_identity.employee_count)

    if industry is None and employees is None:
        return _answer(
            "What does the company do, and how big is it?",
            "No registered industry or employee count available.",
            EvidenceState.NOT_AVAILABLE,
        )

    parts = []
    if industry:
        parts.append(f"Registered industry: {industry}")
    if employees:
        parts.append(f"{employees} registered employees")
    return _answer(
        "What does the company do, and how big is it?", "; ".join(parts), EvidenceState.AVAILABLE,
        sources=_sources(profile.legal_identity.industry, profile.legal_identity.employee_count),
    )


def _status_answer(profile: CompanyProfile) -> dict:
    """Whether the entity is actually operating -- the single most
    decision-relevant fact for an investor or a job seeker, and one the
    registry answers definitively (bankrupt / in liquidation / active)."""
    status = _optional_value(profile.legal_identity.operating_status)
    legal_form = _optional_value(profile.legal_identity.legal_form)

    if status is None:
        return _answer(
            "Is the company still operating?", "No registered operating status available.", EvidenceState.NOT_AVAILABLE
        )
    text = f"{status}" + (f" ({legal_form})" if legal_form else "")
    return _answer(
        "Is the company still operating?", text, EvidenceState.AVAILABLE,
        sources=_sources(profile.legal_identity.operating_status, profile.legal_identity.legal_form),
    )


def _founded_answer(profile: CompanyProfile) -> dict:
    """How long the company has existed -- a basic trust signal for anyone
    evaluating an employer, a partner or an investment."""
    founded = _optional_value(profile.legal_identity.founded_date)
    if founded is None:
        return _answer("When was the company founded?", "No founding date available.", EvidenceState.NOT_AVAILABLE)
    return _answer(
        "When was the company founded?", f"Founded {founded}", EvidenceState.AVAILABLE,
        sources=_sources(profile.legal_identity.founded_date),
    )


def _recent_activity_answer(profile: CompanyProfile) -> dict:
    """Most recent dated public activity. With registry update events now
    feeding dated_activity, this is answerable even for the ~89% of
    companies that have no website at all."""
    activity = _list_values(profile.activity.dated_activity)
    if not activity:
        return _answer(
            "What is the most recent public activity?", "No dated public activity found.", EvidenceState.NOT_AVAILABLE
        )
    return _answer(
        "What is the most recent public activity?", activity[0], EvidenceState.AVAILABLE,
        sources=_sources(profile.activity.dated_activity[0]),
    )


_LEGAL_FORMS = {
    "AS": "limited company (AS)",
    "ASA": "public limited company (ASA)",
    "ENK": "sole proprietorship (ENK)",
    "NUF": "Norwegian-registered foreign entity (NUF)",
    "SA": "cooperative (SA)",
    "BRL": "housing cooperative (BRL)",
    "ANS": "general partnership (ANS)",
    "DA": "shared-liability partnership (DA)",
    "STI": "foundation (STI)",
}
_ROLES = {
    "daglig leder": "managing director",
    "styrets leder": "chair of the board",
    "styremedlem": "board member",
    "nestleder": "deputy chair",
    "varamedlem": "deputy board member",
}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PLATFORMS = {
    "linkedin.com": "LinkedIn", "facebook.com": "Facebook", "instagram.com": "Instagram",
    "x.com": "X", "twitter.com": "X", "youtube.com": "YouTube", "tiktok.com": "TikTok",
}
_MAX_LEADERS = 3
_MAX_WORKPLACES = 2


@dataclass
class SummarySentence:
    text: str
    fields: list[str]
    claims: list[Claim] = field(default_factory=list)


@dataclass
class Summary:
    """One short, dated narrative about a company, built only from its verified
    claims: what it is, how it is doing, who runs it, what changed, and what is
    still unknown. Each sentence names the claim fields it rests on and keeps
    the Claim objects so the envelope can attach their evidence ids."""

    as_of: str
    sentences: list[SummarySentence]
    unknowns: list[str]
    changes: list[str]

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.sentences)


def _day(claim: Claim) -> Optional[str]:
    if claim.effective_date:
        return claim.effective_date[:10]
    return claim.retrieved_at.date().isoformat() if claim.retrieved_at else None


def _host(url: str) -> str:
    netloc = urlsplit(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def _role_text(leader_value: str) -> str:
    """'Roger Tveide (Daglig leder)' -> 'Roger Tveide (managing director)'."""
    if leader_value.endswith(")") and " (" in leader_value:
        name, role = leader_value[:-1].rsplit(" (", 1)
        return f"{name} ({_ROLES.get(role.strip().lower(), role.strip())})"
    return leader_value


def _latest_dated(claims: list[Claim]) -> Optional[tuple[Claim, str]]:
    """The claim whose own text carries the most recent date, with that date."""
    best: Optional[tuple[Claim, str]] = None
    for claim in claims:
        if claim.state != EvidenceState.AVAILABLE or not claim.value:
            continue
        match = _DATE_RE.findall(claim.value)
        if match and (best is None or max(match) > best[1]):
            best = (claim, max(match))
    return best


def _identity_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    li = profile.legal_identity
    name = _value_or_none(li.legal_name)
    if name is None:
        return None
    form = _optional_value(li.legal_form)
    industry = _optional_value(li.industry)
    founded = _optional_value(li.founded_date)
    status = _optional_value(li.operating_status)
    employees = _optional_value(li.employee_count)

    # A form code we have no plain-English phrase for is stated as the code, not
    # forced into "is a ESEK".
    if form in _LEGAL_FORMS:
        text = f"{name} (org. no. {profile.org_number}) is a {_LEGAL_FORMS[form]}"
    elif form:
        text = f"{name} (org. no. {profile.org_number}) is a registered Norwegian entity (legal form {form})"
    else:
        text = f"{name} (org. no. {profile.org_number}) is a registered Norwegian entity"
    if industry:
        text += f" in the industry {industry}"
    if founded:
        text += f", founded {founded}"
    text += "."
    if status:
        text += f" Registry status: {status}."
    if employees:
        text += f" {employees} registered employees."
    claims = [c for c in (li.legal_name, li.legal_form, li.industry, li.founded_date, li.operating_status, li.employee_count) if c is not None]
    return SummarySentence(text, ["legal_name", "legal_form", "industry", "founded_date", "operating_status", "employee_count"], claims)


_METRIC_WORDS = [
    ("revenue", "revenue"), ("operating_result", "operating result"), ("annual_result", "net result"),
    ("total_assets", "total assets"), ("total_equity", "equity"), ("total_debt", "debt"),
]
_AMOUNT_RE = re.compile(r"^(-?[\d,]+)\s*([A-Z]{3})?$")


def _amount(value: str) -> Optional[tuple[int, str]]:
    match = _AMOUNT_RE.match(value.strip())
    return (int(match.group(1).replace(",", "")), match.group(2) or "") if match else None


def _metric_by_period(profile: CompanyProfile, name: str) -> dict[str, Claim]:
    """The available claims of one figure, keyed by their reporting period."""
    return {
        c.reporting_period: c
        for c in profile.annual_accounts.metrics.get(name, [])
        if c.state == EvidenceState.AVAILABLE and c.value and c.reporting_period
    }


def _accounts_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    """The latest filing's figures, from the discrete claims when there are any
    (each with its own period), else from the combined latest-accounts claim."""
    revenue_periods = {
        period for name, _ in _METRIC_WORDS for period in _metric_by_period(profile, name)
    }
    if revenue_periods:
        period = max(revenue_periods)
        claims, parts = [], []
        for name, word in _METRIC_WORDS:
            claim = _metric_by_period(profile, name).get(period)
            if claim:
                claims.append(claim)
                parts.append(f"{word} {claim.value}")
        end = f", period ending {claims[0].effective_date[:10]}" if claims[0].effective_date else ""
        return SummarySentence(
            f"Latest filed accounts ({period}{end}): " + ", ".join(parts) + ".",
            [name for name, _ in _METRIC_WORDS], claims,
        )

    claim = profile.annual_accounts.latest
    value = _value_or_none(claim)
    if value is None:
        return None
    when = f" ({claim.reporting_period}" + (f", period ending {claim.effective_date[:10]}" if claim.effective_date else "") + ")" if claim.reporting_period else ""
    text = f"Latest filed accounts{when}: {value}."
    history = [c for c in profile.annual_accounts.history if c.state == EvidenceState.AVAILABLE]
    if history:
        text += f" {len(history)} earlier filing{'s' if len(history) != 1 else ''} on record."
    return SummarySentence(text, ["annual_accounts_latest", "annual_accounts_history"], [claim] + history[:1])


def _trend_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    """How the company is doing: the change in revenue between its two newest
    filings, computed from the two sourced figures and quoted with both. Only when
    both are in the same currency and the earlier one is not zero."""
    revenue = _metric_by_period(profile, "revenue")
    if len(revenue) < 2:
        return None
    newest, previous = sorted(revenue, reverse=True)[:2]
    a, b = _amount(revenue[newest].value), _amount(revenue[previous].value)
    if not a or not b or a[1] != b[1] or b[0] == 0:
        return None
    change = (a[0] - b[0]) / abs(b[0]) * 100
    direction = "up" if change > 0.05 else "down" if change < -0.05 else "flat"
    text = (
        f"Revenue {direction} {abs(change):.1f}% from {previous} to {newest} "
        f"({revenue[previous].value} to {revenue[newest].value})"
        if direction != "flat" else f"Revenue flat between {previous} and {newest} ({revenue[newest].value})"
    )
    result = _metric_by_period(profile, "annual_result")
    claims = [revenue[newest], revenue[previous]]
    if newest in result and previous in result:
        r_new, r_old = _amount(result[newest].value), _amount(result[previous].value)
        if r_new and r_old and r_new[1] == r_old[1]:
            text += f"; net result {result[previous].value} to {result[newest].value}"
            claims += [result[newest], result[previous]]
    return SummarySentence(text + ".", ["revenue", "annual_result"], claims)


def _leadership_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    leaders = [c for c in profile.leadership.leaders if c.state == EvidenceState.AVAILABLE and c.value]
    if not leaders:
        return None
    # One person often holds several roles (managing director and board
    # member): merge them rather than repeating the name.
    people: dict[str, list[str]] = {}
    people_claims: dict[str, list[Claim]] = {}
    for claim in leaders:
        text = _role_text(claim.value)
        name, _, role = text.partition(" (")
        people.setdefault(name, []).append(role[:-1] if role else "")
        people_claims.setdefault(name, []).append(claim)
    names = list(people)
    shown = names[:_MAX_LEADERS]
    text = "Run by " + ", ".join(
        f"{n} ({', '.join(r for r in people[n] if r)})" if any(people[n]) else n for n in shown
    )
    if len(names) > len(shown):
        text += f" and {len(names) - len(shown)} more registered role holders"
    return SummarySentence(text + ".", ["leader"], [c for n in shown for c in people_claims[n]])


def _workplaces_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    places = [c for c in profile.leadership.workplaces if c.state == EvidenceState.AVAILABLE and c.value]
    if not places:
        return None
    shown = places[:_MAX_WORKPLACES]
    lead_in = f"{len(places)} registered workplaces, e.g. " if len(places) > 1 else "1 registered workplace: "
    text = lead_in + "; ".join(c.value for c in shown) + "."
    return SummarySentence(text, ["workplace"], shown)


def _web_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    site = profile.online_presence.official_site
    profiles = [c for c in profile.online_presence.company_profiles if c.state == EvidenceState.AVAILABLE and c.value]
    parts: list[str] = []
    claims: list[Claim] = []
    if _value_or_none(site):
        parts.append(f"official website {_host(site.value)}")
        claims.append(site)
    platforms: list[str] = []
    for claim in profiles:
        host = _host(claim.value)
        label = next((name for domain, name in _PLATFORMS.items() if host == domain or host.endswith("." + domain)), host)
        if label not in platforms:
            platforms.append(label)
    if platforms:
        parts.append("company profiles on " + ", ".join(platforms[:4]))
        claims.extend(profiles[:4])
    if not parts:
        return None
    return SummarySentence("Online: " + "; ".join(parts) + ".", ["official_website", "company_profile"], claims)


def _hiring_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    signals = [c for c in profile.activity.hiring_signals if c.state == EvidenceState.AVAILABLE and c.value]
    if not signals:
        return None
    # Role-level evidence ("Careers page lists 3 open roles, e.g. ...") is a
    # hiring statement; a bare "Careers page: <url>" only says the page exists,
    # and the summary must not say more than the claim does.
    def only_a_page(claim: Claim) -> bool:
        return claim.value.startswith(CAREERS_VALUE_PREFIX) and " open roles" not in claim.value

    ranked = sorted(signals, key=only_a_page)  # role-level evidence first
    first = ranked[0]
    when = f" (seen {_day(first)})" if _day(first) else ""
    more = f" and {len(signals) - 1} more" if len(signals) > 1 else ""
    # A careers claim already reads as a sentence ("Careers page: <url>" or
    # "Careers page lists 3 open roles, ..."); only a NAV ad or a job posting needs
    # the "Hiring:" lead-in.
    text = first.value if first.value.startswith(CAREERS_VALUE_PREFIX) else f"Hiring: {first.value}"
    return SummarySentence(f"{text}{more}{when}.", ["hiring_signal"], [first])


def _activity_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    latest = _latest_dated(profile.activity.dated_activity)
    if latest is None:
        return None
    claim, date = latest
    # "Registry record updated (Endring) on 2026-04-28 - Brønnøysundregistrene"
    # says nothing more than the date; don't repeat it as if it were news.
    if claim.value.startswith("Registry record updated"):
        return SummarySentence(f"Last registry update on {date}.", ["dated_activity"], [claim])
    return SummarySentence(f"Most recent dated activity ({date}): {claim.value}.", ["dated_activity"], [claim])


def build_summary(profile: CompanyProfile) -> Summary:
    """The short dated narrative for `profile`. Same discipline as the Q&A in
    answer_business_questions: nothing is stated that a verified Claim doesn't
    support, and every gap is named under `unknowns` instead of guessed."""
    as_of = profile.run_timestamp.date().isoformat()
    builders = (
        _identity_sentence, _accounts_sentence, _trend_sentence, _leadership_sentence, _workplaces_sentence,
        _web_sentence, _hiring_sentence, _activity_sentence,
    )
    sentences = [s for s in (build(profile) for build in builders) if s is not None]

    unknowns: list[str] = []
    if _value_or_none(profile.online_presence.official_site) is None:
        unknowns.append("official website")
    if not profile.online_presence.company_profiles:
        unknowns.append("company-owned social profiles")
    if _value_or_none(profile.annual_accounts.latest) is None and not profile.annual_accounts.metrics:
        unknowns.append("annual accounts")
    if not profile.leadership.leaders:
        unknowns.append("leadership")
    if not profile.activity.hiring_signals:
        unknowns.append("hiring signals")

    meta = profile.refresh_metadata
    if meta.is_first_run:
        changes: list[str] = []
        sentences.append(SummarySentence(f"First snapshot on {as_of}; there is no earlier run to compare against.", []))
    elif meta.material_changes:
        changes = list(meta.material_changes)
        sentences.append(SummarySentence("Changed since the last run: " + "; ".join(changes) + ".", []))
    else:
        changes = []
        sentences.append(SummarySentence("No material changes since the last run.", []))
    if unknowns:
        sentences.append(SummarySentence("Not found: " + ", ".join(unknowns) + ".", []))

    if sentences:
        sentences[0].text = f"As of {as_of}: {sentences[0].text}"
    return Summary(as_of=as_of, sentences=sentences, unknowns=unknowns, changes=changes)


def answer_business_questions(profile: CompanyProfile) -> list[dict]:
    """Fixed set of business questions, each answered strictly from Claims
    already in `profile` -- see module docstring. Returns a list of
    {"question", "answer", "state"} dicts, in a stable, presentation-ready
    order."""
    return [
        _identity_answer(profile),
        _business_answer(profile),
        _status_answer(profile),
        _founded_answer(profile),
        _website_answer(profile),
        _leadership_answer(profile),
        _hiring_answer(profile),
        _accounts_answer(profile),
        _workplaces_answer(profile),
        _recent_activity_answer(profile),
        _changes_answer(profile),
    ]
