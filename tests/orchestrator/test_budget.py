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
