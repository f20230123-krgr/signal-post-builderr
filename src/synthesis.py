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

from typing import Optional

from src.models.profile import Claim, CompanyProfile, EvidenceState


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
