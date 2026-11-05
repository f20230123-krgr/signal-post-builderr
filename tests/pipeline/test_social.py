"""Tests for src/pipeline/social.py -- only a company's own profile is a company profile.

The bad URLs below were found in the real 1,000-company corpus: staff members'
personal LinkedIn pages, single Instagram posts, tracking-laden copies."""
from datetime import datetime, timezone

import pytest

from src.pipeline.extract import social_profile_facts, structured_facts
from src.pipeline.social import canonical_social_profile_url, social_profile_link

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize("url", [
    "https://linkedin.com/in/martin-davidsen-51425a111",              # a person
    "https://www.linkedin.com/in/zivid/",
    "https://www.linkedin.com/in/%C3%B8yvind-theie-8169ba7b/",
    "https://www.instagram.com/p/BYeIuUngIfo/",                        # a single post
    "https://www.instagram.com/reel/Cabc123/",
    "https://www.facebook.com/sharer/sharer.php?u=https://x.no",       # share button
    "https://www.facebook.com/events/123456789/",
    "https://www.youtube.com/watch?v=abc123",                          # a video
    "https://www.youtube.com/embed/abc123",
    "https://twitter.com/intent/tweet?text=hi",
    "https://twitter.com/share",
    "https://sv.wikipedia.org/wiki/Acme",                              # not a social platform
    "mailto:raja@endowinvest.no",
    "https://www.linkedin.com/",
    "https://www.facebook.com/",
    "javascript:void(0)",
])
def test_links_that_are_not_a_company_profile_are_rejected(url):
    assert canonical_social_profile_url(url) is None


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/company/nve/?originalSubdomain=no", "https://www.linkedin.com/company/nve"),
    ("https://no.linkedin.com/company/nve/", "https://www.linkedin.com/company/nve"),
    ("https://www.linkedin.com/company/insider-facility-solutions/mycompany/?viewAsMember=true", "https://www.linkedin.com/company/insider-facility-solutions"),
    ("https://www.linkedin.com/school/ntnu", "https://www.linkedin.com/school/ntnu"),
    ("https://nb-no.facebook.com/acmenorge/?ref=page", "https://www.facebook.com/acmenorge"),
    ("https://www.facebook.com/profile.php?id=100063763590152&sk=info", "https://www.facebook.com/profile.php?id=100063763590152"),
    ("https://www.facebook.com/pages/Acme-AS/12345678", "https://www.facebook.com/pages/Acme-AS/12345678"),
    ("https://www.facebook.com/p/Maskinentrepren%C3%B8r-Stian-A-Olsen-AS-61555049420983/", "https://www.facebook.com/p/Maskinentreprenør-Stian-A-Olsen-AS-61555049420983"),
    ("https://www.instagram.com/bilforhandler1/?hl=nb", "https://www.instagram.com/bilforhandler1/"),
    ("https://instagram.com/acme.norge", "https://www.instagram.com/acme.norge/"),
    ("https://twitter.com/acme", "https://twitter.com/acme"),
    ("https://x.com/acme?lang=no", "https://x.com/acme"),
    ("https://www.tiktok.com/@acme", "https://www.tiktok.com/@acme"),
    ("https://www.youtube.com/channel/UCT3I0QGAfONRJO2GluzIuvg?sub_confirmation=1", "https://www.youtube.com/channel/UCT3I0QGAfONRJO2GluzIuvg"),
    ("https://www.youtube.com/@acme", "https://www.youtube.com/@acme"),
])
def test_a_company_profile_is_reduced_to_its_canonical_form(url, expected):
    assert canonical_social_profile_url(url) == expected


def test_the_published_link_is_written_the_way_builderrs_reference_data_writes_it():
    """Builderr's sample writes https://facebook.com/afgruppen: no www., no country or mobile
    prefix, no trailing slash, x.com for twitter.com, no tracking parameters."""
    link, key = social_profile_link("https://www.linkedin.com/company/nve/?originalSubdomain=no")

    assert link == "https://linkedin.com/company/nve"
    assert key == "https://www.linkedin.com/company/nve"
    assert social_profile_link("https://www.instagram.com/bilforhandler1/?hl=nb")[0] == "https://instagram.com/bilforhandler1"
    assert social_profile_link("http://twitter.com/sailormusicno")[0] == "https://x.com/sailormusicno"
    assert social_profile_link("https://m.facebook.com/Constokonsern/")[0] == "https://facebook.com/Constokonsern"
    assert social_profile_link("https://www.facebook.com/profile.php?id=100057441009551&ref=x")[0] == (
        "https://facebook.com/profile.php?id=100057441009551"
    )


def test_the_same_profile_linked_three_ways_is_one_claim():
    html = (
        '<a href="https://www.linkedin.com/company/nve/">a</a>'
        '<a href="https://no.linkedin.com/company/nve?trk=x">b</a>'
        '<a href="https://www.linkedin.com/company/nve/?originalSubdomain=no">c</a>'
    )

    facts = social_profile_facts(html, "https://nve.no", NOW)

    assert [f.value for f in facts] == ["https://linkedin.com/company/nve"]


def test_posts_staff_and_share_links_in_a_footer_publish_nothing():
    html = (
        '<a href="https://www.instagram.com/p/BYeIuUngIfo/">post</a>'
        '<a href="https://www.linkedin.com/in/someone/">our CEO</a>'
        '<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>'
        '<a href="https://www.facebook.com/acmenorge">Facebook</a>'
    )

    assert [f.value for f in social_profile_facts(html, "https://acme.no", NOW)] == ["https://facebook.com/acmenorge"]


def test_sameas_in_json_ld_is_filtered_the_same_way():
    html = (
        '<script type="application/ld+json">{"@type":"Organization","name":"Acme AS","sameAs":'
        '["https://www.linkedin.com/in/ceo/","https://www.linkedin.com/company/acme/?trk=1","https://en.wikipedia.org/wiki/Acme"]}</script>'
    )

    values = [f.value for f in structured_facts(html, "https://acme.no", NOW) if f.field_name == "company_profile"]

    assert values == ["https://linkedin.com/company/acme"]


def test_a_malformed_link_is_not_a_profile_and_not_a_crash():
    assert canonical_social_profile_url("https://[::1/linkedin.com/company/x") is None
    assert social_profile_facts('<a href="http://[bad">x</a><a href="https://www.facebook.com/acme">f</a>', "https://acme.no", NOW)[0].value == "https://facebook.com/acme"


@pytest.mark.parametrize("url,expected", [
    ("https://www.linkedin.com/company/ragasco-as/mycompany/", "https://linkedin.com/company/ragasco-as"),
    ("https://www.linkedin.com/company/rsv-gruppen/posts/", "https://linkedin.com/company/rsv-gruppen"),
    ("https://www.linkedin.com/company/845627/admin/feed/posts/", "https://linkedin.com/company/845627"),
    ("https://no.linkedin.com/company/entra-asa", "https://linkedin.com/company/entra-asa"),
])
def test_a_linkedin_company_is_published_as_the_company_page_never_one_of_its_tabs(url, expected):
    assert social_profile_link(url)[0] == expected


def test_a_facebook_policy_page_is_not_a_page_and_the_people_form_is():
    assert canonical_social_profile_url("https://mbasic.facebook.com/privacy/policies/cookies/printable/") is None
    assert canonical_social_profile_url("https://www.facebook.com/terms") is None
    assert canonical_social_profile_url("https://facebook.com/people/hammaren-barnehage/100053974317354") == (
        "https://www.facebook.com/people/hammaren-barnehage/100053974317354"
    )


# --- profiles shipped as script data ------------------------------------------------------------

from src.pipeline.social import identity_tokens, resembles_company, script_social_candidates  # noqa: E402


def test_identity_tokens_are_the_distinctive_name_words_the_joined_name_and_the_domain_label():
    assert identity_tokens("AF GRUPPEN ASA", "https://www.afgruppen.no/") == {"afgruppen"}
    assert identity_tokens("NORDIC DOOR AS", "https://www.nordicdoor.no") >= {"nordicdoor"}
    assert "kitron" in identity_tokens("KITRON ASA", "https://www.kitron.com")
    assert identity_tokens("AS", "") == set()


def test_an_account_resembles_the_company_when_its_name_contains_or_is_part_of_a_token():
    tokens = {"afgruppen", "kitron"}

    assert resembles_company("https://www.linkedin.com/company/af-gruppen", tokens)
    assert resembles_company("https://www.facebook.com/kitron", tokens)
    assert resembles_company("https://twitter.com/kitron_group", tokens)
    assert not resembles_company("https://www.linkedin.com/company/some-partner", tokens)
    assert not resembles_company("https://www.facebook.com/ab", {"ab"})  # too short to mean anything


def test_profiles_in_script_data_are_found_with_escaped_slashes_and_quoted_as_found():
    html = (
        r'<script>window.__DATA__={"social":{"facebook":"https:\/\/www.facebook.com\/afgruppen",'
        r'"linkedin":"https:\/\/no.linkedin.com\/company\/af-gruppen\/","partner":"https://www.linkedin.com/company/other-firm"}}</script>'
    )

    found = script_social_candidates(html, {"afgruppen"})

    assert [f[0] for f in found] == ["https://facebook.com/afgruppen", "https://linkedin.com/company/af-gruppen"]
    assert found[0][2] in html  # the span is the text exactly as the page has it


def test_tracking_pixels_share_links_and_a_partners_profile_in_script_data_are_not_company_profiles():
    html = (
        '<script>fbq("init"); img="https://www.facebook.com/tr?id=123&ev=PageView";'
        'share="https://www.facebook.com/sharer/sharer.php?u=x"; p="https://www.instagram.com/p/ABC123/";'
        'other="https://www.facebook.com/somebodyelse"</script>'
    )

    assert script_social_candidates(html, {"afgruppen"}) == []


def test_facebooks_old_pg_page_form_is_the_same_page():
    assert social_profile_link("https://www.facebook.com/pg/MtmSkogservice/about/") == (
        "https://facebook.com/MtmSkogservice", "https://www.facebook.com/MtmSkogservice",
    )


def test_a_profile_is_the_companys_own_only_when_its_name_resembles_the_company_or_is_an_id():
    """Hand-checked on Builderr's 100-company sample: this removed a parent group's accounts,
    an owner's personal brand and a supplier's channel, and none of Builderr's confirmed 76."""
    from src.pipeline.social import canonical_social_profile_url, plausibly_own_profile

    def own(url, name, site):
        return plausibly_own_profile(canonical_social_profile_url(url), name, site)

    assert own("https://www.linkedin.com/company/peab/", "PEAB BYGG AS", "https://peab.no")
    assert own("https://www.instagram.com/nspnorge/", "NORDIC SUPPLY PARTNER AS", "https://www.nsp.no")
    assert own("https://www.youtube.com/channel/UC-L9GGxaVCNOCMziy1rXqIw", "HJELPEMIDDELSPESIALISTEN AS", "https://hm-spes.no")
    assert own("https://www.facebook.com/profile.php?id=100057441009551", "RSV GRUPPEN AS", "https://rsvgruppen.no")
    assert not own("https://www.linkedin.com/company/eltera-gruppen/", "VALDRES INSTALLASJON AS", "https://valdres-installasjon.no")
    assert not own("https://www.youtube.com/@Prevent1942/playlists", "NORDIC SUPPLY PARTNER AS", "https://www.nsp.no")
    assert not own("https://www.instagram.com/orsolyahaarberg/", "FJELLHEIMEN GALLERI AS", "https://fjellheimengalleri.no")


def test_facebook_feed_posts_are_not_profiles_and_youtube_tabs_are_dropped():
    from src.pipeline.social import canonical_social_profile_url

    assert canonical_social_profile_url("https://www.facebook.com/599407360228640_1627673669063159") is None
    assert canonical_social_profile_url("https://facebook.com/1552125346617992") is None
    assert social_profile_link("https://www.youtube.com/channel/UCSAz4EpgiM1HlNrjPy38_Lw/featured")[0] == (
        "https://youtube.com/channel/UCSAz4EpgiM1HlNrjPy38_Lw"
    )


def test_a_foreign_parents_group_or_branch_account_is_not_the_companys_own():
    """Official audit (23 Oct): social accounts belonging to other companies. Hand-checked on
    200 never-seen companies: the UK parent's "epsco-ltd", the Danish "stema-shipping-a-s",
    the group's "autoin-gruppen" and a chain's "uniteperformancelarvik" were linked from the
    company's site but are not its own. A company that is itself the group keeps its account."""
    from src.pipeline.social import plausibly_own_profile

    assert not plausibly_own_profile("https://www.linkedin.com/company/epsco-ltd", "EPSCO NORGE AS", "https://www.epsco.no")
    assert not plausibly_own_profile("https://dk.linkedin.com/company/stema-shipping-a-s", "MIBAU STEMA NORGE AS", "https://www.mibau-stema.com")
    assert not plausibly_own_profile("https://www.linkedin.com/company/autoin-gruppen", "AUTOIN BILSKADE AS", "https://www.autoin.no")
    assert not plausibly_own_profile(
        "https://www.instagram.com/uniteperformancelarvik/", "UNITE PERFORMANCE AS", "https://uniteperformance.no", "Storgata 1, 3126 TØNSBERG"
    )
    assert plausibly_own_profile("https://www.facebook.com/kitrongroup", "KITRON ASA", "https://www.kitron.com")
    assert plausibly_own_profile("https://www.linkedin.com/company/tormatic-as", "TORMATIC AS", "https://www.tormatic.no")
    assert plausibly_own_profile("https://www.facebook.com/byggmesterolaisenbodo", "BYGGMESTER OLAISEN AS", "https://byggmesterolaisen.no", "Gata 2, 8006 BODØ")


def test_a_profile_with_norwegian_letters_is_written_the_way_the_browser_sends_it():
    # Builderr's data writes "velv%C3%A6re"; the same page linked with "æ" must match it letter for letter.
    for raw in ("https://www.linkedin.com/company/the-well---en-verden-av-velvære/",
                "https://www.linkedin.com/company/the-well---en-verden-av-velv%C3%A6re"):
        link, key = social_profile_link(raw)
        assert link == "https://linkedin.com/company/the-well---en-verden-av-velv%C3%A6re"
        assert key == "https://www.linkedin.com/company/the-well---en-verden-av-velvære"
    assert social_profile_link("https://www.facebook.com/profile.php?id=100057441009551")[0] == \
        "https://facebook.com/profile.php?id=100057441009551"
    assert social_profile_link("https://www.youtube.com/@gozaptec")[0] == "https://youtube.com/@gozaptec"
