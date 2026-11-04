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
    assert parse_date("2026-12-01", TODAY) is None  # two months ahead: an event teaser, not news
    assert parse_date("2026-10-20", TODAY) == "2026-10-20"  # soon: allowed
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
        "<article><time datetime='2026-09-24'></time><h2><a href='/nyheter/nytt-kontor'>Vi åpner nytt kontor</a></h2></article>", "https://example.no/nyheter", NOW
    )

    assert fact.field_name == "dated_activity"
    assert fact.value == "Vi åpner nytt kontor (2026-09-24)"
    assert fact.evidence_span == "Vi åpner nytt kontor"
    assert fact.effective_date == "2026-09-24"


def test_extract_and_verify_carry_the_effective_date_through():
    html = "<html><body><article><time datetime='2026-09-24'></time><h2><a href='/nyheter/kontor'>Vi åpner nytt kontor</a></h2></article></body></html>"
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


def test_section_labels_and_date_strings_are_not_headlines():
    """Found on 100 notable companies: 'Press room', 'Share and analyst information',
    '2026-10-12 &ndash; 2026-10-14' were published as news items."""
    html = (
        "<ul><li><time datetime='2026-09-29'></time><h3>Press room</h3></li>"
        "<li><time datetime='2025-10-15'></time><h3>Share and analyst information</h3></li>"
        "<li><span>2026-09-23</span><h3>2026-10-12 &ndash; 2026-10-14</h3></li>"
        "<li><time datetime='2026-09-20'></time><h3>Automated IC programming: preventing errors</h3></li></ul>"
    )

    assert news_items(html, TODAY) == [("Automated IC programming: preventing errors", "2026-09-20")]


def test_an_item_dated_the_day_of_the_crawl_is_not_news():
    """A careers page 'dated' today (a page-level timestamp) was published as news."""
    html = "<ul><li><time datetime='2026-10-03'></time><h3>Jobb og karriere i Consto</h3></li></ul>"

    assert news_items(html, TODAY) == []


def test_json_ld_article_dates_lose_their_time_of_day_and_become_the_effective_date():
    from src.pipeline.extract import structured_facts

    html = ('<script type="application/ld+json">{"@type":"NewsArticle","headline":"SvarUT er nå tilgjengelig",'
            '"datePublished":"2023-04-13T15:12:07.000Z"}</script>')

    fact = next(f for f in structured_facts(html, "https://dips.com/n", NOW) if f.field_name == "dated_activity")

    assert fact.value == "SvarUT er nå tilgjengelig (2023-04-13)"
    assert fact.effective_date == "2023-04-13" and fact.evidence_span == "SvarUT er nå tilgjengelig"


# --- more layouts and sources ----------------------------------------------------------------


def test_news_laid_out_as_a_grid_of_plain_card_divs_is_read():
    html = (
        '<div class="news-card"><span>12. mai 2026</span><h3>Ny avtale med Equinor</h3></div>'
        '<div class="post-item"><span>01.04.2026</span><h3>Kvartalsrapport er klar</h3></div>'
        '<div class="footer-links"><span>2026-01-01</span><h3>Personvern og vilkår</h3></div>'
    )

    assert news_items(html, TODAY) == [("Ny avtale med Equinor", "2026-05-12"), ("Kvartalsrapport er klar", "2026-04-01")]


def test_a_time_tag_with_no_wrapper_is_paired_with_the_headline_next_to_it():
    html = "<div><time datetime='2026-09-20'>20. sep</time><a href='/n/1'>Vi åpner nytt kontor i Bergen</a></div>"

    assert news_items(html, TODAY) == [("Vi åpner nytt kontor i Bergen", "2026-09-20")]


def test_a_wordpress_site_is_recognised_and_its_post_list_address_is_built():
    from src.pipeline.news import is_wordpress, wp_posts_url

    assert is_wordpress('<link rel="stylesheet" href="/wp-content/themes/x/style.css">')
    assert is_wordpress('<meta name="generator" content="WordPress 6.5">')
    assert not is_wordpress("<html><body>plain</body></html>")
    assert wp_posts_url("https://www.example.no/") == "https://www.example.no/wp-json/wp/v2/posts?per_page=6&_fields=date,title,link"


def test_wordpress_posts_become_dated_headlines_quoted_as_the_json_writes_them():
    from src.pipeline.news import wp_post_facts

    body = (
        '[{"date":"2026-09-22T10:00:00","title":{"rendered":"Nytt kontor &#8211; Bergen"},"link":"https://x.no/a"},'
        '{"date":"2026-10-03T09:00:00","title":{"rendered":"Skrevet i dag"},"link":"https://x.no/b"},'
        '{"date":"2026-08-01T09:00:00","title":{"rendered":"Press room"},"link":"https://x.no/c"}]'
    )

    facts = wp_post_facts(body, "https://x.no/wp-json/wp/v2/posts", NOW)

    assert [f.value for f in facts] == ["Nytt kontor – Bergen (2026-09-22)"]  # today's item and a section label are dropped
    assert facts[0].evidence_span == "Nytt kontor &#8211; Bergen" and facts[0].evidence_span in body
    assert facts[0].effective_date == "2026-09-22"
    assert wp_post_facts("not json", "u", NOW) == [] and wp_post_facts('{"a": 1}', "u", NOW) == []


def test_the_crawl_asks_a_wordpress_homepage_for_its_post_list_and_the_extractor_reads_it():
    home = '<html><head><link rel="stylesheet" href="/wp-content/x.css"></head><body>hi</body></html>'
    posts = '[{"date":"2026-09-22T10:00:00","title":{"rendered":"Nytt kontor i Bergen"},"link":"https://example.com/a"}]'
    routes = {"https://example.com/": home, "https://example.com/wp-json/wp/v2/posts?per_page=6&_fields=date,title,link": posts}

    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client(routes), follow_news=True)
    facts = [f for p in pages for f in extract(p, now=lambda: NOW) if f.field_name == "dated_activity"]

    assert any(p.url.endswith("/wp-json/wp/v2/posts?per_page=6&_fields=date,title,link") for p in pages)
    assert [f.value for f in facts] == ["Nytt kontor i Bergen (2026-09-22)"]
    assert verify(facts[0], _entity("https://example.com/")) is not None


def test_a_site_that_is_not_wordpress_is_never_asked_for_a_post_list():
    pages = crawl(_entity("https://example.com/"), BudgetGovernor(), client=_client({"https://example.com/": "<html>x</html>"}), follow_news=True)

    assert [p.url for p in pages] == ["https://example.com/"]


# --- only dated posts with their own page (official audit, 23 Oct) -------------------------


def test_an_item_counts_only_when_it_links_to_its_own_post_page():
    """Not "Kontakt oss", not an undated or unlinked line, not a link to another site."""
    html = (
        "<ul><li><time datetime='2026-10-01'></time><h3><a href='/nyheter/ny-avtale'>Ny avtale med Equinor</a></h3></li>"
        "<li><time datetime='2026-10-02'></time><h3><a href='/kontakt-oss'>Kontakt oss</a></h3></li>"
        "<li><time datetime='2026-10-03'></time><h3>Et innlegg uten lenke</h3></li>"
        "<li><time datetime='2026-10-04'></time><h3><a href='https://annen.no/sak'>En sak hos noen andre</a></h3></li></ul>"
    )

    assert news_items(html, TODAY, "https://example.no/nyheter") == [("Ny avtale med Equinor", "2026-10-01")]


def test_a_card_whose_link_sits_outside_its_dated_part_still_counts():
    """salmongroup.no/nyheter/ puts the post link in the card's heading and the date in a
    separate "post-date" div: the headline's own link elsewhere on the page proves the post."""
    html = (
        "<div class='card'><h3><a href='/nytt-styre-i-salmon-group/'>Nytt styre i Salmon Group</a></h3></div>"
        "<div class='post-date'>9. juni 2024</div><span><h3>Nytt styre i Salmon Group</h3></span>"
    )

    assert ("Nytt styre i Salmon Group", "2024-06-09") in news_items(html, TODAY, "https://salmongroup.no/nyheter/")


def test_template_posts_section_labels_and_one_word_titles_are_not_news():
    from src.pipeline.news import is_news_title

    for title in ("Hello world!", "Hei, verden!", "Welcome to Flatsome", "Kontakt oss", "Personvern", "Historie", "Bendiksen"):
        assert not is_news_title(title), title
    for title in ("Strategisamling i K8", "Lederkonferansen 2026", "Ny avtale med Equinor"):
        assert is_news_title(title), title


def test_an_about_heading_and_a_job_ads_dates_are_not_news():
    """1,000-company check, 3 Nov: "Om AF Anlegg (2026-11-08)" came from a job ad on
    afgruppen.no, dated by its application deadline."""
    from src.pipeline.news import is_news_title

    for title in ("Om AF Anlegg", "Om oss", "About the company"):
        assert not is_news_title(title), title
    assert is_news_title("Omsetningen økte kraftig i 2025")

    html = (
        "<html><body><article><h2>Om AF Anlegg</h2><time datetime='2026-11-08'>8. november 2026</time>"
        "<a href='/karriere/ledige-stillinger/2026/09/sommerjobb/'>Les mer</a></article></body></html>"
    )
    page = FetchedPage(
        url="https://www.afgruppen.no/karriere/ledige-stillinger/2026/09/sommerjobb/", raw_html=html,
        fetched_at=datetime(2026, 11, 3, tzinfo=timezone.utc), fetch_state=EvidenceState.AVAILABLE,
    )

    assert [f for f in extract(page) if f.field_name == "dated_activity"] == []
