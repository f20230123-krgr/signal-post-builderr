"""
Tests for src/synthesis.py.

Evaluator feedback: "Add a simple synthesis interface that answers fixed
business questions from the collected records." Every answer must be
strictly derived from already-verified Claims -- never fabricated, and
explicit about evidence gaps rather than silently guessing.
"""
from datetime import datetime, timezone

from src.models.profile import EvidenceState
from src.synthesis import answer_business_questions
from tests.conftest import available_claim, make_profile


def test_full_data_answers_every_question_with_real_values():
    profile = make_profile(
        legal_name=available_claim("EQUINOR ASA"),
        official_site=available_claim("https://www.equinor.com"),
        leaders=[available_claim("Anders Opedal (President and CEO)")],
        hiring_signals=[available_claim("Kahoot! AS is hiring: Senior Backend Engineer")],
        annual_latest=available_claim("67,956,000,000", reporting_period="FY2025"),
        workplaces=[available_claim("SOTRA")],
        is_first_run=False,
        material_changes=["legal_name: 'Old' -> 'EQUINOR ASA'"],
    )

    answers = answer_business_questions(profile)
    by_question = {a["question"]: a for a in answers}

    assert "EQUINOR ASA" in by_question["What is the company's legal name and brand?"]["answer"]
    assert "equinor.com" in by_question["What is the official website?"]["answer"]
    assert "Opedal" in by_question["Who leads the company?"]["answer"]
    assert "hiring" in by_question["Is the company currently hiring?"]["answer"].lower()
    assert "67,956,000,000" in by_question["What are the latest filed annual accounts?"]["answer"]
    assert "FY2025" in by_question["What are the latest filed annual accounts?"]["answer"]
    assert "SOTRA" in by_question["Where are the registered workplaces?"]["answer"]
    assert "legal_name" in by_question["What has changed since the last run?"]["answer"]

    for a in answers:
        assert a["state"] in {s.value for s in EvidenceState}


def test_sparse_data_answers_are_explicit_about_gaps_never_fabricated():
    profile = make_profile()  # everything NOT_AVAILABLE, first run

    answers = answer_business_questions(profile)
    by_question = {a["question"]: a for a in answers}

    for question in [
        "What is the company's legal name and brand?",
        "What is the official website?",
        "Who leads the company?",
        "What are the latest filed annual accounts?",
        "Where are the registered workplaces?",
    ]:
        entry = by_question[question]
        assert entry["state"] != "available"
        assert "not available" in entry["answer"].lower() or "no " in entry["answer"].lower()

    hiring = by_question["Is the company currently hiring?"]
    assert "no hiring signals" in hiring["answer"].lower()

    changes = by_question["What has changed since the last run?"]
    assert "first run" in changes["answer"].lower()


def test_material_changes_on_a_refresh_with_no_changes_says_so():
    profile = make_profile(is_first_run=False, previous_run_timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc), material_changes=[])

    answers = answer_business_questions(profile)
    changes = next(a for a in answers if a["question"] == "What has changed since the last run?")

    assert "no material changes" in changes["answer"].lower()


def test_answers_never_include_a_value_when_state_is_not_available():
    """Regression-shaped: the exact bug class already found once in
    assemble.py (state=available with no value) must never leak into
    synthesis either -- an unavailable claim's answer text must not somehow
    surface a stale/placeholder value."""
    profile = make_profile(legal_name=available_claim("EQUINOR ASA"))  # official_site left NOT_AVAILABLE

    answers = answer_business_questions(profile)
    site_answer = next(a for a in answers if a["question"] == "What is the official website?")

    assert site_answer["state"] == "not_available"
    assert "http" not in site_answer["answer"]


def test_profile_question_answers_from_the_free_registry_identity_fields():
    """Industry, employee count and operating status come free from the
    registry record we already read (registry_extras.universe_identity_facts)
    -- they answer real questions a job seeker or investor actually asks, and
    cost nothing extra to synthesize since the claims are already in hand."""
    from src.models.profile import Claim

    profile = make_profile(legal_name=available_claim("SANDNES ELEKTRISKE AS"))
    profile.legal_identity.industry = available_claim("43.210 Elektrisk installasjonsarbeid")
    profile.legal_identity.employee_count = available_claim("11")
    profile.legal_identity.operating_status = available_claim("Active")

    answers = answer_business_questions(profile)
    by_q = {a["question"]: a for a in answers}
    profile_answer = next(a for q, a in by_q.items() if "do" in q.lower() and "what" in q.lower())

    assert "Elektrisk installasjonsarbeid" in profile_answer["answer"]
    assert profile_answer["state"] == EvidenceState.AVAILABLE.value

    status_answer = next(a for q, a in by_q.items() if "operating" in q.lower() or "still active" in q.lower())
    assert "Active" in status_answer["answer"]


def test_new_questions_degrade_honestly_when_the_registry_fields_are_absent():
    """A company outside Builderr's universe manifest has no such claims --
    the questions must still be answered, honestly, never guessed."""
    profile = make_profile(legal_name=available_claim("SOME COMPANY AS"))

    answers = answer_business_questions(profile)

    assert all("answer" in a and "state" in a for a in answers)
    assert not any(a["state"] == EvidenceState.AVAILABLE.value and a["answer"].strip() == "" for a in answers)


def test_founded_question_answers_from_the_live_registry_founding_date():
    profile = make_profile(legal_name=available_claim("ACME AS"))
    profile.legal_identity.founded_date = available_claim("2012-07-01")

    answer = next(a for a in answer_business_questions(profile) if "founded" in a["question"].lower())

    assert answer["answer"] == "Founded 2012-07-01"
    assert answer["state"] == EvidenceState.AVAILABLE.value


def test_founded_question_is_honest_when_no_date_is_known():
    answer = next(
        a for a in answer_business_questions(make_profile(legal_name=available_claim("ACME AS")))
        if "founded" in a["question"].lower()
    )

    assert answer["state"] == EvidenceState.NOT_AVAILABLE.value
