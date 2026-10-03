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


def test_the_published_link_keeps_the_companys_own_form_without_tracking_parameters():
    link, key = social_profile_link("https://www.linkedin.com/company/nve/?originalSubdomain=no")

    assert link == "https://www.linkedin.com/company/nve/"
    assert key == "https://www.linkedin.com/company/nve"
    assert social_profile_link("https://www.instagram.com/bilforhandler1/?hl=nb")[0] == "https://www.instagram.com/bilforhandler1/"


def test_the_same_profile_linked_three_ways_is_one_claim():
    html = (
        '<a href="https://www.linkedin.com/company/nve/">a</a>'
        '<a href="https://no.linkedin.com/company/nve?trk=x">b</a>'
        '<a href="https://www.linkedin.com/company/nve/?originalSubdomain=no">c</a>'
    )

    facts = social_profile_facts(html, "https://nve.no", NOW)

    assert [f.value for f in facts] == ["https://www.linkedin.com/company/nve/"]


def test_posts_staff_and_share_links_in_a_footer_publish_nothing():
    html = (
        '<a href="https://www.instagram.com/p/BYeIuUngIfo/">post</a>'
        '<a href="https://www.linkedin.com/in/someone/">our CEO</a>'
        '<a href="https://www.facebook.com/sharer/sharer.php?u=x">share</a>'
        '<a href="https://www.facebook.com/acmenorge">Facebook</a>'
    )

    assert [f.value for f in social_profile_facts(html, "https://acme.no", NOW)] == ["https://www.facebook.com/acmenorge"]


def test_sameas_in_json_ld_is_filtered_the_same_way():
    html = (
        '<script type="application/ld+json">{"@type":"Organization","name":"Acme AS","sameAs":'
        '["https://www.linkedin.com/in/ceo/","https://www.linkedin.com/company/acme/?trk=1","https://en.wikipedia.org/wiki/Acme"]}</script>'
    )

    values = [f.value for f in structured_facts(html, "https://acme.no", NOW) if f.field_name == "company_profile"]

    assert values == ["https://www.linkedin.com/company/acme/"]


def test_a_malformed_link_is_not_a_profile_and_not_a_crash():
    assert canonical_social_profile_url("https://[::1/linkedin.com/company/x") is None
    assert social_profile_facts('<a href="http://[bad">x</a><a href="https://www.facebook.com/acme">f</a>', "https://acme.no", NOW)[0].value == "https://www.facebook.com/acme"
