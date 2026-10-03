"""Tests for src/pipeline/news.py -- dated headlines from a company's own site."""
from datetime import date, datetime, timezone

import httpx

from src.models.profile import EvidenceState
from src.orchestrator.budget import BudgetGovernor
from src.pipeline.crawl import FetchedPage, crawl
from src.pipeline.extract import extract
from src.pipeline.news import find_news_links, news_item_facts, news_items, parse_date
from src.pipeline.verify import verify
from tests.pipeline.test_crawl import _entity

TODAY = date(2026, 10, 3)
NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def test_dates_are_read_in_the_formats_norwegian_and_english_sites_use():
    for text in ("2024-05-12", "12.05.2024", "12. 5. 2024", "12. mai 2024", "12 May 2024", "May 12, 2024", "12. mai. 2024", "Publisert 12. MAI 2024 kl 10"):
        assert parse_date(text, TODAY) == "2024-05-12", text
    assert parse_date("1. des 2025", TODAY) == "2025-12-01"
    assert parse_date("Mars 3rd, 2025", TODAY) == "2025-03-03"


def test_implausible_dates_are_not_dates():
    assert parse_date("31.02.2024", TODAY) is None  # no such day
    assert parse_date("copyright 1999-01-01", TODAY) is None  # before 2000
    assert parse_date("2030-01-01", TODAY) is None  # more than a year ahead
    assert parse_date("no date here", TODAY) is None


def test_a_date_in_a_time_attribute_pairs_with_the_headline():
    html = '<ul><li><time datetime="2026-09-24">24. sep</time><h3><a href="/n/1">Vi åpner nytt kontor i Bergen</a></h3></li></ul>'

    assert news_items(html, TODAY) == [("Vi åpner nytt kontor i Bergen", "2026-09-24")]


def test_a_visible_norwegian_date_pairs_with_the_headline_in_the_same_item():
    html = (
        "<article><span>12. mai 2026</span><h2>Ny avtale med Equinor</h2><p>tekst</p></article>"
        "<article><h2>Kvartalsrapport klar</h2><span>01.04.2026</span></article>"
    )

    assert news_items(html, TODAY) == [("Ny avtale med Equinor", "2026-05-12"), ("Kvartalsrapport klar", "2026-04-01")]


def test_a_date_with_no_headline_and_a_headline_with_no_date_are_not_items():
    assert news_items("<ul><li>12. mai 2026</li><li><h3>En overskrift uten dato</h3></li></ul>", TODAY) == []


def test_the_same_headline_is_not_listed_twice_and_the_list_is_capped():
    one = "<li><time datetime='2026-01-01'></time><h3>Samme overskrift her</h3></li>" * 3
    many = "".join(f"<li><time datetime='2026-01-{i % 28 + 1:02d}'></time><h3>Overskrift nummer {i:02d}</h3></li>" for i in range(30))

    assert len(news_items(one, TODAY)) == 1
    assert len(news_items(many, TODAY)) == 10


def test_a_script_or_style_block_is_never_read_as_news():
    html = "<script>var x = '<li>2026-01-01 <h3>Fake headline here</h3></li>'</script>"

    assert news_items(html, TODAY) == []


def test_the_fact_carries_headline_span_and_effective_date():
    [fact] = news_item_facts(
        "<article><time datetime='2026-09-24'></time><h2>Vi åpner nytt kontor</h2></article>", "https://example.no/nyheter", NOW
    )

    assert fact.field_name == "dated_activity"
    assert fact.value == "Vi åpner nytt kontor (2026-09-24)"
    assert fact.evidence_span == "Vi åpner nytt kontor"
    assert fact.effective_date == "2026-09-24"


def test_extract_and_verify_carry_the_effective_date_through():
    html = "<html><body><article><time datetime='2026-09-24'></time><h2>Vi åpner nytt kontor</h2></article></body></html>"
    facts = [f for f in extract(FetchedPage("https://example.com/nyheter", html, NOW, EvidenceState.AVAILABLE), now=lambda: NOW)
             if f.evidence_span == "Vi åpner nytt kontor"]

    confirmed = verify(facts[0], _entity("https://example.com/"))

    assert confirmed.effective_date == "2026-09-24" and confirmed.evidence_span == "Vi åpner nytt kontor"


# --- finding the news page ---------------------------------------------------------


def _links(html):
    return find_news_links(html, "https://www.example.no/", "example.no")


def test_finds_the_news_page_by_path_or_by_link_text_on_the_same_site_only():
    html = (
        '<a href="/om-oss">Om oss</a><a href="/nyheter">Nyheter</a><a href="/media">Aktuelt</a>'
        '<a href="https://other.com/news">News</a>'
    )

    assert _links(html) == ["https://www.example.no/nyheter", "https://www.example.no/media"]


def test_a_single_article_is_not_the_news_page_and_the_limit_holds():
    html = '<a href="/nyheter/2026/en-historie">Les mer</a>' + "".join(f'<a href="/blogg{i}">Blogg</a>' for i in range(5))

    assert _links(html) == ["https://www.example.no/blogg0", "https://www.example.no/blogg1"]


def _client(routes):
    return httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, text=routes[str(r.url)]) if str(r.url) in routes else httpx.Response(404)
    ))


def test_crawl_follows_the_news_link_only_when_asked():
    routes = {"https://example.com/": '<a href="/nyheter">Nyheter</a>', "https://example.com/nyheter": "<html>x</html>"}

    off = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes))
    on = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes), follow_news=True)

    assert [p.url for p in off] == ["https://example.com/"]
    assert [p.url for p in on] == ["https://example.com/", "https://example.com/nyheter"]
