"""Discrete financial claims from Regnskapsregisteret filings.

Builderr's reference schema carries revenue, operating result, result before tax,
annual result, assets, equity and debt per filed year, each with its period, and a
link to the filed accounts. A figure the filing doesn't state is omitted, never zero.
"""
import json
from datetime import datetime, timezone

import httpx

from src.orchestrator.budget import BudgetGovernor
from src.pipeline.assemble import assemble
from src.pipeline.envelope import to_envelope
from src.pipeline.registry_extras import (
    ACCOUNT_METRIC_FIELDS,
    MAX_FILINGS_WITH_METRICS,
    _entry_slices,
    _metric_span,
    fetch_registry_extras,
)
from tests.pipeline.test_registry_extras import _client_for, _entity

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def _filing(year, revenue=1_000_000.0, kind="SELSKAP", **extra):
    entry = {
        "id": year, "journalnr": f"{year}0001", "regnskapstype": kind, "valuta": "NOK",
        "regnskapsperiode": {"fraDato": f"{year}-01-01", "tilDato": f"{year}-12-31"},
        "resultatregnskapResultat": {
            "aarsresultat": 90_000.0, "ordinaertResultatFoerSkattekostnad": 120_000.0,
            "driftsresultat": {"driftsresultat": 150_000.0, "driftsinntekter": {"sumDriftsinntekter": revenue}},
        },
        "eiendeler": {"sumEiendeler": 5_000_000.0},
        "egenkapitalGjeld": {"egenkapital": {"sumEgenkapital": 2_000_000.0}, "gjeldOversikt": {"sumGjeld": 3_000_000.0}},
    }
    entry.update(extra)
    return entry


def _facts_for(filings):
    body = json.dumps(filings)

    def handler(request):
        return httpx.Response(200, text=body) if "regnskapsregisteret" in str(request.url) else httpx.Response(404)

    facts, _ = fetch_registry_extras(_entity(), BudgetGovernor(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    return facts, body


def _metrics(facts):
    return {(f.field_name, f.reporting_period): f for f in facts if f.field_name in ACCOUNT_METRIC_FIELDS}


def test_each_figure_is_its_own_claim_with_period_end_date_and_currency():
    facts, _ = _facts_for([_filing(2025, revenue=1_021_375_635.0)])

    m = _metrics(facts)

    assert m[("revenue", "FY2025")].value == "1,021,375,635 NOK"
    assert m[("revenue", "FY2025")].effective_date == "2025-12-31"
    assert m[("operating_result", "FY2025")].value == "150,000 NOK"
    assert m[("profit_before_tax", "FY2025")].value == "120,000 NOK"
    assert m[("annual_result", "FY2025")].value == "90,000 NOK"
    assert m[("total_assets", "FY2025")].value == "5,000,000 NOK"
    assert m[("total_equity", "FY2025")].value == "2,000,000 NOK"
    assert m[("total_debt", "FY2025")].value == "3,000,000 NOK"


def test_a_figure_the_filing_does_not_state_is_omitted_never_zero():
    filing = _filing(2025)
    del filing["eiendeler"]
    filing["resultatregnskapResultat"]["driftsresultat"]["driftsinntekter"] = {}

    m = _metrics(_facts_for([filing])[0])

    assert ("total_assets", "FY2025") not in m and ("revenue", "FY2025") not in m
    assert all("0 NOK" != f.value for f in m.values())


def test_every_span_is_the_filings_own_text_for_that_year():
    facts, body = _facts_for([_filing(2025, revenue=111.0), _filing(2024, revenue=222.0)])

    m = _metrics(facts)

    assert m[("revenue", "FY2025")].evidence_span == '"sumDriftsinntekter": 111.0'
    assert m[("revenue", "FY2024")].evidence_span == '"sumDriftsinntekter": 222.0'
    pdf = next(f for f in facts if f.field_name == "annual_report_pdf" and f.reporting_period == "FY2025")
    assert pdf.evidence_span == '"journalnr": "20250001"'
    for fact in [*m.values(), *(f for f in facts if f.field_name == "annual_report_pdf")]:
        assert fact.evidence_span in body


def test_only_the_newest_filings_get_figures_and_a_pdf_link_for_each():
    facts, _ = _facts_for([_filing(y) for y in (2022, 2023, 2024, 2025)])

    periods = {f.reporting_period for f in facts if f.field_name in ACCOUNT_METRIC_FIELDS}

    assert periods == {"FY2025", "FY2024"} and MAX_FILINGS_WITH_METRICS == 2
    pdfs = {f.reporting_period: f.value for f in facts if f.field_name == "annual_report_pdf"}
    assert pdfs == {
        "FY2025": "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/997770234/2025",
        "FY2024": "https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/997770234/2024",
    }


def test_the_companys_own_accounts_are_preferred_over_the_groups_for_the_same_year():
    facts, _ = _facts_for([_filing(2025, revenue=999.0, kind="KONSERN"), _filing(2025, revenue=111.0, kind="SELSKAP")])

    assert _metrics(facts)[("revenue", "FY2025")].value == "111 NOK"


def test_a_group_filing_is_used_when_it_is_the_only_one():
    facts, _ = _facts_for([_filing(2025, revenue=999.0, kind="KONSERN")])

    assert _metrics(facts)[("revenue", "FY2025")].value == "999 NOK"


def test_the_legacy_combined_value_is_still_published_with_its_end_date():
    facts, _ = _facts_for([_filing(2025)])

    latest = next(f for f in facts if f.field_name == "annual_accounts_latest")

    assert latest.value.startswith("Revenue: 1,000,000 NOK") and latest.effective_date == "2025-12-31"


def test_slices_split_the_array_into_each_filings_own_text():
    body = '[{"a": 1, "k": {"x": 2}}, {"a": 3}]'

    assert _entry_slices(body) == ['{"a": 1, "k": {"x": 2}}', '{"a": 3}']
    assert _entry_slices("[]") == [] and _entry_slices("not json") == []
    assert _metric_span('{"a": 1.5, "b": -2}', "b") == '"b": -2'
    assert _metric_span('{"a": 1}', "zzz") is None


def test_the_real_equinor_filing_yields_quoted_figures():
    facts, _ = fetch_registry_extras(_entity(org_number="923609016", legal_name="EQUINOR ASA"), BudgetGovernor(), client=_client_for("923609016"))

    m = _metrics(facts)

    assert any(f.field_name == "revenue" and f.value.endswith("USD") for f in m.values())
    assert all(f.evidence_span for f in m.values())


def test_figures_flow_through_assembly_into_the_envelope_with_periods_and_evidence():
    facts, _ = _facts_for([_filing(2025), _filing(2024)])
    profile = assemble(_entity(), facts, None, now=lambda: NOW)

    envelope = to_envelope(profile, run_id="r")
    revenue = [c for c in envelope["claims"] if c["field"] == "revenue"]

    assert [c["reporting_period"] for c in revenue] == ["FY2025", "FY2024"]
    assert revenue[0]["effective_at"] == "2025-12-31"
    evidence = {e["id"]: e for e in envelope["evidence"]}
    assert evidence[revenue[0]["evidence_ids"][0]]["claim_span"].startswith('"sumDriftsinntekter"')
    assert {c["field"] for c in envelope["claims"]} >= set(ACCOUNT_METRIC_FIELDS)
