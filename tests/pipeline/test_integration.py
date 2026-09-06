"""
Small end-to-end integration test across 5 fixture companies.
See docs/testing-strategy.md section 4. No live network calls -- every
registry lookup and page fetch is served from fixtures/ via httpx.MockTransport.
"""
import json
from pathlib import Path

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.assemble import assemble
from src.pipeline.crawl import crawl
from src.pipeline.extract import extract
from src.pipeline.registry_extras import fetch_registry_extras
from src.pipeline.resolve import resolve
from src.pipeline.verify import verify

FIXTURES = Path(__file__).parent.parent.parent / "fixtures"
REGISTRY = FIXTURES / "registry"
PAGES = FIXTURES / "pages"

# (org_number, has_official_site) -- mirrors fixtures/registry + fixtures/pages
COMPANIES = [
    ("923609016", True),
    ("997770234", True),
    ("925836613", True),
    ("991753591", True),
    ("000000000", False),  # doesn't exist in the registry -- not_available
]


def _extra_fixture(kind: str, org_number: str):
    path = REGISTRY / kind / f"{org_number}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _client_for(org_number: str) -> httpx.Client:
    registry_fixture = json.loads((REGISTRY / f"{org_number}.json").read_text(encoding="utf-8"))
    registry_url = f"https://data.brreg.no/enhetsregisteret/api/enheter/{org_number}"
    roller_fixture = _extra_fixture("roller", org_number)
    regnskap_fixture = _extra_fixture("regnskap", org_number)
    underenheter_fixture = _extra_fixture("underenheter", org_number)

    page_path = PAGES / org_number / "official_site.html"
    page_html = page_path.read_text(encoding="utf-8") if page_path.exists() else None

    def _respond(fixture):
        if fixture["body"] is None:
            return httpx.Response(fixture["status"])
        return httpx.Response(fixture["status"], json=fixture["body"])

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == registry_url:
            return _respond(registry_fixture)
        if roller_fixture and url == f"{registry_url}/roller":
            return _respond(roller_fixture)
        if regnskap_fixture and url == f"https://data.brreg.no/regnskapsregisteret/regnskap/{org_number}":
            return _respond(regnskap_fixture)
        if underenheter_fixture and url == f"https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet={org_number}":
            return _respond(underenheter_fixture)
        if page_html is not None:
            return httpx.Response(200, text=page_html)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _process(org_number: str):
    client = _client_for(org_number)
    entity = resolve(org_number, client=client)

    raw_facts = []
    confirmed = []
    if entity.resolution_state == EvidenceState.AVAILABLE:
        budget = BudgetGovernor(BudgetLimits(max_requests=20, max_spend_usd=10, max_wall_clock_seconds=60))
        # The mocked page is served at the entity's own resolved domain, so
        # crawl()'s allow-list (official site only) is naturally satisfied.
        pages = crawl(entity, budget, client=client)
        for page in pages:
            if page.fetch_state == EvidenceState.AVAILABLE:
                raw_facts.extend(extract(page))
        confirmed = [c for c in (verify(f, entity) for f in raw_facts) if c is not None]
        confirmed += fetch_registry_extras(entity, budget, client=client)

    return assemble(entity, confirmed, previous_snapshot=None)


def _all_claims(profile):
    claims = [
        profile.legal_identity.legal_name,
        profile.legal_identity.public_brand,
        profile.annual_accounts.latest,
        profile.online_presence.official_site,
    ]
    claims += profile.annual_accounts.history
    claims += profile.leadership.leaders
    claims += profile.leadership.workplaces
    claims += profile.online_presence.company_profiles
    claims += profile.activity.hiring_signals
    claims += profile.activity.dated_activity
    claims += profile.evidence_log
    return claims


def test_five_fixture_companies_produce_five_valid_profiles():
    profiles = [_process(org_number) for org_number, _ in COMPANIES]

    assert len(profiles) == 5
    assert {p.org_number for p in profiles} == {org for org, _ in COMPANIES}

    by_org = {p.org_number: p for p in profiles}
    assert by_org["923609016"].legal_identity.legal_name.value == "EQUINOR ASA"
    assert by_org["997770234"].legal_identity.legal_name.value == "KAHOOT! AS"
    assert by_org["000000000"].legal_identity.legal_name.state == EvidenceState.NOT_AVAILABLE

    # registry_extras wired end-to-end: leadership/accounts/workplaces populated
    equinor = by_org["923609016"]
    assert len(equinor.leadership.leaders) > 0
    assert len(equinor.leadership.workplaces) > 0
    assert equinor.annual_accounts.latest.state == EvidenceState.AVAILABLE
    assert equinor.annual_accounts.latest.reporting_period == "FY2025"


def test_every_claim_in_every_profile_has_a_valid_state():
    valid_states = set(EvidenceState)

    for org_number, _ in COMPANIES:
        profile = _process(org_number)
        for claim in _all_claims(profile):
            assert claim.state in valid_states
            if claim.state == EvidenceState.AVAILABLE:
                assert claim.value is not None
                assert claim.retrieved_at is not None
            else:
                assert claim.value is None
