"""Tests for src/pipeline/careers.py -- hiring signals from a company's careers page.

Official feedback on the 3rd submission: hiring signals were 0.0% covered, and the
pool's hiring facts are careers pages on company sites. Earlier feedback still
holds: a bare "Careers" heading, or a page saying there are no openings, is not
evidence of hiring."""
from datetime import datetime, timezone

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor
from src.pipeline.careers import (
    CAREERS_VALUE_PREFIX,
    careers_page_facts,
    find_careers_links,
    is_careers_url,
    role_links,
)
from src.pipeline.crawl import DEFAULT_ATS_DOMAINS, MAX_CAREERS_PAGES, crawl
from src.pipeline.extract import RawFact, extract
from src.pipeline.crawl import FetchedPage
from src.pipeline.verify import verify
from tests.pipeline.test_crawl import _entity

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)
SITE = "example.no"


def _links(html, ats=None):
    return find_careers_links(html, "https://www.example.no/", SITE, ats or set(DEFAULT_ATS_DOMAINS))


# --- finding the page from links the site already has -----------------------


def test_finds_a_careers_link_by_its_text_or_its_path():
    html = '<a href="/om-oss">About</a><a href="/karriere">Karriere</a><a href="/x/jobs/">Open</a>'

    assert _links(html) == ["https://www.example.no/karriere", "https://www.example.no/x/jobs/"]


def test_does_not_mistake_membership_or_working_methods_for_a_careers_link():
    html = (
        '<a href="/medlemskap/bli-medlem">Bli medlem</a>'
        '<a href="/barekraft">Slik jobber vi med bærekraft</a>'
        '<a href="/jobbe-med-oss-tjenester">Vi tilbyr jobbtrening</a>'
    )

    assert _links(html) == []


def test_follows_a_careers_subdomain_and_a_known_ats_but_not_other_sites():
    html = (
        '<a href="https://karriere.example.no/">Karriere</a>'
        '<a href="https://acme.recman.no/">Ledige stillinger</a>'
        '<a href="https://other-company.com/careers">Careers</a>'
    )

    assert set(_links(html)) == {"https://karriere.example.no/", "https://acme.recman.no/"}


def test_never_returns_more_than_the_limit_or_the_same_page_twice():
    html = "".join(f'<a href="/jobs/{i}">Jobs</a>' for i in range(5)) + '<a href="/jobs/0">Jobs</a>'

    assert len(_links(html)) == MAX_CAREERS_PAGES


def test_is_careers_url_recognises_paths_hosts_and_ats_platforms():
    assert is_careers_url("https://example.no/karriere")
    assert is_careers_url("https://careers.example.no/")
    assert is_careers_url("https://acme.recman.no/", DEFAULT_ATS_DOMAINS)
    assert not is_careers_url("https://example.no/om-oss")


# --- proving it -------------------------------------------------------------


def _page(body, title="Karriere - Example AS"):
    return f"<html><head><title>{title}</title></head><body>{body}</body></html>"


def test_a_page_listing_open_roles_is_a_role_level_signal_with_the_title_as_span():
    html = _page(
        '<a href="/stillinger/backend-utvikler">Backend-utvikler</a><a href="/stillinger/elektriker">Elektriker</a>'
        '<a href="/stillinger/">Se alle stillinger</a>'
    )

    [fact] = careers_page_facts(html, "https://www.example.no/karriere", NOW)

    assert fact.field_name == "hiring_signal"
    assert fact.value == f'{CAREERS_VALUE_PREFIX} lists 2 open roles, e.g. "Backend-utvikler": https://www.example.no/karriere'
    assert fact.evidence_span == "Backend-utvikler"
    assert fact.evidence_span in html


def test_navigation_links_are_not_counted_as_roles():
    html = _page('<a href="/jobs/all">Se alle stillinger</a><a href="/jobs/apply">Søk nå</a><a href="/jobs/more">Read more</a>')

    assert role_links(html, "https://www.example.no/karriere") == []


def test_a_careers_page_without_visible_listings_claims_only_that_the_page_exists():
    """Listings loaded by script leave no roles in the static HTML. The claim is
    the page, never that a role is open."""
    [fact] = careers_page_facts(_page("<div id='app'></div>"), "https://www.example.no/karriere", NOW)

    assert fact.value == f"{CAREERS_VALUE_PREFIX}: https://www.example.no/karriere"
    assert "open role" not in fact.value and "hiring" not in fact.value.lower()
    assert fact.evidence_span == "Karriere - Example AS"


def test_a_page_that_says_there_are_no_openings_publishes_nothing():
    for sentence in ("Ingen ledige stillinger for øyeblikket", "We don't have any open positions right now", "We are not hiring"):
        assert careers_page_facts(_page(f"<p>{sentence}</p>"), "https://www.example.no/karriere", NOW) == []


def test_an_error_page_or_a_page_with_no_title_publishes_nothing():
    assert careers_page_facts(_page("<p>x</p>", title="404 - Side ikke funnet"), "https://www.example.no/karriere", NOW) == []
    assert careers_page_facts("<html><body>Jobs</body></html>", "https://www.example.no/karriere", NOW) == []


def test_a_page_that_is_not_a_careers_url_and_lists_no_roles_publishes_nothing():
    assert careers_page_facts(_page("<p>Hello</p>", title="Karriere"), "https://www.example.no/om-oss", NOW) == []


def test_an_ats_page_is_accepted_even_when_its_title_is_generic():
    facts = careers_page_facts(_page("<div></div>", title="Example AS"), "https://acme.recman.no/", NOW, ats_domains=DEFAULT_ATS_DOMAINS)

    assert [f.value for f in facts] == [f"{CAREERS_VALUE_PREFIX}: https://acme.recman.no/"]


def test_extract_runs_the_careers_check_only_on_careers_urls():
    body = '<a href="/jobs/a-role">A role</a><a href="/jobs/b-role">B role</a>'

    on_careers = extract(FetchedPage("https://www.example.no/karriere", _page(body), NOW, EvidenceState.AVAILABLE), now=lambda: NOW)
    elsewhere = extract(FetchedPage("https://www.example.no/blog", _page(body), NOW, EvidenceState.AVAILABLE), now=lambda: NOW)

    assert any(f.field_name == "hiring_signal" for f in on_careers)
    assert not any(f.field_name == "hiring_signal" for f in elsewhere)


# --- verifying it -----------------------------------------------------------


def _fact(url, value=f"{CAREERS_VALUE_PREFIX}: https://example.com/karriere", linked_from=None):
    return RawFact("hiring_signal", value, url, "text", NOW, linked_from=linked_from)


def test_a_careers_page_on_the_companys_own_site_is_confirmed():
    assert verify(_fact("https://example.com/karriere"), _entity("https://example.com/")) is not None


def test_a_careers_page_found_on_a_site_the_company_only_occupies_a_page_of_is_dropped():
    """samvirkebarnehagene.no/vare-barnehager/<branch>/ is one kindergarten of a
    group: the group's careers page is not this company's."""
    entity = _entity("https://example.com/our-branches/branch-x/")

    assert verify(_fact("https://example.com/karriere"), entity) is None


def test_a_language_prefix_is_not_depth():
    assert verify(_fact("https://example.com/karriere"), _entity("https://example.com/en/")) is not None


def test_a_careers_page_on_an_ats_is_confirmed_through_the_link_chain():
    fact = _fact("https://acme.recman.no/", f"{CAREERS_VALUE_PREFIX}: https://acme.recman.no/", linked_from="https://example.com/")

    confirmed = verify(fact, _entity("https://example.com/"))

    assert confirmed is not None and confirmed.linked_from == "https://example.com/"


# --- crawling it ------------------------------------------------------------


def _client(routes):
    def handler(request):
        text = routes.get(str(request.url))
        return httpx.Response(200, text=text) if text is not None else httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_crawl_follows_the_careers_link_from_the_homepage_without_guessing_paths():
    requested = []
    routes = {
        "https://example.com/": '<a href="/karriere">Karriere</a>',
        "https://example.com/karriere": _page("<p>x</p>"),
    }

    def handler(request):
        requested.append(str(request.url))
        text = routes.get(str(request.url))
        return httpx.Response(200, text=text) if text else httpx.Response(404)

    pages = crawl(
        _entity("https://example.com/"), BudgetGovernor(), client=httpx.Client(transport=httpx.MockTransport(handler)),
        follow_careers=True, ats_domains=DEFAULT_ATS_DOMAINS,
    )

    assert [p.url for p in pages] == ["https://example.com/", "https://example.com/karriere"]
    assert requested == ["https://example.com/", "https://example.com/karriere"]
    assert pages[1].linked_from is None  # same domain: plain provenance


def test_crawl_does_not_follow_careers_links_unless_asked():
    routes = {"https://example.com/": '<a href="/karriere">Karriere</a>', "https://example.com/karriere": _page("x")}

    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes))

    assert [p.url for p in pages] == ["https://example.com/"]


def test_crawl_reaches_a_careers_subdomain_through_a_link_chain():
    routes = {
        "https://example.com/": '<a href="https://karriere.example.com/">Karriere</a>',
        "https://karriere.example.com/": _page("<p>x</p>"),
    }

    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes), follow_careers=True)

    sub = next(p for p in pages if p.url == "https://karriere.example.com/")
    assert sub.linked_from == "https://example.com/"
    assert sub.fetch_state == EvidenceState.AVAILABLE


def test_crawl_never_follows_more_than_the_cap_of_careers_pages():
    links = "".join(f'<a href="/jobs{i}/">Jobs</a>' for i in range(10))
    routes = {"https://example.com/": links, **{f"https://example.com/jobs{i}/": _page("x") for i in range(10)}}

    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes), follow_careers=True)

    assert len(pages) == 1 + MAX_CAREERS_PAGES


# --- regressions found running 100 real companies --------------------------


def test_footer_and_terms_links_on_a_job_ad_page_are_not_roles():
    """A webcruiter job ad linked "Bruksvilkår" (terms of use) under the ad's own
    URL; it was published as 'lists 2 open roles, e.g. "Bruksvilkår"'."""
    url = "https://acme.webcruiter.no/Main/Recruit/Public/5173322543"
    html = _page(
        '<a href="/Main/Recruit/Public/5173322543/terms">Bruksvilkår</a>'
        '<a href="/Main/Recruit/Public/5180563505">Personvern</a><a href="/jobs/12345">Cookies</a>',
        title="Stilling - Acme",
    )

    assert role_links(html, url, DEFAULT_ATS_DOMAINS) == []
    [fact] = careers_page_facts(html, url, NOW, ats_domains=DEFAULT_ATS_DOMAINS)
    assert fact.value == f"{CAREERS_VALUE_PREFIX}: {url}"  # the page, never invented roles


def test_roles_linked_to_a_general_job_board_are_not_this_companys_roles():
    html = _page('<a href="https://www.finn.no/job/ad/111111">Sjåfør</a><a href="https://www.finn.no/job/ad/222222">Lagerarbeider</a>')

    assert role_links(html, "https://www.example.no/karriere", DEFAULT_ATS_DOMAINS) == []


def test_roles_on_a_known_ats_count_for_the_companys_careers_page():
    html = _page(
        '<a href="https://acme.recman.no/job_post.php?id=1">Sjåfør</a><a href="https://acme.recman.no/job_post.php?id=2">Lagerarbeider</a>'
    )

    assert [t for t, _ in role_links(html, "https://www.example.no/karriere", DEFAULT_ATS_DOMAINS)] == ["Sjåfør", "Lagerarbeider"]


def _claim(value):
    from tests.conftest import available_claim

    return available_claim(value)


def test_assembly_keeps_one_careers_page_not_one_per_sub_page():
    from src.pipeline.assemble import _limit_careers_claims

    claims = [
        _claim(f"{CAREERS_VALUE_PREFIX}: https://s.dk/career/job-opportunities"),
        _claim(f"{CAREERS_VALUE_PREFIX}: https://s.dk/career"),
        _claim(f"{CAREERS_VALUE_PREFIX}: https://s.dk/career/apprentices-and-trainees"),
        _claim("Renholder (published 2026-09-02) - NAV"),
    ]

    kept = [c.value for c in _limit_careers_claims(claims)]

    assert kept == [
        f"{CAREERS_VALUE_PREFIX}: https://s.dk/career/job-opportunities",
        f"{CAREERS_VALUE_PREFIX}: https://s.dk/career",
        "Renholder (published 2026-09-02) - NAV",
    ]


def test_assembly_prefers_role_level_careers_claims_and_never_drops_a_nav_ad():
    from src.pipeline.assemble import _limit_careers_claims

    role = f'{CAREERS_VALUE_PREFIX} lists 3 open roles, e.g. "Elektriker": https://s.no/karriere'
    claims = [
        _claim(f"{CAREERS_VALUE_PREFIX}: https://s.no/a"),
        _claim(f"{CAREERS_VALUE_PREFIX}: https://s.no/b"),
        _claim(role),
        _claim("Elektriker - NAV"),
    ]

    assert [c.value for c in _limit_careers_claims(claims)] == [role, "Elektriker - NAV"]


def test_the_summary_states_a_careers_claim_as_it_is_and_leads_a_nav_ad_with_hiring():
    from src.synthesis import build_summary
    from tests.conftest import make_profile

    bare = make_profile(org_number="923609016", hiring_signals=[_claim(f"{CAREERS_VALUE_PREFIX}: https://s.no/karriere")])
    nav = make_profile(org_number="923609016", hiring_signals=[_claim("Elektriker - NAV")])

    # A bare careers page is stated as a page, never as "hiring"; an ad is stated as hiring.
    assert "Online: careers page." in build_summary(bare).text
    assert "hiring:" not in build_summary(bare).text
    assert "hiring: Elektriker" in build_summary(nav).text


def test_a_listing_at_the_root_of_a_careers_subdomain_counts_roles_under_its_own_address():
    """career.ors-consulting.com/ lists ads at /jobs/<id>-<slug>: they live under the
    page's own address and are exactly the roles."""
    html = _page(
        '<a href="/jobs/7116722-senior-konsulent">Senior Konsulent Consulting · Esbjerg</a>'
        '<a href="/jobs/6909218-senior-consultant">Senior Consultant Consulting · Utrecht</a>',
        title="Careers - ORS",
    )

    [fact] = careers_page_facts(html, "https://career.example.no/", NOW, ats_domains=DEFAULT_ATS_DOMAINS)

    assert "lists 2 open roles" in fact.value


def test_malformed_links_on_a_real_page_are_skipped_not_fatal():
    html = (
        '<a href="http://example.no:99999/karriere">Karriere</a><a href="http://[::1/jobs">Jobs</a>'
        '<a href="https://www.example.no/karriere">Karriere</a>'
    )

    assert _links(html) == ["https://www.example.no/karriere"]
    assert crawl(
        _entity("https://example.com/"), BudgetGovernor(),
        client=_client({"https://example.com/": html.replace("example.no", "example.com")}), follow_careers=True, follow_news=True,
    )[0].url == "https://example.com/"


def test_team_introductions_and_testimonials_under_a_jobs_path_are_not_open_roles():
    """consto.no/jobb/ listed 'Møt Anna og Lars' and dips.com a person's name and title as
    'open roles'. A bare /jobb/<slug> is not an ad."""
    html = _page('<a href="/jobb/mot-anna-og-lars">Møt Anna og Lars</a><a href="/jobb/jon-bratberg">Jon Bratberg Produktsjef</a>')

    assert role_links(html, "https://www.example.no/jobb/") == []
    [fact] = careers_page_facts(html, "https://www.example.no/jobb/", NOW)
    assert fact.value == f"{CAREERS_VALUE_PREFIX}: https://www.example.no/jobb/"  # the page, not invented roles


def test_a_job_path_with_a_numeric_id_or_an_ad_word_is_an_ad():
    html = _page(
        '<a href="/jobs/7116722-senior-konsulent">Senior Konsulent Oslo</a>'
        '<a href="/ledig-stilling/elektriker-bergen">Elektriker Bergen</a>'
    )

    assert [t for t, _ in role_links(html, "https://www.example.no/karriere")] == ["Senior Konsulent Oslo", "Elektriker Bergen"]


def test_an_ats_dashboard_or_login_page_is_not_a_careers_page():
    """app.teamtailor.com/companies/<id>/dashboard was published as a careers page."""
    url = "https://app.teamtailor.com/companies/HbC9EqmTjjI@eu/dashboard"

    assert not is_careers_url(url, DEFAULT_ATS_DOMAINS)
    assert careers_page_facts(_page("<p>x</p>", title="Teamtailor"), url, NOW, ats_domains=DEFAULT_ATS_DOMAINS) == []
    assert not is_careers_url("https://acme.recman.no/login", DEFAULT_ATS_DOMAINS)
    assert is_careers_url("https://acme.recman.no/", DEFAULT_ATS_DOMAINS)


def test_when_a_big_sitemap_is_trimmed_careers_and_news_pages_survive_the_cut():
    from src.pipeline.crawl import MAX_SITEMAP_URLS, _discover_sitemap_urls

    locs = "".join(f"<url><loc>https://example.com/about/team-{i}</loc></url>" for i in range(20))
    locs += "<url><loc>https://example.com/news/latest</loc></url><url><loc>https://example.com/karriere/ledige-stillinger</loc></url>"
    sitemap = f"<urlset>{locs}</urlset>"

    def handler(request):
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow:\n")
        return httpx.Response(200, text=sitemap) if url.endswith("/sitemap.xml") else httpx.Response(404)

    found = _discover_sitemap_urls("https://example.com/", httpx.Client(transport=httpx.MockTransport(handler)), lambda s: None, BudgetGovernor(), None, "2026-10-04")

    assert len(found) == MAX_SITEMAP_URLS
    assert found[0] == "https://example.com/karriere/ledige-stillinger" and found[1] == "https://example.com/news/latest"


def test_contact_and_about_pages_are_found_from_the_sites_own_links_contact_first():
    from src.pipeline.careers import find_info_links

    html = '<a href="/om-oss">Om oss</a><a href="/hjem">Hjem</a><a href="/kontakt-oss">Kontakt oss</a><a href="https://other.no/kontakt">Kontakt</a>'

    assert find_info_links(html, "https://www.example.no/", "example.no") == ["https://www.example.no/kontakt-oss", "https://www.example.no/om-oss"]


def test_the_crawl_follows_the_sites_contact_link_instead_of_guessing_paths():
    requested = []
    routes = {"https://example.com/": '<a href="/kontakt">Kontakt</a>', "https://example.com/kontakt": "<p>Org.nr 923 609 016</p>"}

    def handler(request):
        requested.append(str(request.url))
        text = routes.get(str(request.url))
        return httpx.Response(200, text=text) if text else httpx.Response(404)

    crawl(_entity("https://example.com/"), BudgetGovernor(), client=httpx.Client(transport=httpx.MockTransport(handler)), follow_info=True)

    assert requested == ["https://example.com/", "https://example.com/kontakt"]
