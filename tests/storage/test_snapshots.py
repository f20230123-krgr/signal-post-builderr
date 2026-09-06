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
