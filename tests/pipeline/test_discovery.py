"""
Tests for src/pipeline/discovery.py -- candidate website discovery for
companies with no official site on file in the registry.

Source policy: "search/discovery providers for candidate generation only,
never as evidence itself" -- a search hit from ANY provider is never trusted
on its own; `verify_discovered_site` independently fetches the candidate and
requires the SAME name-match threshold verify.py uses before accepting it.

Provider chain, in order: Exa (best free tier, no card) -> Parallel (also
generous, no card, native source-exclusion) -> DuckDuckGo (always available,
free, no key, but Wikipedia-infobox-gated -- weak for small/non-notable
companies, see module docstring). Each tier is skipped (not attempted, no
budget spent) when its API key isn't set -- `discover_candidate_site`'s
`exa_api_key`/`parallel_api_key` params default to reading
EXA_API_KEY/PARALLEL_API_KEY from the environment, but tests always pass
them explicitly as None/a fake value so a developer's real shell environment
can never make a test flaky.
"""
from datetime import datetime, timezone

import httpx
import pytest

from src.orchestrator.budget import BudgetGovernor, BudgetLimits
from src.pipeline.discovery import (
    MAX_FETCH_RETRIES,
    ProviderHealth,
    check_provider_keys,
    discover_candidate_site,
    strip_leader_role_title,
    verify_discovered_site,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _no_keys(**overrides):
    """Explicit, hermetic 'no paid provider configured' kwargs -- forces the
    chain straight to DuckDuckGo regardless of the real shell's environment."""
    kwargs = {"exa_api_key": None, "parallel_api_key": None}
    kwargs.update(overrides)
    return kwargs


def _ddg_response(infobox_website: str | None) -> dict:
    content = []
    if infobox_website:
        content.append({"label": "Website", "value": f"[{infobox_website}]"})
    return {"Heading": "Equinor", "Infobox": {"content": content} if content else None}


def test_query_strips_the_legal_entity_suffix():
    """Real-world regression: DuckDuckGo's Instant Answer infobox match is
    keyed to Wikipedia article titles, which use a company's common name
    ("Equinor"), not its legal name with entity suffix ("Equinor ASA") --
    querying with the suffix intact returns nothing at all, even for a
    company with a real Wikipedia infobox."""
    seen_queries = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_queries.append(dict(request.url.params)["q"])
        return httpx.Response(200, json=_ddg_response("equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys())

    assert seen_queries == ["EQUINOR"]


def test_discovers_website_from_infobox():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "duckduckgo.com" in str(request.url)
        return httpx.Response(200, json=_ddg_response("equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys())

    assert candidate == "https://equinor.com"


def test_accepts_a_202_response_with_a_valid_body():
    """Real-world regression: DuckDuckGo's Instant Answer API sometimes
    returns 202 Accepted (not 200) for a perfectly valid, full response
    body -- must not be treated as a failure."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(202, json=_ddg_response("equinor.com"))))
    budget = BudgetGovernor()

    assert discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys()) == "https://equinor.com"


def test_retries_once_on_an_empty_body_before_giving_up():
    """Real-world regression: DuckDuckGo occasionally returns a 200 with a
    completely empty body for a query that works moments later -- worth one
    retry before concluding there's genuinely no result."""
    responses = [httpx.Response(200, content=b""), httpx.Response(200, json=_ddg_response("equinor.com"))]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    assert discover_candidate_site("EQUINOR ASA", client, budget, sleep=lambda s: None, **_no_keys()) == "https://equinor.com"


def test_no_infobox_returns_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ddg_response(None))))
    budget = BudgetGovernor()

    assert discover_candidate_site("SOME SMALL COMPANY AS", client, budget, **_no_keys()) is None


def test_discovery_respects_budget():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ddg_response("equinor.com"))))
    budget = BudgetGovernor(BudgetLimits(max_requests=0, max_spend_usd=10, max_wall_clock_seconds=1000))

    assert discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys()) is None


def test_discovery_handles_network_failure_gracefully():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    assert discover_candidate_site("EQUINOR ASA", client, budget, sleep=lambda s: None, **_no_keys()) is None


def test_verify_discovered_site_accepts_matching_name():
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Equinor ASA"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget, now=lambda: NOW)

    assert confirmed is not None
    assert confirmed.field_name == "official_site"
    assert confirmed.value == "https://equinor.com"
    assert confirmed.source_class == "external"
    assert confirmed.extraction_method == "discovery"


def test_verify_discovered_site_rejects_mismatched_name():
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Totally Different Company AS"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget, now=lambda: NOW)

    assert confirmed is None


def test_verify_discovered_site_falls_back_to_page_text_when_no_json_ld():
    html = "<html><body><p>Equinor ASA is a Norwegian energy company.</p></body></html>"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget, now=lambda: NOW)

    assert confirmed is not None


def test_verify_discovered_site_handles_too_many_redirects_gracefully():
    """Real-world regression, found running the real 1,000-company batch
    with live Exa/Parallel discovery: a discovered candidate can redirect in
    a loop. httpx.TooManyRedirects is an httpx.HTTPError, not an
    httpx.TransportError -- the same exception-type gap already fixed once
    for crawl.py's malformed-redirect case, just not carried over here. Must
    degrade to None, not propagate past this function and lose the whole
    company to the runner's outer safety net."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TooManyRedirects("Exceeded maximum allowed redirects.", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget, sleep=lambda s: None)

    assert confirmed is None


def test_verify_discovered_site_rejects_a_url_that_embeds_a_different_companys_org_number():
    """Real-world regression, confirmed in a real 1,000-company batch: org
    836296222 ("GRE HOLDING AS") was linked to
    https://21st.ai/en/companies/306155/no/oslo/-/org-number-929396855-greve-holding-as/summary
    -- a page for a DIFFERENT company, org 929396855 ("GREVE HOLDING AS").
    Name-similarity alone can't reliably catch this: RapidFuzz scores "GRE
    HOLDING AS" vs "GREVE HOLDING AS" at 92-93 under every metric tested
    (partial_ratio, token_sort_ratio, token_set_ratio, WRatio), all above
    NAME_MATCH_THRESHOLD=90 -- short, generic Norwegian holding-company names
    are inherently prone to this. But the candidate URL itself embeds the
    OTHER company's real 9-digit Norwegian org number, which is a much more
    reliable, domain-agnostic signal (works even on a directory site not yet
    in AGGREGATOR_DOMAIN_BLACKLIST): if a URL contains a 9-digit sequence
    that isn't this entity's own org number, it belongs to someone else."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Greve Holding AS"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://21st.ai/en/companies/306155/no/oslo/-/org-number-929396855-greve-holding-as/summary",
        "GRE HOLDING AS",
        client,
        budget,
        org_number="836296222",
    )

    assert confirmed is None


def test_verify_discovered_site_rejects_a_url_that_embeds_this_companys_own_org_number():
    """Real-world regression, found regenerating the 1,000-company corpus: six
    companies were given a directory page KEYED BY THEIR OWN ORG NUMBER as their
    "official website" -- areg.no/816028612, listings.no/b/922899924,
    forvalt.no/Nettbutikk/produkter/912410943 and
    datalog.co.uk/browse/detail.php/CompanyNumber/NO890546242/... Each such page
    lists the company's own org number, so the rule "a page that publishes this
    company's org number is accepted" waved them through. A company's own
    homepage doesn't carry its org number in the URL; a directory listing does."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"STAVLAND HOLDING AS"}</script></head><body>Org.nr: 816 028 612</body></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))

    for url, org in (
        ("https://areg.no/816028612", "816028612"),
        ("https://listings.no/b/922899924", "922899924"),
        ("https://forvalt.no/Nettbutikk/produkter/912410943", "912410943"),
        ("https://datalog.co.uk/browse/detail.php/CompanyNumber/NO890546242/CompanyName/X", "890546242"),
        ("https://www.equinor.com/about/org-number-923609016", "923609016"),
    ):
        assert verify_discovered_site(url, "STAVLAND HOLDING AS", client, BudgetGovernor(), org_number=org) is None, url


def test_verify_discovered_site_with_no_org_number_mismatch_signal_still_works_as_before():
    """A candidate URL with no embedded 9-digit number at all (the normal
    case -- most homepages are just a bare domain) must be unaffected."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Equinor ASA"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://www.equinor.com", "EQUINOR ASA", client, budget, org_number="923609016")

    assert confirmed is not None


def test_verify_discovered_site_respects_budget():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html></html>")))
    budget = BudgetGovernor(BudgetLimits(max_requests=0, max_spend_usd=10, max_wall_clock_seconds=1000))

    assert verify_discovered_site("https://equinor.com", "EQUINOR ASA", client, budget) is None


def test_1881_no_is_blacklisted():
    """Real-world regression, found via independent review of a real
    100-company batch: org 816981522 (BERATO INVEST AS, no site of its own)
    was attributed 1881.no -- a Norwegian phone/business-directory site --
    as its official_website, and subsequent crawling of that shared
    directory domain scraped OTHER, wholly unrelated businesses' own
    listing pages (ama-trykkluft-as, advokatfellesskapet-drift-as,
    soermaskin-swt-as, ...) as if they were BERATO INVEST AS's own dated
    activity. Same directory-domain class as rosa.no/regnskapstall.no."""
    from src.pipeline.discovery import _is_blacklisted_domain

    assert _is_blacklisted_domain("https://www.1881.no/tlf/example-as/123456789")


def test_generate_no_is_blacklisted():
    """Real-world regression, found auditing a real 1,000-company batch and
    confirmed against the live Brreg API and the frozen universe file: 88
    unrelated shell/holding companies (all with NO registered website in
    either source -- e.g. org 818842422 ARTING EIENDOM AS, hjemmeside=None,
    universe website="") were all attributed "https://www.generate.no" as
    their official_website, and it cascaded into repeated duplicate
    company_profile facts too once the pipeline treated it as that entity's
    confirmed domain. generate.no lists/manages many unrelated companies
    (a company-formation/administration service), so it belongs on the same
    blacklist as rosa.no/21st.ai/etc, not treated as anyone's own site."""
    from src.pipeline.discovery import _is_blacklisted_domain

    assert _is_blacklisted_domain("https://www.generate.no")
    assert _is_blacklisted_domain("https://www.generate.no/about")


def test_verify_discovered_site_rejects_a_blacklisted_directory_domain_even_when_the_name_matches_exactly():
    """Real-world regression, found auditing a real 1,000-company batch:
    business-directory/aggregator pages (e.g. rosa.no, regnskapstall.no,
    virksomhet.brreg.no -- a government registry lookup page, not the
    company's own site) always echo the exact legal name verbatim, so they
    trivially clear NAME_MATCH_THRESHOLD and get published as
    "official_website" -- ~40% of discovered sites in that batch were one of
    these, none of them the company's actual site. A directory hit must be
    rejected on domain alone, before ever spending a request to fetch it."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a blacklisted domain at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.rosa.no/tlf/equinor-asa/923609016", "EQUINOR ASA", client, budget
    )

    assert confirmed is None
    assert budget.requests_used == 0


def test_verify_discovered_site_rejects_prospeo_lead_gen_aggregator():
    """Real-world regression, found running a genuinely unseen 100-company
    batch (org numbers not in the submitted entry-companies.jsonl, sampled
    from companies the universe file lists with no website on file --
    the hardest discovery case). Exa returned
    https://prospeo.io/c/sykkelkomponenter-no-revenue for "SYKKELKOMPONENTER
    AS" -- confirmed live (browser fetch) to be a B2B contact/lead-gen
    company-database page ("Sykkelkomponenter.no Revenue, Funding &
    Valuation"), not the company's own site. It genuinely is about the
    right company (so name-match alone would accept it), which is exactly
    why a domain blacklist -- not a name/identity check -- is the correct
    layer to reject it at."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a blacklisted domain at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://prospeo.io/c/sykkelkomponenter-no-revenue", "SYKKELKOMPONENTER AS", client, budget
    )

    assert confirmed is None
    assert budget.requests_used == 0


def test_verify_discovered_site_rejects_a_nav_job_posting_as_an_official_site():
    """Real-world regression, found in a fourth re-run of the same general
    unseen batch: Exa returned
    https://arbeidsplassen.nav.no/stillinger/stilling/1e484d11-0782-4b10-8e9f-221658d4a745
    for "TEMPTATION RESTAURANT AS" -- confirmed live to be a specific NAV
    (Norway's labor/welfare directorate) job posting ("Erfaren à la
    carte-kokk søkes..."), not the restaurant's own site."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a blacklisted domain at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://arbeidsplassen.nav.no/stillinger/stilling/1e484d11-0782-4b10-8e9f-221658d4a745",
        "TEMPTATION RESTAURANT AS", client, budget,
    )

    assert confirmed is None
    assert budget.requests_used == 0


def test_verify_discovered_site_rejects_a_generic_directory_listing_url_shape():
    """Real-world regression, found re-running the same batch a third time:
    ipqwery.com (an IP/trademark database, "ipowner/en/owner/profile/...")
    and pappers.no (a French company-registry aggregator's Norwegian arm,
    "company/<name>-<org-number>") were both new, previously-unseen domains
    -- yet every confirmed aggregator/directory found across three separate
    unseen batches this session shares one structural trait: a generic
    "listing/profile" word as its OWN url path segment (company, bedrift,
    selskap, firma, owner, profile, ...). A real company's own homepage
    essentially never structures its URL this way. This is a domain-agnostic
    defense-in-depth -- it would have caught most of the domain-specific
    blacklist entries above even before they were individually identified,
    and should catch the next one not yet seen."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a directory-shaped URL at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    assert verify_discovered_site(
        "https://www.ipqwery.com/ipowner/en/owner/profile/1800975-seastate-7-as.html",
        "SEASTATE 7 AS", client, budget,
    ) is None
    assert verify_discovered_site(
        "https://www.pappers.no/company/tibtek-as-928649628", "TIBTEK AS", client, budget
    ) is None
    assert budget.requests_used == 0


def test_verify_discovered_site_directory_path_check_does_not_reject_real_sites():
    """Guards against over-rejection: a real company's own '/about'-style
    page and a housing co-op's own slug on its manager's platform must
    still pass. (The old positive control, a URL embedding the company's own
    org number, is now a rejection: see
    test_verify_discovered_site_rejects_a_url_that_embeds_this_companys_own_org_number.)"""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Equinor ASA"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    assert verify_discovered_site(
        "https://www.equinor.com/about", "EQUINOR ASA", client, budget, org_number="923609016"
    ) is not None


def test_verify_discovered_site_rejects_the_second_wave_of_confirmed_aggregators():
    """Real-world regression: re-running the same general unseen 100-company
    batch (live discovery results are not deterministic run-to-run) surfaced
    14 more real aggregator/lead-gen/SaaS-profile-page hits not caught by
    the blacklist at the time, each confirmed live before being added -- see
    AGGREGATOR_DOMAIN_BLACKLIST's docstring for what each one actually is.
    The same URL (opplysning.byndle.no) was returned as the "official site"
    for four different companies in one run -- the clearest possible signal
    it's a directory, not anyone's own site."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a blacklisted domain at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    second_wave_domains = [
        ("https://opplysning.byndle.no", "TORES TRANSPORT AS"),
        ("https://nyeselskaper.no/companies/69342306-d17b-4da4-9dd0-9566007671a9", "TOMTEVEIEN 51 A AS"),
        ("https://allebedrifter.no/companies/58e259ae-81b9-422e-b7ed-aea4aae1e666", "KOPPERUDS AS"),
        ("https://www.1850.no/bedrifter/annet/annet/oslo/oslo/tibtek-as-928649628", "TIBTEK AS"),
        ("https://flowfirma.no/firma/handverker-jan-erik-moxness-hjem-as-914826098", "HANDVERKER JAN-ERIK MOXNESS, HJEM AS"),
        ("https://haandverkerportalen.no/bedrift/byggmester-atle-fjaereide-as-915709818", "BYGGMESTER ATLE FJAEREIDE AS"),
        ("https://mittanbud.no/bedrift/9231606", "BYGGMESTER AKERSVEEN AS"),
        ("https://eiendomssjekk.no/bedrift/933303586", "M-DRIFT AS"),
        ("https://www.fagfolkguiden.no/bedrift/j-m-transport-as-991565086", "J.M TRANSPORT AS"),
        ("https://firmview.no/company/926544063", "KIKO INVEST AS"),
        ("https://www.vexter.no/selskap/selo-holding-as/923185283", "SELO HOLDING AS"),
        ("https://agama.no/bedrift/behr-invest-as-998178924", "BEHR INVEST AS"),
        ("https://elinnweb.no/1437/about", "NIKOLAISEN ELEKTRO AS"),
        ("https://thehub.io/startups/seastate-7", "SEASTATE 7 AS"),
        ("https://stipendportalen.no", "STIFTELSEN STAVANGER HAVNEMISJON"),
    ]
    for url, legal_name in second_wave_domains:
        assert verify_discovered_site(url, legal_name, client, budget) is None, url
    assert budget.requests_used == 0


def test_verify_discovered_site_rejects_northdata_and_merinfo_aggregators():
    """Real-world regression, found running a general (not website-filtered)
    unseen 100-company batch: northdata.com ("North Data Smart Research", a
    European company-research aggregator) and merinfo.no (a Norwegian
    business-lookup directory) were both discovered and would have passed
    name-matching (they legitimately are about the right company), which is
    exactly why the domain blacklist -- not name similarity -- is the right
    layer to reject them at."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a blacklisted domain at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    assert verify_discovered_site("https://northdata.com/Gilje+Group+AS", "GILJE GROUP AS", client, budget) is None
    assert verify_discovered_site(
        "http://merinfo.no/bedrift/AS/929431650/0186-BARDRIFT-AS", "0186 BARDRIFT AS", client, budget
    ) is None
    assert budget.requests_used == 0


def test_verify_discovered_site_rejects_a_page_showing_a_different_labeled_org_number():
    """Real-world regression, same unseen-batch run as above: Exa returned
    https://adnor.no ("Adnor Advokat AS", org 996399869, confirmed live via
    Brreg) for "ADVOKAT VIBEKE MELAND" (a different, separately registered
    entity, org 989750291). Name-matching can't catch this -- a law firm's
    site plausibly mentions an associated lawyer's name -- and the URL
    itself carries no org number. Norwegian law requires a business to
    display its own org number on its site, and adnor.no's footer does:
    "Org.nr: 996 399 869" -- a different number than this entity's own,
    which is exactly the signal that should reject it."""
    html = (
        "<html><body><footer>Org.nr: 996 399 869<br>"
        "Adnor Advokat, Dronningens gate 9, Trondheim</footer></body></html>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://adnor.no", "ADVOKAT VIBEKE MELAND", client, budget, now=lambda: NOW, org_number="989750291"
    )

    assert confirmed is None


def test_verify_discovered_site_rejects_labeled_org_number_split_by_a_real_html_tag():
    """Real-world regression: re-running the exact adnor.no case above
    against the REAL live site (not the synthetic HTML in the test above)
    found this check silently failing to fire. The real page renders
    "<strong>Org.nr:</strong> 996 399 869" -- a closing tag sits between
    the label and the digits, which the original whitespace-only regex
    never matched. The synthetic test's hand-written HTML happened not to
    include any markup there, so it passed without exercising the real
    shape. Confirmed via a direct fetch of the live page before fixing."""
    html = (
        "<html><body><p><a href='mailto:x'>advokat@adnor.no</a></p>"
        "<p><strong>Org.nr:</strong> 996 399 869</p></body></html>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.adnor.no", "ADVOKAT VIBEKE MELAND", client, budget, now=lambda: NOW, org_number="989750291"
    )

    assert confirmed is None


def test_verify_discovered_site_accepts_a_page_showing_its_own_matching_org_number():
    html = (
        '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Adnor Advokat"}</script>'
        "</head><body><footer>Org.nr: 996 399 869</footer></body></html>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://adnor.no", "ADNOR ADVOKAT AS", client, budget, now=lambda: NOW, org_number="996399869"
    )

    assert confirmed is not None


def test_verify_discovered_site_rejects_a_short_near_miss_company_name():
    """Real-world regression, same unseen-batch run as the prospeo.io case
    above: Exa returned https://fanison.fi/en/company/ for "FANSON AS" --
    confirmed live to be "Fanison Oy", an unrelated Finnish ventilation
    company in Lahti, Finland. The page's structured organization_name is
    the bare word "Fanison" (7 chars); partial_ratio("FANSON AS",
    "Fanison") scores 92.3 -- above NAME_MATCH_THRESHOLD (90) -- purely
    because a 1-character edit distance on a short string produces a high
    substring-alignment score, not because the names actually match. See
    name_similarity's docstring in verify.py for the fix: a short candidate
    must match (near-)exactly, not just fuzzily, to be accepted."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Fanison"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site("https://fanison.fi/en/company/", "FANSON AS", client, budget, now=lambda: NOW)

    assert confirmed is None


# --- Exa (primary provider) ---


def _exa_response(url: str | None) -> dict:
    results = [{"title": "Company", "url": url}] if url else []
    return {"requestId": "req-1", "results": results}


def test_exa_is_tried_first_when_key_present():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers.get("x-api-key") == "fake-exa-key"
        return httpx.Response(200, json=_exa_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate == "https://equinor.com"
    assert calls == ["https://api.exa.ai/search"]


def test_skips_non_webpage_results_like_pdfs():
    """Real-world regression: a real Parallel query for a real company's
    official site returned a PDF annual report as the top result, not the
    homepage. verify_discovered_site correctly rejected it (extruct/
    trafilatura can't extract an org name from a PDF), but that wastes a
    verify-fetch on something that could never work -- skip obviously
    non-webpage results (by file extension) before returning a candidate at
    all, and fall through to the next result instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "requestId": "req-1",
                "results": [
                    {"title": "Annual Report", "url": "https://example.com/reports/annual-2023.pdf"},
                    {"title": "Logo", "url": "https://example.com/assets/logo.png"},
                    {"title": "Homepage", "url": "https://example.com/"},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EXAMPLE AS", client, budget, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate == "https://example.com/"


def test_exa_skips_blacklisted_aggregator_domains_and_falls_through_to_the_next_result():
    """Real-world regression: unlike Parallel, Exa's request body has no
    exclude-domains parameter at all, and Exa is tried FIRST in the provider
    chain -- so a directory/aggregator hit from Exa sailed straight through
    with zero filtering. _first_webpage_url must reject a blacklisted domain
    client-side (defense in depth, not dependent on any one provider's own
    exclusion feature) and try the next result instead."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "requestId": "req-1",
                "results": [
                    {"title": "Directory listing", "url": "https://www.rosa.no/tlf/example-as/923609016"},
                    {"title": "Government registry lookup", "url": "https://virksomhet.brreg.no/nb/oppslag/enheter/923609016"},
                    {"title": "Homepage", "url": "https://example.no/"},
                ],
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EXAMPLE AS", client, budget, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate == "https://example.no/"


def test_exa_no_results_returns_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_exa_response(None))))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "SOME SMALL COMPANY AS", client, budget, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate is None


def test_exa_network_failure_does_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TransportError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, sleep=lambda s: None, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    assert candidate is None


def test_exa_402_payment_required_is_not_retried_and_falls_through_immediately():
    """Real-world regression, confirmed live against the real Exa API mid-
    session: a 402 Payment Required (quota exhausted) is a PERMANENT
    failure for the rest of the billing period, not a transient one --
    unlike a 5xx or network blip, retrying it can never succeed. The old
    code retried it anyway (MAX_FETCH_RETRIES times, with backoff sleep),
    wasting real wall-clock time on a call already known to be doomed, and
    still spent a full unit of request budget per attempt. With three
    discovery tiers (legal name, leader name, org number) each hitting Exa
    first, that waste multiplied 3x per company across a real 1,000-company
    batch -- a large factor in why that run took ~78 minutes instead of the
    usual ~22. Must call the endpoint exactly ONCE, not MAX_FETCH_RETRIES
    times, and fall through to Parallel immediately."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "exa.ai" in str(request.url):
            return httpx.Response(402)
        return httpx.Response(200, json=_parallel_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, sleep=lambda s: None,
        exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key",
    )

    exa_calls = [c for c in calls if "exa.ai" in c]
    assert len(exa_calls) == 1
    assert candidate == "https://equinor.com"


def test_exa_401_and_403_are_also_not_retried():
    for status in (401, 403):
        calls = []

        def handler(request: httpx.Request, status=status) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(status)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        budget = BudgetGovernor()

        discover_candidate_site(
            "EQUINOR ASA", client, budget, sleep=lambda s: None, exa_api_key="fake-exa-key", parallel_api_key=None
        )

        exa_calls = [c for c in calls if "exa.ai" in c]
        assert len(exa_calls) == 1, f"status {status} was retried"


def test_5xx_and_network_errors_are_still_retried():
    """Backward-compatible: a genuinely transient failure (server error,
    transport blip) must still get the existing retry behavior -- only
    permanent auth/payment failures skip retries."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site(
        "EQUINOR ASA", client, budget, sleep=lambda s: None, exa_api_key="fake-exa-key", parallel_api_key=None
    )

    exa_calls = [c for c in calls if "exa.ai" in c]
    assert len(exa_calls) == MAX_FETCH_RETRIES


def test_explicit_none_key_is_never_overridden_by_the_real_environment(monkeypatch):
    """Real-world regression: exa_api_key=None must mean "this provider is
    explicitly disabled," never "not specified, fall back to the real
    environment." A previous version of discover_candidate_site treated an
    explicit None the same as "not provided" and silently read os.environ
    instead -- invisible in a dev session where the env var wasn't yet
    inherited by the running process, but a real problem once a persistent
    setx-set key naturally propagates to a fresh session."""
    monkeypatch.setenv("EXA_API_KEY", "a-real-key-that-must-be-ignored")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_ddg_response(None))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys())

    assert not any("exa.ai" in u for u in calls)


def test_exa_skipped_entirely_without_a_key():
    """No EXA_API_KEY -- must not attempt a request to Exa's endpoint at all."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_ddg_response(None))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, **_no_keys())

    assert not any("exa.ai" in u for u in calls)


# --- Parallel (fallback provider) ---


def _parallel_response(url: str | None) -> dict:
    results = [{"title": "Company", "url": url, "excerpts": ["..."]}] if url else []
    return {"search_id": "s-1", "results": results}


def test_parallel_is_tried_when_exa_has_no_key():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert request.headers.get("x-api-key") == "fake-parallel-key"
        return httpx.Response(200, json=_parallel_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key=None, parallel_api_key="fake-parallel-key"
    )

    assert candidate == "https://equinor.com"
    assert calls == ["https://api.parallel.ai/v1/search"]


def test_parallel_request_excludes_aggregator_domains_and_requests_multiple_results():
    """Parallel's advanced_settings.source_policy.exclude_domains and
    advanced_settings.max_results are used natively, matching the same
    aggregator blacklist principle already applied elsewhere in the project
    (proff.no/purehelp.no were already excluded from direct crawling for the
    same reason). Nesting confirmed against the real live API -- a flatter
    `advanced_settings.exclude_domains` 422s with "Extra inputs are not
    permitted"."""
    seen_body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(__import__("json").loads(request.content))
        return httpx.Response(200, json=_parallel_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, exa_api_key=None, parallel_api_key="fake-parallel-key")

    advanced = seen_body.get("advanced_settings", {})
    excluded = set(advanced.get("source_policy", {}).get("exclude_domains", []))
    assert {"proff.no", "purehelp.no", "facebook.com", "linkedin.com", "wikipedia.org"} <= excluded
    assert advanced.get("max_results", 0) >= 5


def test_parallel_is_tried_when_exa_yields_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        if "exa.ai" in str(request.url):
            return httpx.Response(200, json=_exa_response(None))
        return httpx.Response(200, json=_parallel_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key"
    )

    assert candidate == "https://equinor.com"


def test_parallel_not_tried_when_exa_already_found_something():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=_exa_response("https://equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key"
    )

    assert not any("parallel.ai" in u for u in calls)


def test_duckduckgo_is_last_resort_when_both_paid_providers_yield_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "exa.ai" in url:
            return httpx.Response(200, json=_exa_response(None))
        if "parallel.ai" in url:
            return httpx.Response(200, json=_parallel_response(None))
        return httpx.Response(200, json=_ddg_response("equinor.com"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    candidate = discover_candidate_site(
        "EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key"
    )

    assert candidate == "https://equinor.com"


def test_all_three_providers_absent_or_empty_returns_none():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ddg_response(None))))
    budget = BudgetGovernor()

    assert discover_candidate_site("SOME SMALL COMPANY AS", client, budget, **_no_keys()) is None


# --- Leader/founder bridge (agent playbook §2, learning harness §1) ---
#
# The retry-with-a-leader-name orchestration lives in runner.py, not here --
# see discover_candidate_site's and strip_leader_role_title's docstrings for
# why (a real, measured bug: retrying only when THIS function finds nothing
# misses the common case where it finds something that then fails
# verify_discovered_site's identity check). This module just provides the
# name-formatting helper and stays a plain single-query function.


def test_strip_leader_role_title_removes_the_parenthetical():
    assert strip_leader_role_title("Anders Opedal (Daglig leder)") == "Anders Opedal"


def test_strip_leader_role_title_leaves_a_bare_name_unchanged():
    assert strip_leader_role_title("Anders Opedal") == "Anders Opedal"


def test_only_attempted_providers_spend_budget():
    """Exa succeeds on the first try -- Parallel and DuckDuckGo must never be
    attempted, so only 1 request should be spent, not 3."""
    client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_exa_response("https://equinor.com")))
    )
    budget = BudgetGovernor()

    discover_candidate_site("EQUINOR ASA", client, budget, exa_api_key="fake-exa-key", parallel_api_key="fake-parallel-key")

    assert budget.requests_used == 1


def test_verify_discovered_site_requires_positive_identity_confirmation():
    """2-factor gate, second factor: a discovered candidate must POSITIVELY
    confirm whose site it is -- either the entity's own 9-digit org number
    appears on the page (Norwegian businesses are legally required to
    publish it), or the legal name itself does. This is the generalization
    of the lillemarkens.no case: a shopping mall's own site genuinely
    mentions none of its tenants' legal names or org numbers, yet fuzzy
    name-matching accepted it as one tenant's official website."""
    mall_html = (
        "<html><body><h1>Lillemarkens</h1>"
        "<p>Midt i hjertet av Kristiansand - 30 butikker under ett tak.</p>"
        "</body></html>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=mall_html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.lillemarkens.no", "SANS & SMAK AS", client, budget, now=lambda: NOW, org_number="915621279"
    )

    assert confirmed is None


def test_verify_discovered_site_accepts_a_page_confirming_identity_by_org_number():
    """The org number is the strongest possible confirmation -- accept on it
    even when the site trades under a brand name that looks nothing like the
    registered legal name (very common for small Norwegian companies)."""
    html = (
        "<html><body><h1>Snekkersentralen</h1>"
        "<footer>Org.nr: 914 826 098</footer></body></html>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://snekkersentralen.no",
        "HANDVERKER JAN-ERIK MOXNESS, HJEM AS",
        client, budget, now=lambda: NOW, org_number="914826098",
    )

    assert confirmed is not None
    assert confirmed.match_confidence == 100.0


def test_verify_discovered_site_accepts_a_page_confirming_identity_by_legal_name():
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Equinor ASA"}</script></head><body><p>Equinor ASA is a Norwegian energy company.</p></body></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.equinor.com", "EQUINOR ASA", client, budget, now=lambda: NOW, org_number="923609016"
    )

    assert confirmed is not None


def test_verify_discovered_site_rejects_directory_and_search_path_segments():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a directory-shaped URL at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    assert verify_discovered_site("https://example.no/directory/acme-as", "ACME AS", client, budget) is None
    assert verify_discovered_site("https://example.no/search/acme", "ACME AS", client, budget) is None
    assert budget.requests_used == 0


def test_short_named_company_on_a_foreign_domain_is_rejected_by_name_matching_alone():
    """The real "FANSON AS" vs Finnish "Fanison Oy" collision. A separate
    foreign-TLD rule was considered for this (reject .fi/.se/.dk for short
    names) and measured against the real registry -- only 0.2% (14 of 6,665)
    of short-named Norwegian companies with a website use one, so the cost
    would have been small. It was NOT implemented: the short-candidate
    exact-match rule in verify.name_similarity already rejects every real
    case found across three unseen batches, so the TLD rule would add a
    coverage cost and more code for no measured benefit. This test pins that
    the existing rule is what carries the case."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Fanison"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://fanison.fi", "FANSON AS", client, budget, now=lambda: NOW, org_number="875368532"
    )

    assert confirmed is None


def test_a_norwegian_company_on_a_foreign_domain_is_accepted_via_org_number():
    """Real registry data: 14 short-named Norwegian companies genuinely
    operate on .fi/.se/.dk domains (e.g. SKOGEX AS on skogex.se). Hard
    org-number evidence must beat any heuristic about which TLD a Norwegian
    company "should" use."""
    html = "<html><body><h1>Skogex</h1><footer>Org.nr: 917 860 025</footer></body></html>"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://skogex.se", "SKOGEX AS", client, budget, now=lambda: NOW, org_number="917860025"
    )

    assert confirmed is not None


def test_verify_discovered_site_rejects_a_shared_venue_listing_many_organizations():
    """Closes the last confirmed open false positive: lillemarkens.no is a
    shopping mall's own site (30 tenant shops in Kristiansand) that was
    accepted as the official website of SANS & SMAK AS, one of its tenants.
    Generalizes past that one domain: a page whose structured data declares
    many distinct organizations is a venue/marketplace/portal listing other
    businesses, not any single one of them's own site. A real company site
    declares itself, not a directory of its neighbours."""
    tenants = ",".join(
        f'{{"@type":"Organization","name":"Tenant {i} AS"}}' for i in range(6)
    )
    html = (
        '<html><head><script type="application/ld+json">'
        f'[{{"@type":"Organization","name":"Sans & Smak AS"}},{tenants}]'
        "</script></head><body></body></html>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.lillemarkens.no", "SANS & SMAK AS", client, budget, now=lambda: NOW, org_number="915621279"
    )

    assert confirmed is None


def test_a_normal_company_page_with_a_couple_of_organizations_is_still_accepted():
    """Must not over-reject: a real company page legitimately carries its own
    Organization plus e.g. a parent or a publisher block. Only a page listing
    MANY distinct organizations looks like a venue directory."""
    html = (
        '<html><head><script type="application/ld+json">'
        '[{"@type":"Organization","name":"Equinor ASA"},'
        '{"@type":"Organization","name":"Equinor Energy AS"}]'
        "</script></head><body></body></html>"
    )
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.equinor.com", "EQUINOR ASA", client, budget, now=lambda: NOW, org_number="923609016"
    )

    assert confirmed is not None


def test_verify_discovered_site_rejects_a_foreign_country_tld_without_org_number_proof():
    """Real-world regression: Exa returned https://lasventures.in/ ("LAS
    Ventures - Where Opportunity Meets Capital", an Indian firm, no mention
    of Norway, no matching org number) for Norwegian "LAS VENTURES AS".

    The name matches EXACTLY, so neither name-similarity nor the
    short-candidate exact-match rule can catch it -- a foreign country-code
    domain is the only remaining signal. Measured against the real registry:
    foreign ccTLDs account for ~0.3% of registered Norwegian company
    websites, and this rule applies only to search-DISCOVERED candidates
    (registry-provided sites never reach it) and is waived outright by
    org-number proof, so the real cost is smaller still."""
    html = "<html><head><title>LAS Ventures</title></head><body><h1>LAS Ventures</h1></body></html>"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://lasventures.in/", "LAS VENTURES AS", client, budget, now=lambda: NOW, org_number="927662558"
    )

    assert confirmed is None


def test_generic_tlds_and_norwegian_as_domains_are_unaffected():
    """.com/.io/.as are used by real Norwegian companies constantly (anti.as
    is a real, confirmed example) -- the rule targets foreign COUNTRY codes,
    not every non-.no domain. Blanket non-.no rejection would have hit 13.5%
    of real registered websites."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Anticoach AS"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    budget = BudgetGovernor()

    assert verify_discovered_site(
        "https://anti.as/", "ANTICOACH AS", client, budget, now=lambda: NOW, org_number="923413812"
    ) is not None


def test_verify_discovered_site_rejects_a_government_registry_lookup_page():
    """Real-world regression: finanstilsynet.no (Norway's Financial
    Supervisory Authority) registry-detail page was returned as a company's
    official website -- and the page was for a DIFFERENT company entirely
    ("ERGO FORSIKRING A/S NUF"). Same class as virksomhet.brreg.no and
    sgregister.dibk.no, already blacklisted: a government register lookup is
    never any company's own site."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch a blacklisted domain at all")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()

    confirmed = verify_discovered_site(
        "https://www.finanstilsynet.no/en/finanstilsynets-registry/details?id=253935",
        "SOME COMPANY AS", client, budget,
    )

    assert confirmed is None


def test_a_permanent_provider_failure_disables_that_provider_for_the_whole_run():
    """Real-world regression, hit twice in one session: once Exa's credits
    run out it returns 402 on EVERY call for the rest of the billing period.
    Not retrying a single 402 (already fixed) isn't enough -- the chain still
    made one doomed Exa call per tier, per company. Across a 100-company
    batch with three discovery tiers that's ~270 guaranteed-useless calls,
    each burning a unit of request budget and real wall-clock, and starving
    the tiers that DO work. One permanent failure must disable the provider
    for the remainder of the run."""
    exa_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "exa.ai" in url:
            exa_calls.append(url)
            return httpx.Response(402, json={"error": "NO_MORE_CREDITS"})
        if "parallel.ai" in url:
            return httpx.Response(200, json={"results": [{"url": "https://acme.no"}]})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    budget = BudgetGovernor()
    health = ProviderHealth()

    for _ in range(5):
        discover_candidate_site(
            "ACME AS", client, budget, sleep=lambda s: None,
            exa_api_key="k", parallel_api_key="k", provider_health=health,
        )

    assert len(exa_calls) == 1, f"Exa should be tried once then disabled, got {len(exa_calls)}"
    assert not health.is_available("exa")
    assert "exa" in health.disabled
    # Parallel is untouched and still serving.
    assert health.is_available("parallel")


def test_a_transient_provider_failure_does_not_disable_the_provider():
    """A 5xx or a network blip is exactly what retries exist for -- it must
    not take the provider out for the rest of the run."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    health = ProviderHealth()

    for _ in range(2):
        discover_candidate_site(
            "ACME AS", client, BudgetGovernor(), sleep=lambda s: None,
            exa_api_key="k", parallel_api_key=None, provider_health=health,
        )

    assert health.is_available("exa")
    assert len(calls) > 2


def test_discovery_works_without_a_provider_health_object():
    """provider_health is optional -- existing callers and tests must be
    unaffected."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_ddg_response("acme.no"))))

    assert discover_candidate_site("ACME AS", client, BudgetGovernor(), **_no_keys()) == "https://acme.no"


# --- Startup key check ---


def test_check_provider_keys_reports_an_exhausted_key_as_dead():
    """One tiny search per configured key before any company is processed, so
    a dead key is reported in seconds instead of after a whole batch."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "exa.ai" in str(request.url):
            return httpx.Response(402, json={"error": "NO_MORE_CREDITS"})
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    dead, requests_used = check_provider_keys(client, exa_api_key="k", parallel_api_key="k")

    assert set(dead) == {"exa"}
    assert "402" in dead["exa"]
    assert requests_used == 2


def test_check_provider_keys_does_not_call_a_transient_failure_dead():
    """A 5xx or a network blip at startup says nothing about the key -- only
    401/402/403 do. Calling a healthy key dead would stop a local run (or
    skip a working provider) for no reason."""
    def handler(request: httpx.Request) -> httpx.Response:
        if "exa.ai" in str(request.url):
            return httpx.Response(503)
        raise httpx.TransportError("boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    dead, _ = check_provider_keys(client, exa_api_key="k", parallel_api_key="k", sleep=lambda s: None)

    assert dead == {}


def test_check_provider_keys_skips_unconfigured_providers_entirely():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call a provider with no key")

    client = httpx.Client(transport=httpx.MockTransport(handler))

    assert check_provider_keys(client, exa_api_key=None, parallel_api_key=None) == ({}, 0)


# --- Search result cache ---


class _DictCache:
    """In-memory stand-in for ResponseCache with the same get/put contract."""

    def __init__(self):
        self.store = {}

    def get(self, url, date_bucket):
        return self.store.get((url, date_bucket))

    def put(self, url, date_bucket, raw):
        self.store[(url, date_bucket)] = raw


def test_a_repeated_search_the_same_day_is_served_from_cache_for_free():
    """Search responses were the one thing never cached, so every local
    re-run re-bought the same results -- a large part of how the Exa credits
    ran out twice. A cache hit costs no request and no credits."""
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"results": [{"url": "https://acme.no"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cache = _DictCache()
    budget = BudgetGovernor()

    first = discover_candidate_site("ACME AS", client, budget, exa_api_key="k", parallel_api_key=None,
                                    cache=cache, date_bucket="2026-09-15")
    second = discover_candidate_site("ACME AS", client, budget, exa_api_key="k", parallel_api_key=None,
                                     cache=cache, date_bucket="2026-09-15")

    assert first == second == "https://acme.no"
    assert len(calls) == 1
    assert budget.requests_used == 1


def test_a_failed_search_is_never_cached():
    exa_calls = {"n": 0}

    def handler(request):
        if "exa.ai" not in str(request.url):
            return httpx.Response(404)  # the keyless last-resort tier
        exa_calls["n"] += 1
        if exa_calls["n"] <= MAX_FETCH_RETRIES:
            return httpx.Response(503)
        return httpx.Response(200, json={"results": [{"url": "https://acme.no"}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    cache = _DictCache()

    assert discover_candidate_site("ACME AS", client, BudgetGovernor(), sleep=lambda s: None, exa_api_key="k",
                                   parallel_api_key=None, cache=cache, date_bucket="2026-09-15") is None
    assert cache.store == {}
    assert discover_candidate_site("ACME AS", client, BudgetGovernor(), sleep=lambda s: None, exa_api_key="k",
                                   parallel_api_key=None, cache=cache, date_bucket="2026-09-15") == "https://acme.no"


def test_the_cache_never_stores_the_api_key():
    """The cache is a plain SQLite file on disk -- a secret must not end up in it."""
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"results": [{"url": "https://acme.no"}]})
    ))
    cache = _DictCache()

    discover_candidate_site("ACME AS", client, BudgetGovernor(), exa_api_key="super-secret-key",
                            parallel_api_key="another-secret", cache=cache, date_bucket="2026-09-15")

    assert cache.store
    assert not any("secret" in key[0] or "secret" in value for key, value in cache.store.items())


# --- Former names as identity ---


def test_verify_discovered_site_accepts_a_page_under_the_company_s_own_former_name():
    """A company renamed recently often still runs its site under the old
    name; the runner only passes a former name after confirming no other
    entity holds it now."""
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Gamle Navn AS"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))

    confirmed = verify_discovered_site(
        "https://gamlenavn.no", "NYTT NAVN AS", client, BudgetGovernor(), now=lambda: NOW,
        org_number="997770234", alternate_names=["GAMLE NAVN AS"],
    )

    assert confirmed is not None


def test_verify_discovered_site_without_the_former_name_still_rejects_it():
    html = '<html><head><script type="application/ld+json">{"@type":"Organization","name":"Gamle Navn AS"}</script></head></html>'
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))

    assert verify_discovered_site(
        "https://gamlenavn.no", "NYTT NAVN AS", client, BudgetGovernor(), now=lambda: NOW, org_number="997770234"
    ) is None


# --- Search spend tracking and the run-wide spend cap ---


def _search_hit(request):
    return httpx.Response(200, json={"results": [{"url": "https://acme.no"}]})


def test_a_paid_exa_search_is_recorded_against_the_batch_spend_budget():
    """Every search used to be recorded as costing $0, so the batch's $10
    limit had nothing to enforce. Exa is $7 per 1,000 searches."""
    budget = BudgetGovernor()

    discover_candidate_site("ACME AS", httpx.Client(transport=httpx.MockTransport(_search_hit)), budget,
                            exa_api_key="k", parallel_api_key=None)

    assert budget.spend_used == pytest.approx(0.007)


def test_free_tier_and_cached_searches_cost_nothing():
    budget = BudgetGovernor()
    cache = _DictCache()
    client = httpx.Client(transport=httpx.MockTransport(_search_hit))

    discover_candidate_site("ACME AS", client, budget, exa_api_key=None, parallel_api_key="k")
    discover_candidate_site("ACME AS", client, budget, exa_api_key="k", parallel_api_key=None,
                            cache=cache, date_bucket="2026-09-16")
    spend_after_first_exa = budget.spend_used
    discover_candidate_site("ACME AS", client, budget, exa_api_key="k", parallel_api_key=None,
                            cache=cache, date_bucket="2026-09-16")

    assert spend_after_first_exa == pytest.approx(0.007)
    assert budget.spend_used == pytest.approx(0.007)


def test_the_batch_dollar_limit_stops_paid_searches():
    def handler(request):
        if "exa.ai" in str(request.url):
            raise AssertionError("must not make a paid search past the batch's $ limit")
        return _search_hit(request)

    budget = BudgetGovernor(BudgetLimits(max_requests=2000, max_spend_usd=0.005, max_wall_clock_seconds=1000))

    candidate = discover_candidate_site("ACME AS", httpx.Client(transport=httpx.MockTransport(handler)), budget,
                                        exa_api_key="k", parallel_api_key="k")

    assert candidate == "https://acme.no"  # served by the free tier instead
    assert budget.spend_used == 0


def test_the_run_spend_cap_switches_exa_off_and_the_run_continues_on_free_tiers():
    """A per-batch limit resets every 100 companies, so it can't protect a
    1,000-company run from outspending an account. The run-wide cap does:
    once the next Exa search would cross it, Exa is disabled for the rest of
    the run and the free tiers take over -- nothing stops."""
    exa_calls = []

    def handler(request):
        if "exa.ai" in str(request.url):
            exa_calls.append(1)
            return httpx.Response(200, json={"results": []})
        return _search_hit(request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    health = ProviderHealth(spend_cap_usd=0.02)  # room for exactly 2 Exa searches

    results = [
        discover_candidate_site(f"COMPANY {i} AS", client, BudgetGovernor(), exa_api_key="k",
                                parallel_api_key="k", provider_health=health)
        for i in range(5)
    ]

    assert len(exa_calls) == 2
    assert health.spent_usd == pytest.approx(0.014)
    assert not health.is_available("exa")
    assert "spend cap" in health.disabled["exa"]
    assert results == ["https://acme.no"] * 5


def test_no_run_spend_cap_means_no_run_level_limit():
    health = ProviderHealth()
    assert all(health.reserve_spend("exa", 0.007) for _ in range(2000))
    assert health.is_available("exa")


def test_media_and_directory_domains_found_in_the_corpus_are_blacklisted():
    """Every one confirmed as not a company's own site: streaming/social
    platforms (a Spotify artist page was accepted as a company's website) and
    company-data / listing directories."""
    from src.pipeline.discovery import _is_blacklisted_domain

    for url in (
        "https://open.spotify.com/artist/1X1oXwj8XM1KE7BEHGKyK7",
        "https://www.instagram.com/somecompany/",
        "https://www.youtube.com/@somecompany",
        "https://twitter.com/somecompany",
        "https://x.com/somecompany",
        "https://www.tiktok.com/@somecompany",
        "https://forvalt.no/x",
        "https://areg.no/x",
        "https://listings.no/x",
        "https://lei.report/x",
        "https://datalog.co.uk/x",
        "https://www.smartmeny.no/restauranter/viken/nes/2166/x",
    ):
        assert _is_blacklisted_domain(url), url


def test_blacklisting_x_com_does_not_catch_unrelated_domains_ending_in_x():
    from src.pipeline.discovery import _is_blacklisted_domain

    for url in ("https://www.max.com", "https://www.linux.com", "https://foobox.no", "https://spotifyx.no"):
        assert not _is_blacklisted_domain(url), url


def test_selection_skips_directory_shaped_results_and_takes_the_next_real_site():
    """Search returns several results; a directory listing keyed by the org
    number, from a domain nobody has blacklisted yet, must not be the pick --
    and must not end the round either: the next result is the real site."""
    from src.pipeline.discovery import _first_webpage_url

    results = [
        {"url": "https://some-new-directory.no/816028612"},
        {"url": "https://www.example.no/companies/stavland-holding"},
        {"url": "https://stavlandholding.no/"},
    ]

    assert _first_webpage_url(results) == "https://stavlandholding.no/"


# ---- a page name that only CONTAINS part of the legal name ---------------


def _page_named(name, body=""):
    import json

    return (
        '<html><head><script type="application/ld+json">'
        + json.dumps({"@type": "Organization", "name": name})
        + "</script></head><body>" + body + "</body></html>"
    )


def _verify(url, legal_name, html, org_number="988522651"):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    return verify_discovered_site(url, legal_name, client, BudgetGovernor(), org_number=org_number)


def test_a_page_named_with_only_part_of_the_legal_name_is_not_accepted():
    """Real-world regression: "PE UNITED AS" was given united.no -- a football
    supporters' site titled just "United". partial_ratio scores a name that is
    CONTAINED in the legal name at 100, so it passed. Without the company's org
    number on the page, the page's own name has to cover most of the legal
    name's words; a wrong company is worse than a missing site."""
    assert _verify("https://www.united.no", "PE UNITED AS", _page_named("United")) is None
    assert _verify("https://www.malmeeiendom.no", "MAKE EIENDOMSUTVIKLING AS", _page_named("Malme Eiendom")) is None


def test_the_same_partial_name_is_accepted_when_the_page_shows_the_org_number():
    """Norwegian sites must show their org number; that is hard evidence and
    outranks how the brand is spelled."""
    html = _page_named("United", body="Org.nr: 988 522 651")

    assert _verify("https://www.united.no", "PE UNITED AS", html) is not None


def test_names_that_cover_the_legal_name_are_still_accepted():
    assert _verify("https://kahoot.com", "KAHOOT! AS", _page_named("Kahoot!")) is not None
    assert _verify("https://sandnes-el.no", "SANDNES ELEKTRISKE AS", _page_named("Sandnes Elektriske")) is not None
    # brand written as one word
    assert _verify("https://helenavintage.no", "HELENA VINTAGE AS", _page_named("HelenaVintage")) is not None
    # two of three words (0.67) is enough; the rest is usually a place or a suffix
    assert _verify("https://ruud-pedersen.no", "BJ RUUD-PEDERSEN AS", _page_named("Ruud-Pedersen")) is not None


def test_nordlei_is_blacklisted():
    from src.pipeline.discovery import _is_blacklisted_domain

    assert _is_blacklisted_domain("http://no.nordlei.org/lei/894500T30EDD2IS0B159/abc-bolig-eiendom-as")


def test_generic_descriptor_words_in_the_legal_name_do_not_count_against_a_real_site():
    """Legal names carry descriptors the brand drops: JUSTIFY ADVOKATFIRMA AS is
    "Justify", CUSTOS INVEST AS is "Custos", ODDVAR BJELDE & CO AS BIL- OG
    MASKINSERVICE is "Oddvar Bjelde". Only the distinctive words must be covered."""
    assert _verify("https://www.justify.no", "JUSTIFY ADVOKATFIRMA AS", _page_named("Justify")) is not None
    assert _verify("https://www.custos.no", "CUSTOS INVEST AS", _page_named("Custos")) is not None
    assert _verify(
        "https://www.oddvarbjelde.no/", "ODDVAR BJELDE & CO AS BIL- OG MASKINSERVICE", _page_named("Oddvar Bjelde AS")
    ) is not None


def test_a_distinctive_word_that_is_missing_still_rejects_even_with_generic_words_around_it():
    """"PE UNITED AS" vs "United": PE is not a generic word. "MAKE
    EIENDOMSUTVIKLING AS" vs "Malme Eiendom": the distinctive word MAKE is absent."""
    assert _verify("https://www.united.no", "PE UNITED AS", _page_named("United")) is None
    assert _verify("https://www.malmeeiendom.no", "MAKE EIENDOMSUTVIKLING AS", _page_named("Malme Eiendom")) is None


def test_a_legal_name_made_only_of_generic_words_falls_back_to_the_similarity_gate():
    """Nothing distinctive to cover -> the coverage rule has nothing to say and
    the original name-similarity gate decides, as before."""
    assert _verify("https://byggogeiendom.no", "BYGG OG EIENDOM AS", _page_named("Bygg og Eiendom")) is not None


def test_directories_and_non_company_sites_confirmed_in_the_second_corpus_pass_are_blacklisted():
    """Each fetched and confirmed: a "Norge LEI" company search, a restaurant
    menu directory, a media agency's 403 hosting page and a global lost-and-found
    app."""
    from src.pipeline.discovery import _is_blacklisted_domain

    for url in (
        "https://norgelei.no/detaljert-informasjon/",
        "https://restaurantmeny.no",
        "https://mintpage.prod03.mintmedias.no",
        "https://founditapp.org/",
    ):
        assert _is_blacklisted_domain(url), url
