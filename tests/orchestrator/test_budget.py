"""Tests for src/orchestrator/budget.py -- see docs/component-specs.md."""
from src.orchestrator.budget import BudgetGovernor, BudgetLimits


def _clock(times):
    it = iter(times)
    return lambda: next(it)


def test_governor_refuses_spend_once_request_budget_hits_zero():
    gov = BudgetGovernor(BudgetLimits(max_requests=2, max_spend_usd=10.0, max_wall_clock_seconds=1000))

    assert gov.can_spend_request() is True
    gov.record_request()
    assert gov.can_spend_request() is True
    gov.record_request()

    assert gov.can_spend_request() is False


def test_governor_refuses_spend_once_dollar_budget_hits_zero():
    gov = BudgetGovernor(BudgetLimits(max_requests=1000, max_spend_usd=1.0, max_wall_clock_seconds=1000))

    assert gov.can_spend_usd(0.6) is True
    gov.record_spend(0.6)
    assert gov.can_spend_usd(0.5) is False
    assert gov.can_spend_usd(0.4) is True


def test_governor_refuses_spend_once_wall_clock_budget_hits_zero():
    limits = BudgetLimits(max_requests=1000, max_spend_usd=10.0, max_wall_clock_seconds=60)
    clock = _clock([0.0, 0.0, 61.0, 61.0])
    gov = BudgetGovernor(limits, clock=clock)

    assert gov.can_spend_request() is True
    assert gov.can_spend_request() is False


def test_near_exhaustion_triggers_should_degrade_on_requests():
    gov = BudgetGovernor(BudgetLimits(max_requests=100, max_spend_usd=10.0, max_wall_clock_seconds=1000))
    for _ in range(89):
        gov.record_request()
    assert gov.should_degrade() is False

    gov.record_request()  # 90/100 used -> at the 90% degrade threshold
    assert gov.should_degrade() is True


def test_near_exhaustion_triggers_should_degrade_on_wall_clock():
    limits = BudgetLimits(max_requests=1000, max_spend_usd=10.0, max_wall_clock_seconds=100)
    clock = _clock([0.0, 91.0])
    gov = BudgetGovernor(limits, clock=clock)
    assert gov.should_degrade() is True


def test_should_degrade_false_when_nothing_close_to_exhausted():
    gov = BudgetGovernor(BudgetLimits(max_requests=100, max_spend_usd=10.0, max_wall_clock_seconds=1000))
    gov.record_request()
    gov.record_spend(0.5)
    assert gov.should_degrade() is False


def test_reporting_properties_reflect_recorded_usage():
    gov = BudgetGovernor(BudgetLimits(max_requests=100, max_spend_usd=10.0, max_wall_clock_seconds=1000))
    gov.record_request()
    gov.record_request()
    gov.record_spend(1.5)

    assert gov.requests_used == 2
    assert gov.spend_used == 1.5
    assert gov.elapsed_seconds >= 0


# ---- real (wire) request counting ----------------------------------------
# Builderr counts "2,000 total outbound requests, including redirects and
# retries". Stages record one request per LOGICAL fetch; the wire counter sees
# every hop. The governor must trust whichever is higher.


def test_wire_requests_beyond_the_logical_count_are_what_the_governor_enforces():
    wire = {"n": 0}
    gov = BudgetGovernor(
        BudgetLimits(max_requests=100, max_spend_usd=10.0, max_wall_clock_seconds=1000),
        wire_counter=lambda: wire["n"],
    )
    gov.record_request()  # one logical fetch...
    wire["n"] = 95        # ...that really cost 95 requests (redirects, retries)

    assert gov.requests_used == 95
    assert gov.should_degrade() is True
    assert gov.can_spend_request() is True

    wire["n"] = 100
    assert gov.can_spend_request() is False


def test_the_logical_count_still_applies_when_it_is_higher_than_the_wire_count():
    """E.g. a headless-browser render records a request but isn't an httpx call."""
    wire = {"n": 0}
    gov = BudgetGovernor(
        BudgetLimits(max_requests=10, max_spend_usd=10.0, max_wall_clock_seconds=1000),
        wire_counter=lambda: wire["n"],
    )
    for _ in range(10):
        gov.record_request()

    assert gov.requests_used == 10
    assert gov.can_spend_request() is False


def test_requests_made_before_the_governor_existed_are_not_charged_to_it():
    wire = {"n": 500}
    gov = BudgetGovernor(
        BudgetLimits(max_requests=100, max_spend_usd=10.0, max_wall_clock_seconds=1000),
        wire_counter=lambda: wire["n"],
    )
    assert gov.requests_used == 0

    wire["n"] = 512
    assert gov.requests_used == 12


def test_startup_requests_can_be_charged_to_the_first_governor():
    """The key check and the universe download happen before chunk 1's
    governor exists, but they are part of the same evaluated run."""
    wire = {"n": 3}
    gov = BudgetGovernor(
        BudgetLimits(max_requests=100, max_spend_usd=10.0, max_wall_clock_seconds=1000),
        wire_counter=lambda: wire["n"],
        wire_baseline=0,
    )

    assert gov.requests_used == 3


def test_a_safety_margin_leaves_headroom_for_requests_already_in_flight():
    """Concurrent workers can each start one last fetch (and its redirects)
    after the check passes; the margin keeps the real total under the limit."""
    gov = BudgetGovernor(
        BudgetLimits(max_requests=100, max_spend_usd=10.0, max_wall_clock_seconds=1000, request_safety_margin=20)
    )
    for _ in range(79):
        gov.record_request()
    assert gov.can_spend_request() is True

    gov.record_request()  # 80 used; only the reserved 20 are left
    assert gov.can_spend_request() is False


def test_the_margin_defaults_to_zero_so_the_limit_means_what_it_says():
    assert BudgetLimits().request_safety_margin == 0
