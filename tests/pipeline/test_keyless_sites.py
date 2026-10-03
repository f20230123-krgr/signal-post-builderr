"""Tests for src/pipeline/keyless_sites.py -- website candidates that need no search key."""
import httpx

from src.orchestrator.budget import BudgetGovernor
from src.pipeline.discovery import verify_discovered_site
from src.pipeline.keyless_sites import (
    SiteHints,
    email_domain_candidate,
    hints_from_registry_record,
    keyless_candidates,
    name_domain_candidates,
    page_confirms_contact,
    page_shows_location,
)


def test_a_company_domain_in_the_registered_email_is_a_candidate_but_a_mailbox_provider_is_not():
    assert email_domain_candidate("post@nysnoinvest.no") == "https://nysnoinvest.no"
    assert email_domain_candidate("kari@mail.firma.no") == "https://firma.no"
    for generic in ("kari@gmail.com", "kari@online.no", "kari@hotmail.no", "x@telenor.com"):
        assert email_domain_candidate(generic) is None
    assert email_domain_candidate(None) is None and email_domain_candidate("not-an-email") is None


def test_name_domains_fold_norwegian_letters_and_drop_the_legal_suffix():
    assert name_domain_candidates("SANDNES ELEKTRISKE AS")[:2] == ["sandneselektriske.no", "sandnes-elektriske.no"]
    assert name_domain_candidates("BYGG & ANLEGG AS")[0] == "bygganlegg.no"
    assert name_domain_candidates("AS") == []
    assert "aasen.no" not in name_domain_candidates("ÅSEN AS")  # å folds to a: asen.no
    assert name_domain_candidates("ÅSEN AS")[0] == "asen.no"


def test_the_first_word_is_a_candidate_only_when_distinctive():
    assert "abax.no" in name_domain_candidates("ABAX GROUP AS", limit=5)
    assert "norge.no" not in name_domain_candidates("NORGE HOLDING AS", limit=5)  # too generic to stand alone


def test_candidates_come_in_trust_order_and_only_when_they_resolve():
    hints = SiteHints(email="post@firma.no", nav_homepages=["https://www.firma-offisiell.no"])

    seen = []

    def resolves(host):
        seen.append(host)
        return host != "firma.no"

    out = keyless_candidates("FIRMA AS", hints, resolves=resolves)

    assert out[0].url == "https://www.firma-offisiell.no" and out[0].kind == "nav"  # NAV's homepage first
    assert "https://firma.no" not in [c.url for c in out]  # did not resolve
    assert seen.index("firma-offisiell.no") < seen.index("firma.no")  # email domain before name guesses


def test_the_same_host_is_not_tried_twice_and_the_list_is_capped():
    hints = SiteHints(email="x@firma.no", nav_homepages=["https://www.firma.no/"])

    out = keyless_candidates("FIRMA AS", hints, resolves=lambda h: True)

    assert [c.url.split("//")[1].removeprefix("www.").rstrip("/") for c in out].count("firma.no") == 1
    assert len(out) <= 3


def test_hints_are_read_from_a_registry_record():
    hints = hints_from_registry_record({
        "epostadresse": "Post@Firma.no", "telefon": "22 22 22 99", "mobil": "930 60 940",
        "forretningsadresse": {"adresse": ["Hammergata 20"], "postnummer": "3264"},
    })

    assert hints.email == "post@firma.no"
    assert hints.phones == ["22222299", "93060940"]
    assert (hints.street, hints.postcode) == ("Hammergata 20", "3264")
    assert hints_from_registry_record({}) == SiteHints()


def test_the_registered_phone_on_the_page_confirms_it_in_any_common_format():
    hints = SiteHints(phones=["22222299"])

    for shown in ("22 22 22 99", "+47 22 22 22 99", "22222299", "tlf. 22.22.22.99"):
        assert page_confirms_contact(f"<p>Ring oss: {shown}</p>", hints), shown


def test_eight_digits_inside_a_longer_number_do_not_confirm_a_phone():
    hints = SiteHints(phones=["22222299"])

    assert not page_confirms_contact("<p>Org.nr 922 222 299</p>", hints)
    assert not page_confirms_contact("<p>Faktura 2222229912</p>", hints)


def test_the_registered_street_and_postcode_together_confirm_but_either_alone_does_not():
    hints = SiteHints(street="Hammergata 20", postcode="3264")

    assert page_confirms_contact("<address>Hammergata 20, 3264 Larvik</address>", hints)
    assert not page_confirms_contact("<address>Hammergata 20, 0150 Oslo</address>", hints)
    assert not page_confirms_contact("<address>Storgata 1, 3264 Larvik</address>", hints)


# --- the gate ----------------------------------------------------------------------


def _verify(html, hints=None, name="NYSNØ KLIMAINVESTERINGER AS", url="https://nysnoinvest.no/"):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    return verify_discovered_site(url, name, client, BudgetGovernor(), org_number="923456789", hints=hints)


BRAND_ONLY = "<html><head><title>Nysnø</title></head><body>Vi investerer i klima.</body></html>"


def test_a_brand_page_that_never_says_the_legal_name_is_rejected_without_corroboration():
    assert _verify(BRAND_ONLY) is None


def test_the_same_page_is_accepted_when_it_shows_the_registered_phone():
    hints = SiteHints(phones=["22222299"])

    fact = _verify(BRAND_ONLY.replace("klima.", "klima. Tlf 22 22 22 99"), hints)

    assert fact is not None and fact.field_name == "official_site"
    assert fact.content_hash and fact.evidence_span


def test_corroboration_never_overrides_a_different_org_number():
    html = BRAND_ONLY.replace("klima.", "klima. Tlf 22 22 22 99. Org.nr: 111 222 333")

    assert _verify(html, SiteHints(phones=["22222299"])) is None


def test_a_page_of_another_company_with_no_matching_phone_is_still_rejected():
    assert _verify(BRAND_ONLY.replace("klima.", "klima. Tlf 99 99 99 99"), SiteHints(phones=["22222299"])) is None


def test_a_discovered_site_is_evidenced_by_the_org_number_line_on_its_own_page():
    fact = _verify("<html><head><title>X</title></head><body><footer>Nysnø AS - Org.nr: 923 456 789</footer></body></html>")

    assert fact is not None
    assert "923 456 789" in fact.evidence_span


def test_only_a_name_guess_needs_proof_beyond_the_name():
    out = keyless_candidates("FIRMA AS", SiteHints(email="x@firma-epost.no", nav_homepages=["https://firma-nav.no"]), resolves=lambda h: True)

    assert {c.kind: c.needs_proof for c in out} == {"nav": False, "email": False, "guess": True}


def test_a_housing_coop_gets_no_email_candidate_because_the_address_is_its_managers():
    hints = SiteHints(email="post@styrerommet.no")

    kinds = {c.kind for c in keyless_candidates("FOSSUM TERRASSE BOLIGSAMEIE", hints, resolves=lambda h: True)}

    assert "email" not in kinds


def test_a_name_match_alone_is_not_enough_when_proof_is_required():
    """sago.com for SAGO AS: the obvious domain can belong to a different company."""
    page = "<html><head><title>Sago AS</title></head><body>Sago AS</body></html>"

    assert _verify(page, name="SAGO AS", url="https://sago.com/") is not None
    assert verify_discovered_site(
        "https://sago.com/", "SAGO AS", httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page))),
        BudgetGovernor(), org_number="923456789", require_proof=True,
    ) is None


def test_proof_by_org_number_or_registered_phone_satisfies_the_requirement():
    def run(page, hints=None):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
        return verify_discovered_site("https://sago.no/", "SAGO AS", client, BudgetGovernor(), org_number="923456789", hints=hints, require_proof=True)

    assert run("<title>Sago</title><footer>Org.nr: 923 456 789</footer>") is not None
    assert run("<title>Sago</title><p>Tlf 22 22 22 99</p>", SiteHints(phones=["22222299"])) is not None


def test_a_redirect_to_a_login_or_error_url_is_not_recorded_as_the_sites_address():
    page = "<title>Nysnø AS</title>Org.nr: 923 456 789"

    def handler(request):
        if request.url.query:
            return httpx.Response(200, text=page)
        return httpx.Response(302, headers={"location": "https://nysnoinvest.no/?error=true&message=login_required"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fact = verify_discovered_site("https://nysnoinvest.no/", "NYSNØ AS", client, BudgetGovernor(), org_number="923456789")

    assert fact.value == "https://nysnoinvest.no/"  # the address we asked for, not the error redirect


def test_the_registered_postcode_and_town_together_show_a_norwegian_location():
    hints = SiteHints(postcode="0183", poststed="OSLO")

    assert page_shows_location("<p>Bernt Ankers Gate 31, 0183 Oslo</p>", hints)
    assert page_shows_location("<p>0183  oslo</p>", hints)
    assert not page_shows_location("<p>Oslo, Norway. PO 0183</p>", hints)
    assert not page_shows_location("<p>Oslo</p>", SiteHints())


def test_a_name_guess_is_accepted_on_name_plus_registered_location_but_not_on_name_alone():
    page = "<html><head><title>Sago AS</title></head><body>Sago AS, Storgata 1, 0183 Oslo</body></html>"
    hints = SiteHints(postcode="0183", poststed="OSLO")

    def run(html, h):
        client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
        return verify_discovered_site("https://sago.no/", "SAGO AS", client, BudgetGovernor(), org_number="923456789", hints=h, require_proof=True)

    assert run(page, hints) is not None
    assert run(page.replace("0183 Oslo", "Austin, Texas"), hints) is None  # same name, wrong country
    assert run(page, None) is None
