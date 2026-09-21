"""
Tests for src/storage/snapshots.py -- see docs/component-specs.md.

test_running_twice_produces_two_snapshots_not_one_overwritten is THE
idempotency hard-gate test. Do not weaken or skip it once implemented -- see
CLAUDE.md non-negotiables.
"""
from datetime import datetime, timezone

from src.storage.snapshots import SnapshotStore, diff_material_changes
from tests.conftest import available_claim, make_profile


def test_running_twice_produces_two_snapshots_not_one_overwritten(tmp_path):
    store = SnapshotStore(tmp_path)
    first = make_profile(
        org_number="923609016",
        run_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
        legal_name=available_claim("EQUINOR ASA"),
        is_first_run=True,
    )
    second = make_profile(
        org_number="923609016",
        run_timestamp=datetime(2026, 1, 2, tzinfo=timezone.utc),
        legal_name=available_claim("EQUINOR ASA"),
        is_first_run=False,
        previous_run_timestamp=first.run_timestamp,
    )

    store.append(first)
    store.append(second)

    history = store.history("923609016")
    assert len(history) == 2
    assert history[0].run_timestamp == first.run_timestamp
    assert history[0].legal_identity.legal_name.value == "EQUINOR ASA"  # untouched
    assert history[1].run_timestamp == second.run_timestamp

    assert store.latest("923609016").run_timestamp == second.run_timestamp


def test_latest_returns_none_when_no_snapshot_exists(tmp_path):
    store = SnapshotStore(tmp_path)
    assert store.latest("999999999") is None


def test_snapshots_for_different_org_numbers_are_isolated(tmp_path):
    store = SnapshotStore(tmp_path)
    store.append(make_profile(org_number="111111111"))
    store.append(make_profile(org_number="222222222"))

    assert len(store.history("111111111")) == 1
    assert len(store.history("222222222")) == 1


def test_diffing_ignores_trivial_non_material_changes():
    previous = make_profile(
        legal_name=available_claim("  Equinor ASA  "),
        official_site=available_claim("equinor.com"),
    )
    new = make_profile(
        legal_name=available_claim("Equinor ASA"),  # whitespace-only diff
        official_site=available_claim("equinor.com"),  # unchanged, new retrieved_at
    )

    assert diff_material_changes(previous, new) == []


def test_diffing_detects_material_legal_name_change():
    previous = make_profile(legal_name=available_claim("Old Name AS"))
    new = make_profile(legal_name=available_claim("New Name AS"))

    changes = diff_material_changes(previous, new)
    assert any("legal_name" in c for c in changes)


def test_diffing_detects_hiring_signal_added():
    previous = make_profile(hiring_signals=[])
    new = make_profile(hiring_signals=[available_claim("Hiring: Backend Engineer")])

    changes = diff_material_changes(previous, new)
    assert any("hiring_signal" in c for c in changes)


def test_diffing_against_no_previous_snapshot_is_empty():
    new = make_profile(legal_name=available_claim("Some AS"))
    assert diff_material_changes(None, new) == []


def test_diffing_detects_a_new_reporting_period_even_when_the_amount_is_unchanged():
    """Real evaluator feedback: "a new financial reporting period was missed
    when the amount stayed the same." A company can file a new year's accounts
    that happen to report an identical revenue/net-result figure -- that's
    still new information (a new filing exists) and must not be silently
    dropped just because diffing only compared the formatted value string."""
    previous = make_profile(
        annual_latest=available_claim("Revenue: 100,000 NOK", reporting_period="FY2024")
    )
    new = make_profile(
        annual_latest=available_claim("Revenue: 100,000 NOK", reporting_period="FY2025")
    )

    changes = diff_material_changes(previous, new)
    assert any("annual_accounts.latest" in c and "FY2024" in c and "FY2025" in c for c in changes)


def test_diffing_does_not_flag_a_reporting_period_change_when_both_are_none():
    previous = make_profile(annual_latest=available_claim("Revenue: 100,000 NOK"))
    new = make_profile(annual_latest=available_claim("Revenue: 100,000 NOK"))

    assert diff_material_changes(previous, new) == []


def _with_status(profile, status):
    profile.legal_identity.operating_status = available_claim(status) if status is not None else None
    return profile


def test_a_company_going_bankrupt_is_a_material_change():
    old = _with_status(make_profile(legal_name=available_claim("ACME AS")), "Active")
    new = _with_status(make_profile(legal_name=available_claim("ACME AS")), "Bankrupt")

    assert "operating_status: 'Active' -> 'Bankrupt'" in diff_material_changes(old, new)


def test_operating_status_missing_from_an_older_snapshot_is_not_a_change():
    """Snapshots written before the field existed have none -- treating that
    as a change would flag every company on the first run after upgrading."""
    old = _with_status(make_profile(legal_name=available_claim("ACME AS")), None)
    new = _with_status(make_profile(legal_name=available_claim("ACME AS")), "Active")

    assert not any("operating_status" in c for c in diff_material_changes(old, new))


def test_unchanged_operating_status_is_not_a_change():
    old = _with_status(make_profile(legal_name=available_claim("ACME AS")), "Active")
    new = _with_status(make_profile(legal_name=available_claim("ACME AS")), "Active")

    assert not any("operating_status" in c for c in diff_material_changes(old, new))


# ---- registry facts that change between runs -----------------------------


def _with_registry_fields(profile, **fields):
    for name, value in fields.items():
        setattr(profile.legal_identity, name, available_claim(value) if value is not None else None)
    return profile


def test_a_changed_employee_count_is_a_material_change():
    """Builderr's own refresh sample expects exactly this: the registry's
    employee count going from 2 to 3 between two versions of a profile."""
    old = _with_registry_fields(make_profile(legal_name=available_claim("ACME AS")), employee_count="2")
    new = _with_registry_fields(make_profile(legal_name=available_claim("ACME AS")), employee_count="3")

    assert "employee_count: '2' -> '3'" in diff_material_changes(old, new)


def test_a_changed_legal_form_or_industry_is_a_material_change():
    old = _with_registry_fields(
        make_profile(legal_name=available_claim("ACME AS")), legal_form="AS", industry="62.010 Programmeringstjenester"
    )
    new = _with_registry_fields(
        make_profile(legal_name=available_claim("ACME AS")), legal_form="ASA", industry="62.020 Konsulentvirksomhet"
    )

    changes = diff_material_changes(old, new)

    assert "legal_form: 'AS' -> 'ASA'" in changes
    assert "industry: '62.010 Programmeringstjenester' -> '62.020 Konsulentvirksomhet'" in changes


def test_registry_fields_missing_from_either_snapshot_are_not_a_change():
    """Snapshots written before these claims existed have none; flagging that as
    a change would report every company on the first run after upgrading."""
    with_fields = _with_registry_fields(
        make_profile(legal_name=available_claim("ACME AS")), employee_count="3", legal_form="AS", industry="62.010 X"
    )
    without = make_profile(legal_name=available_claim("ACME AS"))

    assert diff_material_changes(without, with_fields) == []
    assert diff_material_changes(with_fields, without) == []


def test_unchanged_registry_fields_are_not_a_change():
    old = _with_registry_fields(make_profile(legal_name=available_claim("ACME AS")), employee_count="3", legal_form="AS")
    new = _with_registry_fields(make_profile(legal_name=available_claim("ACME AS")), employee_count="3", legal_form="AS")

    assert diff_material_changes(old, new) == []
