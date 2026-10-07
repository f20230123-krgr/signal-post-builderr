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
from src.pipeline.careers import CAREERS_VALUE_PREFIX, JOB_AD_VALUE_PREFIX


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
    "revisor": "auditor",
    "regnskapsfører": "accountant",
    "forretningsfører": "business manager",
    "deltaker med delt ansvar": "partner (shared liability)",
    "deltaker med fullt ansvar": "partner (full liability)",
    "innehaver": "owner",
    "komplementar": "general partner",
    "kontaktperson": "contact person",
    "norsk representant for utenlandsk enhet": "Norwegian representative",
    "bestyrende reder": "managing owner",
}
# Roles that do not run the company: never named under "Led by".
_NOT_LEADING = {"revisor", "regnskapsfører", "varamedlem", "kontaktperson"}
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_PLATFORMS = {
    "linkedin.com": "LinkedIn", "facebook.com": "Facebook", "instagram.com": "Instagram",
    "x.com": "X", "twitter.com": "X", "youtube.com": "YouTube", "tiktok.com": "TikTok",
}


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
    headline: str = ""

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


def _short_amount(value: str) -> str:
    """'197,053,257 NOK' -> '197.1 m NOK'. The summary rounds for readability; the claim it
    cites keeps the exact figure."""
    parsed = _amount(value)
    if not parsed:
        return value
    n, currency = parsed
    a = abs(n)
    if a >= 1_000_000_000:
        text = f"{n / 1e9:.2f} bn"
    elif a >= 1_000_000:
        text = f"{n / 1e6:.1f} m"
    elif a >= 10_000:
        text = f"{n / 1e3:.0f} k"
    else:
        text = f"{n:,}"
    return f"{text} {currency}".strip()


_INDUSTRY_CODE_RE = re.compile(r"^\d{2}\.\d{2,3}\s+")
_MAX_INDUSTRY_LEN = 70


_INDUSTRY_EN: Optional[dict[str, str]] = None


def _english_industry(code: str) -> Optional[str]:
    """Statistics Norway's official English label for an industry code (src/data/
    industry_en.json, shipped with the code: no request), or None."""
    global _INDUSTRY_EN
    if _INDUSTRY_EN is None:
        import json
        from pathlib import Path

        path = Path(__file__).parent / "data" / "industry_en.json"
        try:
            _INDUSTRY_EN = json.loads(path.read_text(encoding="utf-8")).get("codes", {})
        except (OSError, ValueError):
            _INDUSTRY_EN = {}
    return _INDUSTRY_EN.get(code)


def _industry_text(industry: str) -> str:
    """The industry in English when the code has an official English label ("17.120
    Produksjon av papir og papp" -> "manufacture of paper and paperboard"), else the
    registry's own wording."""
    code = industry.split(" ", 1)[0]
    english = _english_industry(code) if _INDUSTRY_CODE_RE.match(industry) else None
    if english:
        industry = f"{code} {english}"
    text = _INDUSTRY_CODE_RE.sub("", industry).strip()
    if len(text) > _MAX_INDUSTRY_LEN:
        text = text[: _MAX_INDUSTRY_LEN].rsplit(" ", 1)[0].rstrip(",;") + " ..."
    return text[:1].lower() + text[1:] if text else text


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
    if industry and not industry.startswith("00.000"):  # 00.000: the registry has no industry on file
        text += f" in {_industry_text(industry)}"
    if founded:
        text += f", founded {founded[:4]}"
    extras = [f"registry status {status}"] if status else []
    if employees:
        dated = li.employee_count.effective_date
        extras.append(f"{employees} employees" + (f" (registered {dated[:10]})" if dated else ""))
    if extras:
        text += "; " + ", ".join(extras)
    claims = [c for c in (li.legal_name, li.legal_form, li.industry, li.founded_date, li.operating_status, li.employee_count) if c is not None]
    return SummarySentence(text + ".", ["legal_name", "legal_form", "industry", "founded_date", "operating_status", "employee_count"], claims)


def _activity_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    """What the company itself registered that it does, quoted in the registry's own words
    (Norwegian, untranslated: a quote is not paraphrased)."""
    claim = profile.legal_identity.business_description
    text = _value_or_none(claim) if claim is not None else None
    if not text:
        return None
    return SummarySentence(f'Registered activity: "{text.rstrip(". ")}".', ["business_description"], [claim])


_METRIC_WORDS = [
    ("revenue", "revenue"), ("operating_result", "operating result"), ("annual_result", "net result"),
    ("total_assets", "total assets"), ("total_equity", "equity"), ("total_debt", "debt"),
]
# The figures the summary names: enough to say how big the company is and how it is doing.
_SUMMARY_METRICS = ("revenue", "annual_result", "total_equity")
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


def _revenue_change(profile: CompanyProfile) -> Optional[tuple[str, str, float, Claim, Claim]]:
    """(newest period, previous period, % change, newest claim, previous claim) for revenue
    between the two newest filings; only when both are in the same currency and the earlier
    one is not zero."""
    revenue = _metric_by_period(profile, "revenue")
    if len(revenue) < 2:
        return None
    newest, previous = sorted(revenue, reverse=True)[:2]
    a, b = _amount(revenue[newest].value), _amount(revenue[previous].value)
    if not a or not b or a[1] != b[1] or b[0] == 0:
        return None
    return newest, previous, (a[0] - b[0]) / abs(b[0]) * 100, revenue[newest], revenue[previous]


def _year_pair(profile: CompanyProfile, name: str, period: str) -> tuple[Optional[Claim], Optional[Claim], Optional[str]]:
    """(claim for `period`, claim for the filing before it, that earlier period) of one figure.
    The earlier claim is returned only when both are amounts in the same currency."""
    by_period = _metric_by_period(profile, name)
    now = by_period.get(period)
    earlier = sorted((p for p in by_period if p < period), reverse=True)
    if not now or not earlier:
        return now, None, None
    before = by_period[earlier[0]]
    a, b = _amount(now.value), _amount(before.value)
    if not a or not b or a[1] != b[1]:
        return now, None, None
    return now, before, earlier[0]


def _revenue_phrase(now: Claim, before: Optional[Claim], before_period: Optional[str]) -> str:
    if before is None:
        return f"revenue was {_short_amount(now.value)}"
    a, b = _amount(now.value)[0], _amount(before.value)[0]
    if b == 0:
        return f"revenue was {_short_amount(now.value)}"
    pct = (a - b) / abs(b) * 100
    if abs(pct) <= 0.05:
        return f"revenue was flat on {before_period} at {_short_amount(now.value)}"
    return f"revenue {'grew' if pct > 0 else 'fell'} {abs(pct):.1f}% on {before_period} to {_short_amount(now.value)}"


def _unsigned(value: str) -> str:
    return _short_amount(value).lstrip("-")


def _result_phrase(now: Claim, before: Optional[Claim], before_period: Optional[str], revenue: Optional[Claim]) -> str:
    """Profit or loss, its share of revenue, and how it compares with the filing before:
    a turnaround, a swing into loss, or simply higher or lower."""
    n = _amount(now.value)
    kind = "net profit" if n[0] >= 0 else "net loss"
    text = f"a {kind} of {_unsigned(now.value)}"
    r = _amount(revenue.value) if revenue else None
    if r and r[0] > 0 and r[1] == n[1]:
        margin = n[0] / r[0] * 100
        if abs(margin) < 1000:
            text += f" ({abs(margin):.1f}% of revenue)"
    if before is not None:
        p = _amount(before.value)[0]
        if p < 0 <= n[0]:
            text += f", turning round from a loss of {_unsigned(before.value)} in {before_period}"
        elif n[0] < 0 <= p:
            text += f", after a profit of {_unsigned(before.value)} in {before_period}"
        elif p != 0 and abs(n[0] - p) / abs(p) <= 0.005:
            text += f", about the same as in {before_period}"
        elif n[0] >= 0:
            text += f", {'up' if n[0] > p else 'down'} from {_unsigned(before.value)} in {before_period}"
        else:
            text += f", {'a smaller' if abs(n[0]) < abs(p) else 'a larger'} loss than the {_unsigned(before.value)} of {before_period}"
    return text


def _equity_phrase(now: Claim, before: Optional[Claim]) -> str:
    e = _amount(now.value)[0]
    if e < 0:
        return f"equity was negative ({_short_amount(now.value)})"
    if before is None:
        return f"equity was {_short_amount(now.value)}"
    p = _amount(before.value)[0]
    if p < 0:
        return f"equity turned positive at {_short_amount(now.value)}"
    if p != 0 and abs(e - p) / abs(p) <= 0.005:
        return f"equity held at {_short_amount(now.value)}"
    return f"equity {'rose' if e > p else 'fell'} to {_short_amount(now.value)}"


def _accounts_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    """How the company did in its latest filed year, in words: the revenue trend, profit or
    loss with its margin and its change on the filing before, and which way equity moved.
    Every figure and percentage is computed only from the cited filing figures (both years
    are cited whenever a comparison is made)."""
    periods = {period for name in _SUMMARY_METRICS for period in _metric_by_period(profile, name)}
    if periods:
        period = max(periods)
        claims: list[Claim] = []
        segments: list[str] = []
        revenue, revenue_before, revenue_before_period = _year_pair(profile, "revenue", period)
        result, result_before, result_before_period = _year_pair(profile, "annual_result", period)
        equity, equity_before, _ = _year_pair(profile, "total_equity", period)
        result_text = _result_phrase(result, result_before, result_before_period, revenue) if result else None
        if revenue:
            segment = _revenue_phrase(revenue, revenue_before, revenue_before_period)
            if result_text:
                segment += " and the company made " + result_text
            segments.append(segment)
        elif result_text:
            segments.append("the company made " + result_text)
        if equity:
            segments.append(_equity_phrase(equity, equity_before))
        for claim in (revenue, revenue_before, result, result_before, equity, equity_before):
            if claim is not None:
                claims.append(claim)
        if not segments:
            return None
        end = claims[0].effective_date[:10] if claims[0].effective_date else None
        text = f"In {period}{f' (year to {end})' if end else ''}, " + "; ".join(segments)
        return SummarySentence(text + ".", list(_SUMMARY_METRICS), claims)

    claim = profile.annual_accounts.latest
    value = _value_or_none(claim)
    if value is None:
        return None
    when = f" ({claim.reporting_period}" + (f", to {claim.effective_date[:10]}" if claim.effective_date else "") + ")" if claim.reporting_period else ""
    return SummarySentence(f"Latest filed accounts{when}: {value}.", ["annual_accounts_latest"], [claim])


_MAX_LEADERS = 2


def _leadership_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    everyone = [c for c in profile.leadership.leaders if c.state == EvidenceState.AVAILABLE and c.value]

    def role_of(claim: Claim) -> str:
        inner = claim.value[:-1].rsplit(" (", 1)[-1] if claim.value.endswith(")") and " (" in claim.value else ""
        return inner.split(",")[0].strip().lower()

    leaders = [c for c in everyone if role_of(c) not in _NOT_LEADING]
    others = len(everyone) - len(leaders)
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
    phrases = [f"{n} ({', '.join(r for r in people[n] if r)})" if any(people[n]) else n for n in shown]
    text = "Led by " + " and ".join(phrases)
    # Everyone else the registry lists (board members not named, deputies, auditor, accountant).
    more = len(names) - len(shown) + others
    if more:
        text += f", with {more} more registered role holder{'s' if more > 1 else ''}"
    return SummarySentence(text + ".", ["leader"], [c for n in shown for c in people_claims[n]])


_POSTCODE_TOWN_RE = re.compile(r"\b\d{4}\s+([A-ZÆØÅa-zæøå][\w .'-]*?)(?:\s*\(|,|$)")
_PAREN_TOWN_RE = re.compile(r"\(([A-ZÆØÅ][\wÆØÅæøå .'-]*?)(?:,|\))")


def _town(workplace: str) -> Optional[str]:
    """'ACME, Hammergata 20, 3264 LARVIK (12 ansatte)' -> 'Larvik'; 'ACME (BERGEN, 3 ansatte)' -> 'Bergen'."""
    match = _POSTCODE_TOWN_RE.search(workplace) or _PAREN_TOWN_RE.search(workplace)
    return match.group(1).strip().title() if match else None


def _workplaces_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    """Only when there is more than one site: a single registered address adds nothing."""
    places = [c for c in profile.leadership.workplaces if c.state == EvidenceState.AVAILABLE and c.value]
    if len(places) < 2:
        return None
    towns: list[str] = []
    for claim in places:
        town = _town(claim.value)
        if town and town not in towns:
            towns.append(town)
    where = f", including {', '.join(towns[:3])}" if towns else ""
    return SummarySentence(f"{len(places)} registered sites{where}.", ["workplace"], places[:3])


def _online_sentence(profile: CompanyProfile) -> Optional[SummarySentence]:
    """Website, profiles, hiring and the newest news in one line; every part says only what
    its claim says (a bare careers page is "careers page", never "is hiring")."""
    parts: list[str] = []
    fields: list[str] = []
    claims: list[Claim] = []

    site = profile.online_presence.official_site
    if _value_or_none(site):
        parts.append(_host(site.value))
        fields.append("official_website")
        claims.append(site)

    profiles = [c for c in profile.online_presence.company_profiles if c.state == EvidenceState.AVAILABLE and c.value]
    platforms: list[str] = []
    for claim in profiles:
        host = _host(claim.value)
        label = next((name for domain, name in _PLATFORMS.items() if host == domain or host.endswith("." + domain)), host)
        if label not in platforms:
            platforms.append(label)
    if platforms:
        parts.append(", ".join(platforms[:4]) + " profile" + ("s" if len(platforms) > 1 else ""))
        fields.append("company_profile")
        claims.extend(profiles[:2])

    signals = [c for c in profile.activity.hiring_signals if c.state == EvidenceState.AVAILABLE and c.value]
    if signals:
        def is_page(claim: Claim) -> bool:
            return claim.value.startswith(CAREERS_VALUE_PREFIX) and " open roles" not in claim.value

        first = sorted(signals, key=is_page)[0]  # role-level evidence first
        if " open roles" in first.value:
            count = re.search(r"lists (\d+) open roles", first.value)
            parts.append(f"careers page lists {count.group(1)} open roles" if count else "careers page lists open roles")
        elif first.value.startswith(CAREERS_VALUE_PREFIX):
            parts.append("careers page")
        elif first.value.startswith(JOB_AD_VALUE_PREFIX):
            parts.append("a job ad on its careers pages")
        else:
            title = re.sub(r"\s*\(published.*$| - NAV$", "", first.value).strip()
            parts.append(f"hiring: {title}")
        fields.append("hiring_signal")
        claims.append(first)

    news = _latest_news(profile)
    if news is not None:
        claim, date, headline = news
        parts.append(f"latest news \"{headline}\" ({date})")
        fields.append("dated_activity")
        claims.append(claim)
    elif (latest := _latest_dated(profile.activity.dated_activity)) is not None:
        claim, date = latest
        parts.append(f"last registry update {date}")
        fields.append("dated_activity")
        claims.append(claim)

    if not parts:
        return None
    return SummarySentence("Online: " + "; ".join(parts) + ".", fields, claims)


def _latest_news(profile: CompanyProfile) -> Optional[tuple[Claim, str, str]]:
    """The newest dated item from the company's own pages (not a registry change event),
    as (claim, ISO date, headline). An item dated after this run (an upcoming event) is not
    news yet and is never called the latest news."""
    best: Optional[tuple[Claim, str, str]] = None
    today = profile.run_timestamp.date().isoformat()
    for claim in profile.activity.dated_activity:
        if claim.state != EvidenceState.AVAILABLE or not claim.value or claim.source_class == "official_registry":
            continue
        if claim.value.startswith("Registry record"):
            continue
        date = (claim.effective_date or "")[:10]
        if not date:
            found = _DATE_RE.findall(claim.value)
            date = max(found) if found else ""
        if not date or date > today:
            continue
        headline = re.sub(r"\s*\([^()]*\d{4}[^()]*\)\s*$", "", claim.value).strip()
        # A headline scraped from a page line can carry its own date ("Arkiv 2026-01-28 ..."):
        # the date is already stated beside it.
        headline = re.sub(r"\s+", " ", _DATE_RE.sub(" ", headline)).strip(" -:")
        headline = (headline[:90].rsplit(" ", 1)[0] + " ...") if len(headline) > 90 else headline
        if best is None or date > best[1]:
            best = (claim, date, headline)
    return best


_CHANGE_PATTERNS = [
    (re.compile(r"^hiring_signal added: \[(.*)\]$"), lambda m: "new hiring signal"),
    (re.compile(r"^hiring_signal removed"), lambda m: "a hiring signal ended"),
    (re.compile(r"^operating_status: '(.*)' -> '(.*)'$"), lambda m: f"registry status {m.group(1)} -> {m.group(2)}"),
    (re.compile(r"^employee_count: '(.*)' -> '(.*)'$"), lambda m: f"employees {m.group(1)} -> {m.group(2)}"),
    (re.compile(r"^legal_name: '(.*)' -> '(.*)'$"), lambda m: f"renamed from {m.group(1)}"),
    (re.compile(r"^official_site: (?:'.*'|None) -> '(.*)'$"), lambda m: f"website now {_host(m.group(1))}"),
    (re.compile(r"^official_site: '.*' -> None$"), lambda m: "website no longer found"),
    (re.compile(r"^annual_accounts\.latest"), lambda m: "a new annual filing"),
    (re.compile(r"^leadership\.leaders"), lambda m: "the registered role holders changed"),
    (re.compile(r"^leadership\.workplaces"), lambda m: "the registered sites changed"),
    (re.compile(r"^(legal_form|industry): '(.*)' -> '(.*)'$"), lambda m: f"{m.group(1).replace('_', ' ')} now {m.group(3)}"),
]


def humanize_change(change: str) -> str:
    """'employee_count: '20' -> '21'' -> 'employees 20 -> 21'. The raw strings stay in the
    envelope's `changes` for machines; the summary says it in words."""
    for pattern, render in _CHANGE_PATTERNS:
        match = pattern.match(change)
        if match:
            return render(match)
    return change


def build_summary(profile: CompanyProfile) -> Summary:
    """A short dated narrative (about 80 words): what the company is, how big and how it is
    doing, who runs it, where it is online, what changed, and what could not be found.
    Same discipline as always: nothing is stated that a verified Claim does not support,
    and every gap is named under `unknowns` instead of guessed."""
    as_of = profile.run_timestamp.date().isoformat()
    builders = (
        _identity_sentence, _activity_sentence, _accounts_sentence, _leadership_sentence, _workplaces_sentence,
        _online_sentence,
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
        sentences.append(SummarySentence("First run, so there is nothing earlier to compare with.", []))
    elif meta.material_changes:
        changes = list(meta.material_changes)
        said = [humanize_change(c) for c in changes[:3]]
        more = f" (+{len(changes) - 3} more)" if len(changes) > 3 else ""
        previous = f" on {meta.previous_run_timestamp.date().isoformat()}" if meta.previous_run_timestamp else ""
        sentences.append(SummarySentence(f"Since the previous run{previous}: " + "; ".join(said) + more + ".", []))
    else:
        changes = []
        previous = f" ({meta.previous_run_timestamp.date().isoformat()})" if meta.previous_run_timestamp else ""
        sentences.append(SummarySentence(f"No material change since the previous run{previous}.", []))
    if unknowns:
        sentences.append(SummarySentence("Not found: " + ", ".join(unknowns) + ".", []))

    if sentences:
        sentences[0].text = f"As of {as_of}: {sentences[0].text}"
    return Summary(as_of=as_of, sentences=sentences, unknowns=unknowns, changes=changes, headline=_headline(profile))


def _headline(profile: CompanyProfile) -> str:
    """One line for a list or a tooltip: who, what state, and the headline figure."""
    li = profile.legal_identity
    name = _value_or_none(li.legal_name) or f"Org. {profile.org_number}"
    status = _optional_value(li.operating_status)
    bits = [name + (f": {status.lower()}" if status else "")]
    revenue = _metric_by_period(profile, "revenue")
    if revenue:
        period = max(revenue)
        change = _revenue_change(profile)
        text = f"revenue {_short_amount(revenue[period].value)} ({period}"
        if change and change[0] == period:
            text += f", {'up' if change[2] > 0.05 else 'down' if change[2] < -0.05 else 'flat'}"
            text += "" if abs(change[2]) <= 0.05 else f" {abs(change[2]):.0f}%"
        bits.append(text + ")")
    return ", ".join(bits)


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
