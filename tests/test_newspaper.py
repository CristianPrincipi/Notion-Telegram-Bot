"""The daily newspaper: feeds → EPUB, offline.

Every fetch goes through an injected `fetcher` (url → bytes), so nothing here
touches the network: FEEDS maps each URL to a hand-built RSS document and PAGES
to a hand-built article page. Article paragraphs are all distinct — trafilatura
drops repeated ones.
"""

import io
import re
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest
import requests
import yaml
from ebooklib import epub
from telegram import Chat, Message, MessageEntity, Update
from telegram import User as TelegramUser
from telegram.ext import ApplicationBuilder

import bot.newspaper
import config
import david
import proactive.scheduler as scheduler
from conftest import OWNER_ID, FakeContext, FakeUpdate, run
from services import (newspaper, newspaper_collect, newspaper_epub, newspaper_fulltext,
                      newspaper_sources, newspaper_topnews)
from services.newspaper_collect import Article
from services.newspaper_sources import SECTIONS, Source

NOW = datetime(2026, 11, 23, 6, 30, tzinfo=timezone.utc)


# ─── FIXTURE BUILDERS ──────────────────────────────────────────────────────────

def rss(items):
    """items: [(title, link, hours_ago or None, categories, summary)]"""
    parts = []
    for title, link, hours, categories, summary in items:
        date = (f"<pubDate>{format_datetime(NOW - timedelta(hours=hours))}</pubDate>"
                if hours is not None else "")
        cats = "".join(f"<category>{c}</category>" for c in categories)
        parts.append(f"<item><title>{title}</title><link>{link}</link>{date}{cats}"
                     f"<description>{summary}</description></item>")
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>'
            f'{"".join(parts)}</channel></rss>').encode()


def page(title, paragraphs):
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (f"<html><head><title>{title}</title></head><body><nav>Menu Home Sport</nav>"
            f"<article><h1>{title}</h1>{body}</article><footer>Copyright</footer>"
            f"</body></html>").encode()


def long_paragraphs(topic, count=4):
    """Distinct sentences, comfortably over NEWSPAPER_MIN_TEXT_CHARS together."""
    return [f"Paragraph {i} about {topic}: " + " ".join(f"{topic}{i}word{j}" for j in range(25)) + "."
            for i in range(count)]


def fetcher_for(responses):
    def fetch(url):
        value = responses.get(url)
        if value is None:
            raise requests.ConnectionError(f"no route to {url}")
        if isinstance(value, Exception):
            raise value
        return value
    return fetch


def article(title, source="S", hours=1, section="Finanza", url=None, summary="", **kw):
    slug = re.sub(r"\W+", "-", title)
    return Article(title=title, url=url or f"https://example.com/{slug}",
                   source=source, published=NOW - timedelta(hours=hours),
                   summary=summary, section=section, **kw)


def write_yaml(tmp_path, data):
    path = tmp_path / "news.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return str(path)


BASE_CONFIG = {
    "title": "Il Giornale di Prova",
    "sources": [
        {"name": "Fixed", "url": "https://fixed.example/rss", "section": "Finanza", "max": 2},
        {"name": "Auto", "url": "https://auto.example/rss", "section": "auto", "max": 5},
        {"name": "Down", "url": "https://down.example/rss", "section": "Scienze", "max": 3},
    ],
    "top_news_feed": "https://top.example/rss",
    "category_map": {"Economia": "Finanza", "Mondo": "Geopolitica"},
    "keywords": {"Politica Italiana": ["governo", "parlamento"], "Ambiente": ["clima"]},
    "cluster_threshold": 75,
    "schedule": "06:30",
    "images": False,
}


# ─── news_sources.yaml ─────────────────────────────────────────────────────────

def test_the_repository_file_loads_and_runs_at_0630():
    cfg, err = newspaper_sources.load_config()
    assert err is None, err
    assert (cfg.hour, cfg.minute) == (6, 30)
    assert {s.section for s in cfg.sources} <= set(SECTIONS) | {"auto"}
    assert all(section in SECTIONS for section in cfg.category_map.values())


def test_every_problem_in_the_file_is_listed_at_once():
    data = dict(BASE_CONFIG, schedule="6.30", images=True, sources=[
        {"name": "A", "url": "ftp://x", "section": "Sport", "max": 0}])
    data["category_map"] = {"Sport": "Calcio"}
    cfg, err = newspaper_sources.parse_config(data)
    assert cfg is None
    for needle in ("url must start", "'Sport' is not one of", "max must be",
                   "'Calcio' is not a section", "schedule must be HH:MM", "images"):
        assert needle in err, f"{needle!r} missing from:\n{err}"


def test_a_missing_file_is_an_error_not_a_crash(tmp_path):
    cfg, err = newspaper_sources.load_config(str(tmp_path / "nope.yaml"))
    assert cfg is None and "not found" in err


def test_a_broken_file_moves_the_job_to_the_default_time(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("sources: [", encoding="utf-8")
    assert newspaper_sources.schedule_time(str(path)) == (
        config.NEWSPAPER_DEFAULT_HOUR, config.NEWSPAPER_DEFAULT_MINUTE)


def test_the_schedule_is_read_from_the_file(tmp_path):
    path = write_yaml(tmp_path, dict(BASE_CONFIG, schedule="07:05"))
    assert newspaper_sources.schedule_time(path) == (7, 5)


# ─── COLLECTION ────────────────────────────────────────────────────────────────

def test_only_the_last_24_hours_are_kept_and_undated_items_are_skipped():
    raw = rss([("Fresh story here", "https://a/1", 2, [], ""),
               ("Old story there", "https://a/2", 30, [], ""),
               ("No date at all", "https://a/3", None, [], "")])
    articles, undated = newspaper_collect.parse_feed(raw, "A")
    assert undated == 1
    assert [a.title for a in newspaper_collect.in_window(articles, NOW)] == ["Fresh story here"]


def test_max_is_applied_after_classification():
    """Capping first would keep the five newest — sport — and then drop them all."""
    items = [(f"Sport result {i}", f"https://auto/s{i}", i, ["Sport"], "") for i in range(5)]
    items += [("Il governo approva la manovra", "https://auto/p", 6, ["Italia"], "")]
    source = Source("Auto", "https://auto/rss", "auto", 5)
    kept, classified = newspaper_collect.collect_source(
        source, NOW, {}, {"Politica Italiana": ("governo",)},
        fetcher=fetcher_for({"https://auto/rss": rss(items)}))
    assert [a.title for a in kept] == ["Il governo approva la manovra"]
    assert kept[0].section == "Politica Italiana"
    assert classified == kept


def test_a_failing_source_is_named_and_the_others_still_count(tmp_path):
    cfg, _ = newspaper_sources.load_config(write_yaml(tmp_path, BASE_CONFIG))
    fetch = fetcher_for({
        "https://fixed.example/rss": rss([("Borsa in rialzo oggi", "https://f/1", 1, [], "")]),
        "https://auto.example/rss": rss([]),
        "https://down.example/rss": requests.HTTPError(
            response=type("R", (), {"status_code": 403})()),
    })
    articles, pool, failed = newspaper_collect.collect(cfg, NOW, fetcher=fetch)
    assert [a.title for a in articles] == ["Borsa in rialzo oggi"]
    assert failed == ["Down (HTTP 403)"]


# ─── CLASSIFICATION ────────────────────────────────────────────────────────────

def test_a_fixed_section_wins_over_everything():
    a = article("Il clima cambia", categories=("Mondo",))
    assert newspaper_collect.classify(a, "Scienze", {"Mondo": "Geopolitica"},
                                      {"Ambiente": ("clima",)}) == "Scienze"


def test_the_category_map_goes_in_its_own_order():
    a = article("Una storia", categories=("Scienza", "Mondo"))
    cmap = {"Mondo": "Geopolitica", "Scienza": "Scienze"}
    assert newspaper_collect.classify(a, "auto", cmap, {}) == "Geopolitica"


def test_keywords_are_whole_words():
    keywords = {"Tecnologia": ("ai",), "Politica Italiana": ("governo",)}
    assert newspaper_collect.classify(article("Non è mai troppo tardi"), "auto", {}, keywords) is None
    assert newspaper_collect.classify(article("Nuovi modelli AI"), "auto", {}, keywords) == "Tecnologia"


def test_keywords_read_the_summary_too_and_the_most_matches_win():
    keywords = {"Politica Italiana": ("governo", "parlamento"), "Ambiente": ("clima",)}
    a = article("Una legge sul clima", summary="Il governo la porta in parlamento")
    assert newspaper_collect.classify(a, "auto", {}, keywords) == "Politica Italiana"


def test_nothing_matching_drops_the_item():
    assert newspaper_collect.classify(article("Risultati di calcio", categories=("Sport",)),
                                      "auto", {"Mondo": "Geopolitica"}, {}) is None


# ─── DE-DUPLICATION ────────────────────────────────────────────────────────────

def test_the_query_string_does_not_make_an_article_two():
    assert (newspaper_collect.normalise_url("https://Site.it/a/b/?utm_source=rss#x")
            == newspaper_collect.normalise_url("https://site.it/a/b"))


def test_a_repeated_url_keeps_the_longer_one():
    short = article("Titolo A", url="https://x.it/a?ref=1", summary="corto")
    longer = article("Titolo diverso", url="https://x.it/a", summary="molto più lungo di così")
    kept, dropped = newspaper_collect.dedupe([short, longer])
    assert kept == [longer] and dropped == 1


def test_near_identical_titles_in_one_section_are_one_article():
    a = article("Borsa: Milano apre in rialzo dello 0,5%", summary="breve")
    b = article("Borsa, Milano apre in rialzo dello 0,5%", summary="un sommario più lungo")
    kept, _ = newspaper_collect.dedupe([a, b])
    assert kept == [b]


def test_the_same_title_in_two_sections_is_kept_twice():
    a = article("Vertice sul clima a Roma", section="Ambiente")
    b = article("Vertice sul clima a Roma", section="Geopolitica", url="https://other/1")
    kept, _ = newspaper_collect.dedupe([a, b])
    assert kept == [a, b]


# ─── ARTICLE BODIES ────────────────────────────────────────────────────────────

def test_sanitize_keeps_only_the_whitelist():
    dirty = ('<div onclick="x()"><p class="lead">Hello <b>bold</b> <i>it</i> '
             '<a href="https://ok.it" target="_blank">ok</a> <a href="javascript:evil()">bad</a>'
             '<script>steal()</script><img src="x.png"></p><h1>Head</h1>'
             '<table><tr><td>cell</td></tr></table><span>loose text</span></div>')
    clean = newspaper_fulltext.sanitize(dirty)
    assert "<script" not in clean and "steal" not in clean
    assert "onclick" not in clean and "class=" not in clean and "target=" not in clean
    assert "javascript" not in clean and "bad" in clean       # the link text stays
    assert '<a href="https://ok.it">ok</a>' in clean
    assert "<strong>bold</strong>" in clean and "<em>it</em>" in clean
    assert "<h2>Head</h2>" in clean
    assert "<img" not in clean and "cell" not in clean
    assert "<p>loose text</p>" in clean
    tags = set(re.findall(r"</?([a-z0-9]+)", clean))
    assert tags <= newspaper_fulltext.ALLOWED_TAGS


def test_a_br_splits_its_paragraph():
    clean = newspaper_fulltext.sanitize("<p>First part.<br/>Second part.<br><br>Third.</p>")
    assert clean.split("\n") == ["<p>First part.</p>", "<p>Second part.</p>", "<p>Third.</p>"]


def test_tidy_drops_the_repeated_title_and_repeated_blocks():
    body = "\n".join(["<h2>Borse in calo</h2>", "<p>Borse in calo - Notizie - Ansa.it</p>",
                      "<p>3' di lettura</p>", "<p>The story itself.</p>", "<p>3' di lettura</p>"])
    assert newspaper_fulltext.tidy(body, "Borse in calo").split("\n") == [
        "<p>3' di lettura</p>", "<p>The story itself.</p>"]


def test_a_full_page_becomes_the_body():
    a = article("Una storia vera", url="https://site/1")
    newspaper_fulltext.fill_body(a, fetcher_for({"https://site/1": page(a.title, long_paragraphs("vero"))}))
    assert a.full_text and a.text_chars >= config.NEWSPAPER_MIN_TEXT_CHARS
    assert "vero0word0" in a.body_html
    assert newspaper_fulltext.FALLBACK_NOTE not in a.body_html


@pytest.mark.parametrize("pages", [
    {"https://site/1": page("Teaser", ["Only a teaser sentence for subscribers."])},
    {"https://site/1": requests.Timeout("slow")},
    {"https://site/1": page("Wall", ["Se hai scelto di non accettare i cookie di profilazione, "
                                     "aderisci all'abbonamento Consentless."] + long_paragraphs("muro"))},
], ids=["too short", "fetch failed", "consent wall"])
def test_a_paywall_a_failure_or_a_wall_falls_back_to_the_summary(pages):
    a = article("Una storia", url="https://site/1", summary="Il sommario del feed <3")
    newspaper_fulltext.fill_body(a, fetcher_for(pages))
    assert not a.full_text
    assert "Il sommario del feed &lt;3" in a.body_html
    assert newspaper_fulltext.FALLBACK_NOTE in a.body_html


# ─── LE 3 NOTIZIE DEL GIORNO ───────────────────────────────────────────────────

def with_body(a, chars=500, full=True):
    a.body_html, a.text_chars, a.full_text = f"<p>Lead of {a.title}.</p>\n<p>More.</p>", chars, full
    return a


def test_normalise_title_drops_punctuation_case_and_stopwords():
    assert newspaper_topnews.normalise_title("L'Ucraina ha attaccato una nave, dice Kiev!") == \
        "ucraina attaccato nave dice kiev"


def test_the_story_two_sources_ran_beats_a_single_one():
    a = with_body(article("Nave militare russa colpita a Bengasi", source="ANSA", hours=3))
    b = with_body(article("Bengasi, colpita una nave militare russa", source="Il Post", hours=4,
                          url="https://ilpost/1"))
    c = with_body(article("Il prezzo del gas sale ancora", source="ANSA", hours=1))
    stories = newspaper_topnews.top_stories([a, b, c], [], [], 75)
    assert stories[0].sources == ["ANSA", "Il Post"] and stories[0].score == 2
    assert stories[1].articles == [c]


def test_the_top_news_bonus_adds_one_and_sources_break_the_tie():
    """ANSA's top-news feed carries ANSA's own items: 1 + 1 must not outrank two
    sources just by being newer."""
    single = with_body(article("Borsa Milano apre in calo", source="ANSA", hours=1))
    pair = [with_body(article("Accordo sul clima fallito alla COP31", source="BBC", hours=5)),
            with_body(article("COP31, fallito l'accordo sul clima", source="Guardian", hours=6,
                              url="https://g/1"))]
    stories = newspaper_topnews.top_stories([single, *pair], [], ["Borsa: Milano apre in calo"], 75)
    assert stories[0].score == stories[1].score == 2
    assert stories[0].sources == ["BBC", "Guardian"]
    assert stories[1].on_top_feed


def test_among_equals_the_newest_wins():
    old = with_body(article("Primo fatto del giorno lontano", hours=9))
    new = with_body(article("Secondo fatto appena successo", hours=1))
    assert newspaper_topnews.top_stories([old, new], [], [], 75)[0].articles == [new]


def test_a_story_counts_sources_cut_by_max_but_leads_with_the_paper():
    in_paper = with_body(article("Incendio in una fabbrica di dolci in Indonesia", source="ANSA"))
    cut = article("Indonesia, incendio in fabbrica di dolci", source="BBC", url="https://bbc/1")
    alone = article("Una notizia che nessun altro ha dato", source="X", url="https://x/1")
    stories = newspaper_topnews.top_stories([in_paper], [in_paper, cut, alone], [], 75)
    assert len(stories) == 1                    # `alone` is not in the paper: never a story
    assert stories[0].main is in_paper and stories[0].others == [cut]


def test_a_one_word_title_never_clusters():
    a = with_body(article("Meloni", source="A"))
    b = with_body(article("Meloni incontra Macron a Parigi", source="B", url="https://b/1"))
    assert len(newspaper_topnews.cluster([a, b], 75)) == 2


# ─── THE EPUB ──────────────────────────────────────────────────────────────────

def edition(stories=True):
    sections = {s: [] for s in SECTIONS}
    sections["Finanza"] = [with_body(article("Borsa & spread <oggi>", source="ANSA"))]
    sections["Geopolitica"] = [with_body(article("Vertice a Bruxelles", source="BBC", section="Geopolitica",
                                                 url="https://bbc/2")),
                               with_body(article("Nuove sanzioni", source="Il Post",
                                                 section="Geopolitica", url="https://p/3"))]
    all_articles = [a for v in sections.values() for a in v]
    top = newspaper_topnews.top_stories(all_articles, [], [], 75) if stories else []
    return newspaper_epub.Edition("Il Giornale di Prova", NOW, sections, top)


def test_the_epub_reads_back_with_every_section_and_article(tmp_path):
    ed = edition()
    path = str(tmp_path / "g.epub")
    newspaper_epub.build_epub(ed, path)
    assert newspaper_epub.check_epub(path, 3) is None

    book = epub.read_epub(path)
    assert book.get_metadata("DC", "language")[0][0] == "it"
    assert book.get_metadata("DC", "creator")[0][0] == "David"
    assert book.get_metadata("DC", "title")[0][0] == "Il Giornale di Prova — lunedì 23 novembre 2026"

    sections = [entry for entry in book.toc if isinstance(entry, tuple)]
    assert [s.title.rsplit(" (", 1)[0] for s, _ in sections] == list(SECTIONS)
    finanza_links = sections[0][1]
    assert [link.title for link in finanza_links] == ["Borsa & spread <oggi>"]
    assert finanza_links[0].href == "sezione-finanza.xhtml#art-1"


def test_titles_are_escaped_and_each_article_links_its_original(tmp_path):
    path = str(tmp_path / "g.epub")
    newspaper_epub.build_epub(edition(), path)
    with zipfile.ZipFile(path) as z:
        finanza = z.read("EPUB/sezione-finanza.xhtml").decode()
        cover = z.read("EPUB/copertina.xhtml").decode()
        top = z.read("EPUB/tre-notizie.xhtml").decode()
    assert "Borsa &amp; spread &lt;oggi&gt;" in finanza
    assert "Leggi l'originale su ANSA" in finanza and 'href="https://example.com/' in finanza
    assert "Lunedì 23 novembre 2026" in cover and "3 articoli" in cover
    assert "Le 3 notizie del giorno" in top
    assert 'href="sezione-' in top                    # points to the full article


def test_an_empty_section_says_so_and_the_check_catches_a_short_book(tmp_path):
    path = str(tmp_path / "g.epub")
    newspaper_epub.build_epub(edition(stories=False), path)
    with zipfile.ZipFile(path) as z:
        assert "Nessun articolo oggi" in z.read("EPUB/sezione-scienze.xhtml").decode()
    assert "holds 3 articles, 4 were laid out" in newspaper_epub.check_epub(path, 4)


def test_the_check_refuses_a_file_that_is_not_an_epub(tmp_path):
    path = tmp_path / "bad.epub"
    path.write_bytes(b"not a zip")
    assert "does not re-open" in newspaper_epub.check_epub(str(path), 0)


def test_the_css_is_for_e_ink():
    css = newspaper_epub.CSS
    assert "serif" in css and "line-height: 1.5" in css and "background: none" in css
    assert "background-color" not in css


# ─── THE WHOLE BUILD ───────────────────────────────────────────────────────────

def full_fetcher(down=False):
    responses = {
        "https://fixed.example/rss": rss([
            ("Borsa di Milano in forte rialzo", "https://fixed/1", 1, [], "Sommario uno"),
            ("Lo spread scende ancora", "https://fixed/2", 2, [], "Sommario due"),
            ("Terza notizia oltre il max", "https://fixed/3", 3, [], "Sommario tre")]),
        "https://auto.example/rss": rss([
            ("Borsa di Milano in forte rialzo oggi", "https://auto/1?utm=x", 1, ["Economia"], "Altro"),
            ("Il governo presenta la manovra", "https://auto/2", 2, ["Italia"], "In parlamento"),
            ("Risultati della Serie A", "https://auto/3", 2, ["Sport"], "Calcio")]),
        "https://down.example/rss": requests.ConnectionError("refused"),
        "https://top.example/rss": rss([("Il governo presenta la manovra", "https://top/1", 1, [], "")]),
        "https://fixed/1": page("Borsa", long_paragraphs("borsa")),
        "https://fixed/2": page("Spread", ["Solo un teaser."]),
        "https://auto/1?utm=x": page("Borsa oggi", long_paragraphs("milano")),
        "https://auto/2": page("Manovra", long_paragraphs("manovra")),
    }
    if down:
        responses = {}
    return fetcher_for(responses)


def test_build_newspaper_end_to_end(tmp_path):
    path = write_yaml(tmp_path, BASE_CONFIG)
    result, err = newspaper.build_newspaper(NOW, fetcher=full_fetcher(), config_path=path)
    assert err is None, err
    assert result.filename == "Giornale_2026-11-23.epub"
    # Fixed's third item is over its max; Auto's Borsa is the same story as
    # Fixed's in the same section (title ≥ 90), so one of the two is dropped.
    assert result.counts == {"Finanza": 2, "Politica Italiana": 1, "Tecnologia": 0,
                             "Geopolitica": 0, "Scienze": 0, "Ambiente": 0}
    assert result.failed == ["Down (unreachable)"]
    assert result.summaries == 1

    epub_path = tmp_path / result.filename
    epub_path.write_bytes(result.epub)
    assert newspaper_epub.check_epub(str(epub_path), 3) is None
    with zipfile.ZipFile(io.BytesIO(result.epub)) as z:
        top = z.read("EPUB/tre-notizie.xhtml").decode()
    # The dropped duplicate still counts as a second source: the Borsa story leads.
    assert top.index("Borsa di Milano") < top.index("Il governo presenta la manovra")


def test_the_caption_names_the_date_the_counts_and_the_failures(tmp_path):
    path = write_yaml(tmp_path, BASE_CONFIG)
    result, _ = newspaper.build_newspaper(NOW, fetcher=full_fetcher(), config_path=path)
    caption = result.caption
    assert "lunedì 23 novembre 2026" in caption and "3 articoli" in caption
    assert "• Finanza: 2" in caption and "• Ambiente: 0" in caption
    assert "Fonti non raggiunte: Down (unreachable)" in caption
    assert len(caption) <= newspaper.CAPTION_LIMIT


def test_every_source_down_is_an_error_not_an_empty_paper(tmp_path):
    path = write_yaml(tmp_path, BASE_CONFIG)
    result, err = newspaper.build_newspaper(NOW, fetcher=full_fetcher(down=True), config_path=path)
    assert result is None and "no source could be read" in err


def test_a_broken_file_is_an_error(tmp_path):
    path = write_yaml(tmp_path, dict(BASE_CONFIG, cluster_threshold=500))
    result, err = newspaper.build_newspaper(NOW, fetcher=full_fetcher(), config_path=path)
    assert result is None and "cluster_threshold" in err


def test_no_temporary_file_is_left_behind(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    path = write_yaml(tmp_path, BASE_CONFIG)
    newspaper.build_newspaper(NOW, fetcher=full_fetcher(), config_path=path)
    assert not list(tmp_path.glob("giornale-*"))


# ─── TELEGRAM ──────────────────────────────────────────────────────────────────

FAKE_RESULT = newspaper.Result(epub=b"EPUB", filename="Giornale_2026-11-23.epub", when=NOW,
                               counts={s: 1 for s in SECTIONS})


def test_giornale_says_it_is_working_then_sends_the_file(monkeypatch):
    monkeypatch.setattr(bot.newspaper, "build_newspaper", lambda now: (FAKE_RESULT, None))
    update = FakeUpdate(text="Giornale")
    run(bot.newspaper.handle_giornale(update, FakeContext()))
    assert update.message.reply_texts == [bot.newspaper.PREPARING]
    sent = update.message.documents[0]
    assert sent["document"] == b"EPUB" and sent["filename"] == "Giornale_2026-11-23.epub"
    assert "6 articoli" in sent["caption"]


def test_giornale_reports_a_failed_build_and_sends_nothing(monkeypatch):
    monkeypatch.setattr(bot.newspaper, "build_newspaper", lambda now: (None, "no source could be read"))
    update = FakeUpdate(text="Giornale")
    run(bot.newspaper.handle_giornale(update, FakeContext()))
    assert update.message.documents == []
    assert update.message.replied_with("no source could be read")


@pytest.fixture
def newspaper_job():
    application = ApplicationBuilder().token("123456:test-token").build()
    scheduler.register_all(application, "-100123")
    job = next(j for j in application.job_queue.jobs() if j.name == "newspaper")
    context = FakeContext()
    context.job = type("Job", (), {"chat_id": "-100123"})()
    return job, context


def test_the_morning_job_sends_the_document_to_the_chat(newspaper_job, monkeypatch):
    job, context = newspaper_job
    monkeypatch.setattr(scheduler, "build_newspaper", lambda now: (FAKE_RESULT, None))
    run(job.callback(context))
    assert context.bot.documents[0]["chat_id"] == "-100123"
    assert context.bot.documents[0]["filename"] == "Giornale_2026-11-23.epub"
    assert context.bot.sent == []


def test_the_morning_job_reports_a_failed_build(newspaper_job, monkeypatch):
    job, context = newspaper_job
    monkeypatch.setattr(scheduler, "build_newspaper", lambda now: (None, "news_sources.yaml: broken"))
    run(job.callback(context))
    assert context.bot.documents == []
    assert "news_sources.yaml: broken" in context.bot.sent[0][1]


def _slash_update(user_id, text):
    message = Message(message_id=1, date=NOW, chat=Chat(id=user_id, type=Chat.PRIVATE),
                      from_user=TelegramUser(id=user_id, is_bot=False, first_name="T"),
                      text=text, entities=[MessageEntity(MessageEntity.BOT_COMMAND, 0,
                                                         len(text.split()[0]))])
    return Update(update_id=1, message=message)


@pytest.fixture
def handlers():
    application = ApplicationBuilder().token("123456:test-token").build()
    david.register_handlers(application, OWNER_ID)
    return [h for group in application.handlers.values() for h in group]


def _reached(handlers, update):
    return [h.callback.__name__ for h in handlers if h.check_update(update)]


def test_slash_giornale_reaches_the_dispatcher(handlers):
    assert _reached(handlers, _slash_update(OWNER_ID, "/giornale")) == ["handle_message"]


def test_no_other_slash_command_does(handlers):
    assert _reached(handlers, _slash_update(OWNER_ID, "/start")) == []
    assert _reached(handlers, _slash_update(OWNER_ID, "/giornale_x")) == []


def test_a_stranger_sending_slash_giornale_reaches_only_the_logger(handlers):
    assert _reached(handlers, _slash_update(999999999, "/giornale")) == ["handle_unauthorized"]


# ─── REVIEW OF #80 ─────────────────────────────────────────────────────────────

def test_a_list_with_newlines_between_items_survives_tidy():
    """tidy() cut the body on every newline, so a <ul> whose <li>s trafilatura
    wrote on separate lines became fragments — and a dropped repeat left an
    unmatched tag behind."""
    body = newspaper_fulltext.sanitize("<p>Intro.</p><ul>\n<li>uno</li>\n<li>due</li>\n</ul>"
                                       "<blockquote>\n<p>citato</p>\n</blockquote>")
    tidied = newspaper_fulltext.tidy(body + "\n<p>Intro.</p>", "Titolo")
    for block in tidied.split("\n"):
        opened = re.findall(r"<([a-z0-9]+)>", block)
        closed = re.findall(r"</([a-z0-9]+)>", block)
        assert sorted(opened) == sorted(closed), f"unbalanced block: {block!r}"
    assert "<li>uno</li>" in tidied and "<li>due</li>" in tidied and "citato" in tidied


def test_one_url_under_two_feed_names_is_one_source():
    """Two ANSA desks syndicating the same story are one report, not two."""
    same = [with_body(article("Il governo approva la legge di bilancio", source="ANSA Politica",
                              url="https://ansa.it/x/1")),
            article("Il governo approva la legge di bilancio", source="ANSA Economia",
                    url="https://ansa.it/x/1?ref=eco")]
    two = [with_body(article("Accordo sul clima fallito alla COP31", source="BBC", hours=5,
                             url="https://bbc/c")),
           article("COP31, fallito l'accordo sul clima", source="Guardian", hours=6, url="https://g/c")]
    stories = newspaper_topnews.top_stories([same[0], two[0]], [*same, *two], [], 75)
    by_lead = {s.main.source: s for s in stories}
    assert by_lead["ANSA Politica"].score == 1
    assert by_lead["BBC"].score == 2
    assert stories[0].main.source == "BBC"


@pytest.mark.parametrize("link", ["javascript:alert(1)", "data:text/html,hi", "/relative/path"])
def test_a_feed_link_that_is_not_http_never_reaches_the_paper(link):
    raw = rss([("A trap", link, 1, [], ""), ("A real one", "https://ok.it/1", 1, [], "")])
    articles, _ = newspaper_collect.parse_feed(raw, "Feed")
    assert [a.url for a in articles] == ["https://ok.it/1"]
