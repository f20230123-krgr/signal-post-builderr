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
    profile.run_timestamp = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return profile


def test_the_summary_is_short_dated_and_covers_company_figures_people_web_and_changes():
    summary = build_summary(_rich_profile())
    text = summary.text

    assert text.startswith(
        f"As of {summary.as_of}: EQUINOR ASA (org. no. 923609016) is a public limited company (ASA) in extraction of crude petroleum, founded 1972"
    )
    assert "registry status Active, 21000 employees" in text
    assert (
        "In FY2025 (year to 2025-12-31), revenue grew 7.2% on FY2024 to 107.17 bn USD and the company made "
        "a net profit of 11.90 bn USD (11.1% of revenue); equity was 48.50 bn USD." in text
    )
    assert "Led by Anders Opedal (managing director) and Jon Erik Reinhardsen (chair of the board), with 1 more registered role holder." in text
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

    assert "revenue fell 25.0% on FY2024 to 750 k NOK" in build_summary(down).text
    assert "revenue was flat on FY2024 at 1.0 m NOK" in build_summary(flat).text


def test_no_trend_is_stated_across_currencies_from_zero_or_with_a_single_year():
    def base():
        return make_profile(legal_name=available_claim("ACME AS"))

    mixed = _with_figures(base(), revenue={"FY2025": "1,200 USD", "FY2024": "1,000 NOK"})
    zero = _with_figures(base(), revenue={"FY2025": "1,200 NOK", "FY2024": "0 NOK"})
    single = _with_figures(base(), revenue={"FY2025": "1,200 NOK"})

    for profile in (mixed, zero, single):
        text = build_summary(profile).text
        assert not any(word in text for word in (" grew ", " fell ", " flat ", " up ", " down "))
        assert "revenue was 1,200" in text


def test_the_financial_trend_is_explained_in_words_not_listed():
    """Official feedback: "explain the financial trend instead of listing values"."""
    def acme(**figures):
        return build_summary(_with_figures(make_profile(legal_name=available_claim("ACME AS")), **figures)).text

    turnaround = acme(
        revenue={"FY2025": "1,100,000 NOK", "FY2024": "1,000,000 NOK"},
        annual_result={"FY2025": "50,000 NOK", "FY2024": "-80,000 NOK"},
        total_equity={"FY2025": "400,000 NOK", "FY2024": "350,000 NOK"},
    )
    assert (
        "revenue grew 10.0% on FY2024 to 1.1 m NOK and the company made a net profit of 50 k NOK (4.5% of revenue), "
        "turning round from a loss of 80 k NOK in FY2024; equity rose to 400 k NOK." in turnaround
    )

    into_loss = acme(
        revenue={"FY2025": "900,000 NOK", "FY2024": "1,000,000 NOK"},
        annual_result={"FY2025": "-30,000 NOK", "FY2024": "20,000 NOK"},
        total_equity={"FY2025": "-10,000 NOK"},
    )
    assert "a net loss of 30 k NOK (3.3% of revenue), after a profit of 20 k NOK in FY2024" in into_loss
    assert "equity was negative (-10 k NOK)" in into_loss

    smaller_loss = acme(annual_result={"FY2025": "-30,000 NOK", "FY2024": "-90,000 NOK"})
    assert "In FY2025 (year to 2025-12-31), the company made a net loss of 30 k NOK, a smaller loss than the 90 k NOK of FY2024." in smaller_loss

    lower_profit = acme(annual_result={"FY2025": "60,000 NOK", "FY2024": "90,000 NOK"}, total_equity={"FY2025": "100,000 NOK", "FY2024": "120,000 NOK"})
    assert "a net profit of 60 k NOK, down from 90 k NOK in FY2024; equity fell to 100 k NOK." in lower_profit


def test_the_employee_count_carries_the_date_the_registry_recorded_it():
    profile = make_profile(legal_name=available_claim("ACME AS"))
    profile.legal_identity.employee_count = available_claim("201", effective_date="2026-09-14")

    assert "201 employees (registered 2026-09-14)" in build_summary(profile).text


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
    profile.legal_identity.legal_form = available_claim("XYZ")
    esek = make_profile(org_number="818751362", legal_name=available_claim("HAUGE BYGARD"))
    esek.legal_identity.legal_form = available_claim("ESEK")

    text = build_summary(profile).text

    assert "is a registered Norwegian entity (legal form XYZ)" in text and "a XYZ" not in text
    assert "is an owner-section condominium (ESEK)" in build_summary(esek).text


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
    def acme(**figures):
        return _with_figures(make_profile(legal_name=available_claim("ACME AS")), **figures)

    profiles = [
        _rich_profile(),
        acme(revenue={"FY2025": "1,100,000 NOK", "FY2024": "1,000,000 NOK"},
             annual_result={"FY2025": "50,000 NOK", "FY2024": "-80,000 NOK"},
             total_equity={"FY2025": "400,000 NOK", "FY2024": "350,000 NOK"}),
        acme(revenue={"FY2025": "900,000 NOK", "FY2024": "1,000,000 NOK"},
             annual_result={"FY2025": "-30,000 NOK", "FY2024": "20,000 NOK"}, total_equity={"FY2025": "-10,000 NOK"}),
        acme(annual_result={"FY2025": "-30,000 NOK", "FY2024": "-90,000 NOK"}),
    ]
    sentences = [sentence for profile in profiles for sentence in build_summary(profile).sentences]
    checked = 0

    for sentence in sentences:
        cited = [c for c in sentence.claims if c.value]
        cited_amounts = [
            float(c.value.split()[0].replace(",", "")) for c in cited if re.match(r"^-?[\d,]+(\s+[A-Z]{3})?$", c.value)
        ]
        for number, unit in _MONEY_RE.findall(sentence.text):
            # A loss is written without its minus sign ("a net loss of 30 k NOK"): compare sizes.
            amount = abs(float(number.replace(",", "")) * _UNITS[unit])
            assert any(abs(amount - abs(c)) <= abs(c) * 0.006 + 1 for c in cited_amounts), (sentence.text, amount, cited_amounts)
            checked += 1
        for pct in _PERCENT_RE.findall(sentence.text):
            # The percentage must be computed from two cited claims: the change between two
            # years of one figure, or one figure as a share of another in the same year (margin).
            figures = [(c.reporting_period, float(c.value.split()[0].replace(",", ""))) for c in cited
                       if c.reporting_period and re.match(r"^-?[\d,]+ [A-Z]{3}$", c.value)]
            computed = [abs((a - b) / b * 100) for (pa, a) in figures for (pb, b) in figures if pa > pb and b]
            computed += [abs(a / b * 100) for (pa, a) in figures for (pb, b) in figures if pa == pb and b and a != b]
            assert any(abs(value - float(pct)) <= 0.06 for value in computed), (sentence.text, pct, computed)
            checked += 1
    assert checked >= 15  # amounts, changes and margins across all four profiles were actually checked


def test_the_summary_never_contains_a_figure_when_no_figures_exist():
    text = build_summary(make_profile(legal_name=available_claim("ACME AS"))).text

    assert not re.search(r"\d[\d,.]*\s*(?:bn|m|k)?\s*(?:NOK|USD|EUR)", text)


def test_an_upcoming_event_is_never_called_the_latest_news():
    profile = make_profile(org_number="923609016", legal_name=available_claim("ACME AS"))
    profile.activity.dated_activity = [
        available_claim("Supplier Day (2099-10-28)", source_class="company_owned", effective_date="2099-10-28"),
        available_claim("New office opens (2026-01-02)", source_class="company_owned", effective_date="2026-01-02"),
    ]
    profile.run_timestamp = profile.run_timestamp.replace(year=2026, month=10, day=6)

    text = build_summary(profile).text

    assert 'latest news "New office opens" (2026-01-02)' in text and "Supplier Day" not in text


def test_an_auditor_or_deputy_is_never_named_as_leading_the_company():
    profile = make_profile(org_number="923609016", legal_name=available_claim("ACME AS"), leaders=[
        available_claim("DELOITTE AS (Revisor, org. no. 980211282)"),
        available_claim("Kari Nordmann (Varamedlem)"),
        available_claim("Ola Nordmann (Daglig leder)"),
    ])

    text = build_summary(profile).text

    assert "Led by Ola Nordmann (managing director), with 2 more registered role holders." in text


def test_the_industry_reads_in_english_and_the_registered_activity_is_quoted():
    profile = make_profile(org_number="811413682", legal_name=available_claim("ELOPAK ASA"))
    profile.legal_identity.industry = available_claim("17.120 Produksjon av papir og papp")
    profile.legal_identity.business_description = available_claim("Produksjon og salg av emballasje.")

    summary = build_summary(profile)

    assert "in manufacture of paper and paperboard" in summary.text
    assert 'Registered activity: "Produksjon og salg av emballasje".' in summary.text
    activity = next(s for s in summary.sentences if s.fields == ["business_description"])
    assert activity.claims


def test_an_unstated_industry_is_left_out_rather_than_printed():
    profile = make_profile(org_number="811413682", legal_name=available_claim("ACME AS"))
    profile.legal_identity.industry = available_claim("00.000 Uoppgitt")

    assert "uoppgitt" not in build_summary(profile).text.lower()
