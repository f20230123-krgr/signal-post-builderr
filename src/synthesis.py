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

from src.models.profile import Claim, CompanyProfile, EvidenceState


def _value_or_none(claim: Claim) -> str | None:
    return claim.value if claim.state == EvidenceState.AVAILABLE else None


def _list_values(claims: list[Claim]) -> list[str]:
    return [c.value for c in claims if c.state == EvidenceState.AVAILABLE and c.value]


def _answer(question: str, answer: str, state: EvidenceState) -> dict:
    return {"question": question, "answer": answer, "state": state.value}


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
    return _answer("What is the company's legal name and brand?", text, EvidenceState.AVAILABLE)


def _website_answer(profile: CompanyProfile) -> dict:
    claim = profile.online_presence.official_site
    value = _value_or_none(claim)
    if value is None:
        return _answer("What is the official website?", "No official website on file.", claim.state)
    return _answer("What is the official website?", value, EvidenceState.AVAILABLE)


def _leadership_answer(profile: CompanyProfile) -> dict:
    leaders = _list_values(profile.leadership.leaders)
    if not leaders:
        return _answer("Who leads the company?", "No leadership information available.", EvidenceState.NOT_AVAILABLE)
    return _answer("Who leads the company?", "; ".join(leaders), EvidenceState.AVAILABLE)


def _hiring_answer(profile: CompanyProfile) -> dict:
    signals = _list_values(profile.activity.hiring_signals)
    if not signals:
        return _answer("Is the company currently hiring?", "No hiring signals found.", EvidenceState.NOT_AVAILABLE)
    return _answer("Is the company currently hiring?", f"Yes, hiring signals found: {signals[0]}", EvidenceState.AVAILABLE)


def _accounts_answer(profile: CompanyProfile) -> dict:
    claim = profile.annual_accounts.latest
    value = _value_or_none(claim)
    if value is None:
        return _answer("What are the latest filed annual accounts?", "Not available.", claim.state)
    period = f" ({claim.reporting_period})" if claim.reporting_period else ""
    return _answer("What are the latest filed annual accounts?", f"{value}{period}", EvidenceState.AVAILABLE)


def _workplaces_answer(profile: CompanyProfile) -> dict:
    workplaces = _list_values(profile.leadership.workplaces)
    if not workplaces:
        return _answer("Where are the registered workplaces?", "No registered workplaces available.", EvidenceState.NOT_AVAILABLE)
    return _answer("Where are the registered workplaces?", ", ".join(workplaces), EvidenceState.AVAILABLE)


def _changes_answer(profile: CompanyProfile) -> dict:
    meta = profile.refresh_metadata
    if meta.is_first_run:
        return _answer("What has changed since the last run?", "This is the first run -- no prior snapshot to compare.", EvidenceState.NOT_APPLICABLE)
    if not meta.material_changes:
        return _answer("What has changed since the last run?", "No material changes since the last run.", EvidenceState.AVAILABLE)
    return _answer("What has changed since the last run?", "; ".join(meta.material_changes), EvidenceState.AVAILABLE)


def answer_business_questions(profile: CompanyProfile) -> list[dict]:
    """Fixed set of business questions, each answered strictly from Claims
    already in `profile` -- see module docstring. Returns a list of
    {"question", "answer", "state"} dicts, in a stable, presentation-ready
    order."""
    return [
        _identity_answer(profile),
        _website_answer(profile),
        _leadership_answer(profile),
        _hiring_answer(profile),
        _accounts_answer(profile),
        _workplaces_answer(profile),
        _changes_answer(profile),
    ]
