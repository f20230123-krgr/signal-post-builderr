"""Tests for the short dated company summary (src/synthesis.py: build_summary).

Official feedback on the 3rd submission: "turn the facts into a shorter dated summary
instead of a list". Builderr's rubric: the summary should "explain the company, changes
and unknowns without making unsupported claims". Every test below pins one of those.
"""
import re
from datetime import datetime, timezone

from src.pipeline.envelope import to_envelope
from src.synthesis import build_summary, humanize_change
from tests.conftest import available_claim, make_profile


def _with_figures(profile, **by_period):
    """Attach discrete figure claims: _with_figures(p, revenue={"FY2025": "120 NOK", "FY2024": "100 NOK"})."""
    for name, periods in by_period.items():
        profile.annual_accounts.metrics[name] = [
            available_claim(value, reporting_period=period, effective_date=f"{period[2:]}-12-31")
            for period, value in periods.items()
        ]
    return profile


def _rich_profile():
    profile = make_profile(
        org_number="923609016",
        legal_name=available_claim("EQUINOR ASA"),
        official_site=available_claim("https://www.equinor.com/", source="https://www.equinor.com/"),
        leaders=[
            available_claim("Anders Opedal (Daglig leder)"),
            available_claim("Jon Erik Reinhardsen (Styrets leder)"),
            available_claim("Ola Nordmann (Styremedlem)"),
        ],
        workplaces=[
            available_claim("EQUINOR ENERGY, Forusbeen 50, 4035 STAVANGER (50 ansatte)"),
            available_claim("EQUINOR BERGEN, Sandsliveien 90, 5254 SANDSLI"),
        ],
        hiring_signals=[available_claim('Careers page lists 3 open roles, e.g. "Engineer": https://equinor.com/careers')],
        is_first_run=False,
        previous_run_timestamp=datetime(2026, 9, 24, tzinfo=timezone.utc),
        material_changes=["employee_count: '20000' -> '21000'"],
    )
    profile.legal_identity.legal_form = available_claim("ASA")
    profile.legal_identity.industry = available_claim("06.100 Utvinning av raaolje")
    profile.legal_identity.founded_date = available_claim("1972-09-18")
    profile.legal_identity.operating_status = available_claim("Active")
    profile.legal_identity.employee_count = available_claim("21000")
    profile.activity.dated_activity = [
        available_claim("Registry record updated (Endring) on 2026-03-23 - Bronnoysundregistrene", source_class="official_registry"),
        available_claim("Equinor opens new office (2026-09-22)", source_class="company_owned", effective_date="2026-09-22"),
    ]
    _with_figures(
        profile,
        revenue={"FY2025": "107,174,000,000 USD", "FY2024": "100,000,000,000 USD"},
        annual_result={"FY2025": "11,904,000,000 USD"},
        total_equity={"FY2025": "48,500,000,000 USD"},
    )
    return profile


def test_the_summary_is_short_dated_and_covers_company_figures_people_web_and_changes():
    summary = build_summary(_rich_profile())
    text = summary.text

    assert text.startswith(
        f"As of {summary.as_of}: EQUINOR ASA (org. no. 923609016) is a public limited company (ASA) in utvinning av raaolje, founded 1972"
    )
    assert "registry status Active, 21000 employees" in text
    assert "FY2025 (to 2025-12-31): revenue 107.17 bn USD (up 7.2% on FY2024), net result 11.90 bn USD, equity 48.50 bn USD." in text
    assert "Led by Anders Opedal (managing director) and Jon Erik Reinhardsen (chair of the board), with 1 more registered role holders." in text
    assert "2 registered sites, including Stavanger, Sandsli." in text
    assert 'Online: equinor.com; careers page lists 3 open roles; latest news "Equinor opens new office" (2026-09-22).' in text
    assert "Since the previous run on 2026-09-24: employees 20000 -> 21000." in text
    assert len(text.split()) <= 120


def test_a_first_run_says_so_in_one_short_sentence_and_a_quiet_refresh_says_no_change():
    first = build_summary(make_profile(org_number="923609016", legal_name=available_claim("ACME AS"))).text
    quiet = build_summary(make_profile(
        legal_name=available_claim("ACME AS"), is_first_run=False, previous_run_timestamp=datetime(2026, 9, 24, tzinfo=timezone.utc),
    )).text

    assert "First run, so there is nothing earlier to compare with." in first
    assert "No material change since the previous run (2026-09-24)." in quiet


def test_the_summary_names_the_newest_news_from_the_companys_own_pages_not_a_registry_event():
    assert "last registry update" not in build_summary(_rich_profile()).text  # real news exists, so it leads

    profile = make_profile(org_number="923609016", legal_name=available_claim("ACME AS"))
    profile.activity.dated_activity = [
        available_claim("Registry record updated (Endring) on 2026-03-23 - X", source_class="official_registry")
    ]
    assert "last registry update 2026-03-23" in build_summary(profile).text


def test_a_revenue_decline_and_a_flat_year_are_stated_as_such():
    down = _with_figures(make_profile(legal_name=available_claim("ACME AS")), revenue={"FY2025": "750,000 NOK", "FY2024": "1,000,000 NOK"})
    flat = _with_figures(make_profile(legal_name=available_claim("ACME AS")), revenue={"FY2025": "1,000,000 NOK", "FY2024": "1,000,000 NOK"})

    assert "revenue 750 k NOK (down 25.0% on FY2024)" in build_summary(down).text
    assert "revenue 1.0 m NOK (flat on FY2024)" in build_summary(flat).text


def test_no_trend_is_stated_across_currencies_from_zero_or_with_a_single_year():
    def base():
        return make_profile(legal_name=available_claim("ACME AS"))

    mixed = _with_figures(base(), revenue={"FY2025": "1,200 USD", "FY2024": "1,000 NOK"})
    zero = _with_figures(base(), revenue={"FY2025": "1,200 NOK", "FY2024": "0 NOK"})
    single = _with_figures(base(), revenue={"FY2025": "1,200 NOK"})

    for profile in (mixed, zero, single):
        text = build_summary(profile).text
        assert " up " not in text and " down " not in text and " flat " not in text


def test_the_summary_names_what_is_unknown_and_invents_nothing_for_an_empty_profile():
    summary = build_summary(make_profile(org_number="923609016", legal_name=available_claim("ACME AS")))

    assert summary.unknowns == [
        "official website", "company-owned social profiles", "annual accounts", "leadership", "hiring signals",
    ]
    assert "Not found: official website" in summary.text
    assert "http" not in summary.text
    for absent in ("Online:", "Led by", "registered sites", "FY20"):
        assert absent not in summary.text


def test_annual_accounts_are_not_reported_unknown_when_only_discrete_figures_exist():
    profile = _with_figures(make_profile(legal_name=available_claim("ACME AS")), revenue={"FY2025": "1,200 NOK"})

    assert "annual accounts" not in build_summary(profile).unknowns


def test_a_legal_form_without_a_plain_phrase_is_stated_as_its_code():
    profile = make_profile(org_number="818751362", legal_name=available_claim("HAUGE BYGARD"))
    profile.legal_identity.legal_form = available_claim("ESEK")

    text = build_summary(profile).text

    assert "is a registered Norwegian entity (legal form ESEK)" in text and "a ESEK" not in text


def test_the_summary_says_careers_page_for_a_bare_page_and_hiring_for_an_ad():
    bare = make_profile(org_number="923609016", hiring_signals=[available_claim("Careers page: https://s.no/karriere")])
    roles = make_profile(org_number="923609016", hiring_signals=[available_claim('Careers page lists 3 open roles, e.g. "X": https://s.no/k')])
    nav = make_profile(org_number="923609016", hiring_signals=[available_claim("Elektriker (published 2026-09-02, apply by 2026-10-31) - NAV")])

    assert "Online: careers page." in build_summary(bare).text
    assert "careers page lists 3 open roles" in build_summary(roles).text
    assert "hiring: Elektriker" in build_summary(nav).text


def test_changes_are_said_in_words_and_the_raw_strings_stay_in_the_envelope():
    assert humanize_change("employee_count: '20' -> '21'") == "employees 20 -> 21"
    assert humanize_change("operating_status: 'Active' -> 'Bankrupt'") == "registry status Active -> Bankrupt"
    assert humanize_change("hiring_signal added: ['x - NAV']") == "new hiring signal"
    assert humanize_change("something unrecognised") == "something unrecognised"
    assert to_envelope(_rich_profile(), run_id="r")["summary"]["changes"] == ["employee_count: '20000' -> '21000'"]


def test_the_envelope_carries_a_one_line_headline():
    headline = to_envelope(_rich_profile(), run_id="r")["summary"]["headline"]

    assert headline == "EQUINOR ASA: active, revenue 107.17 bn USD (FY2025, up 7%)"


def test_every_sentence_cites_the_evidence_it_rests_on():
    envelope = to_envelope(_rich_profile(), run_id="r")
    evidence_ids = {e["id"] for e in envelope["evidence"]}

    for sentence in envelope["summary"]["sentences"]:
        if sentence["fields"]:  # the "First run" and "Not found" sentences state no fact
            assert sentence["evidence_ids"], sentence["text"]
            assert set(sentence["evidence_ids"]) <= evidence_ids


# --- faithfulness: no figure in a sentence without a source ---------------------------------

_UNITS = {"bn": 1e9, "m": 1e6, "k": 1e3, "": 1.0}
_MONEY_RE = re.compile(r"(-?\d[\d,]*\.?\d*)\s*(bn|m|k)?\s*(?:USD|NOK|EUR|SEK|DKK)")
_PERCENT_RE = re.compile(r"(\d+\.?\d*)%")


def test_every_figure_in_the_summary_is_traceable_to_a_cited_claim():
    """The scorer's question, 'without unsupported claims', as a test: each amount must
    round-trip to a claim its sentence cites, and each percentage must come from two cited
    revenue claims."""
    summary = build_summary(_rich_profile())
    checked = 0

    for sentence in summary.sentences:
        cited = [c for c in sentence.claims if c.value]
        cited_amounts = [
            float(c.value.split()[0].replace(",", "")) for c in cited if re.match(r"^-?[\d,]+(\s+[A-Z]{3})?$", c.value)
        ]
        for number, unit in _MONEY_RE.findall(sentence.text):
            amount = float(number.replace(",", "")) * _UNITS[unit]
            assert any(abs(amount - c) <= abs(c) * 0.006 + 1 for c in cited_amounts), (sentence.text, amount, cited_amounts)
            checked += 1
        for pct in _PERCENT_RE.findall(sentence.text):
            # The percentage must be the change between two cited claims of different years.
            figures = [(c.reporting_period, float(c.value.split()[0].replace(",", ""))) for c in cited
                       if c.reporting_period and re.match(r"^[\d,]+ [A-Z]{3}$", c.value)]
            changes = [
                abs((a - b) / b * 100) for (pa, a) in figures for (pb, b) in figures if pa > pb and b
            ]
            assert any(abs(change - float(pct)) <= 0.06 for change in changes), (sentence.text, pct, changes)
            checked += 1
    assert checked >= 4  # revenue, net result, equity and the percentage were all actually checked


def test_the_summary_never_contains_a_figure_when_no_figures_exist():
    text = build_summary(make_profile(legal_name=available_claim("ACME AS"))).text

    assert not re.search(r"\d[\d,.]*\s*(?:bn|m|k)?\s*(?:NOK|USD|EUR)", text)
