"""Article extraction: what reaches the summariser, and what you are told about it.

Learn's output feeds Implement, which feeds the Manual, so a defect here does not
stay here — it arrives months later as a thin Manual entry with nothing pointing
back at extraction. All four bugs asserted below were silent in exactly that way:

  * the body was cut at 12,000 characters while the summariser accepted 100,000,
  * a <title> with a nested tag raised AttributeError into a broad except and was
    reported as the website's fault,
  * every internal bug was reported as the website's fault for the same reason,
  * BS4's get_text() handed the model the nav, the cookie banner and the footer.

Offline: `responses` intercepts the fetch, so no test here touches a real site.
"""

import logging

import pytest
import responses

from config import SUMMARY_INPUT_CHARS
from conftest import FakeUpdate, run, with_update
from services import learn

URL = "https://example.com/post"


def html_page(title="A Title", body="Body text.", head_extra="", body_extra=""):
    """A page with the chrome a real article carries around it."""
    return f"""<html><head><title>{title}</title>{head_extra}</head>
<body>
  <nav>Home About Contact Subscribe Newsletter</nav>
  <div id="cookie-banner">We use cookies. Accept all cookies to continue browsing.</div>
  <article><h1>{title}</h1>{body}</article>
  <footer>Copyright 2026. Terms of service. Privacy policy.</footer>
  {body_extra}
</body></html>"""


def paragraphs(text, count=1):
    """`count` paragraphs of `text`, each made UNIQUE by an index.

    The uniqueness is not decoration. trafilatura deduplicates identical blocks —
    twenty copies of one paragraph come back as one — so a fixture built by
    repeating a single string measures the deduplicator instead of the thing
    under test, and a "no truncation" assertion fails for a reason that has
    nothing to do with truncation. Real articles do not repeat themselves; a
    fixture standing in for one should not either.
    """
    return "".join(f"<p>Paragraph {i}. {text}</p>" for i in range(count))


def serve(html, status=200):
    responses.add(responses.GET, URL, status=status, body=html,
                  content_type="text/html")


# ─── 1. THE NESTED <title> CRASH ───────────────────────────────────────────────

@responses.activate
def test_a_title_with_a_nested_tag_does_not_crash_extraction():
    """`soup.title.string` is None whenever <title> has more than one child.

    `<title>Post <em>name</em></title>` is ordinary markup, and `.string.strip()`
    raised AttributeError on it. The broad except turned that into
    "Could not extract content: 'NoneType' object has no attribute 'strip'" —
    which reads as a problem with the site and sends the investigation there.
    """
    serve(html_page(title="Post <em>name</em>",
                    body=paragraphs("Substantive article text about the topic.", 6)))

    result, err = learn.extract_article(URL)

    assert err is None, f"extraction failed instead of reading the title: {err}"
    assert result["title"] == "Post name", (
        f"nested tags were not flattened — got {result['title']!r}")


@responses.activate
def test_the_bs4_fallback_alone_survives_a_nested_title():
    """The fix has to be in the BS4 path itself, not masked by trafilatura.

    Without this, `_extract_bs4` could still hold the `.string` bug and every
    test above it would stay green purely because trafilatura answered first —
    right up to the first page trafilatura declines, which is when the fallback
    is the only parser left and the one that must not crash.
    """
    html = html_page(title="Post <em>name</em>", body=paragraphs("Body.", 2))

    result = learn._extract_bs4(html.encode(), URL)

    assert result["title"] == "Post name"


@pytest.mark.parametrize("title_markup", [
    "Post <em>name</em>",          # nested tag
    "Post <em>name</em> ",         # nested tag, trailing space
    "Post\n  <em>name</em>",       # laid out over two lines
])
def test_a_nested_title_flattens_the_same_way_on_either_parser(title_markup):
    """The SAME page reaches this code in two different shapes.

    A parser that treats <title> as normal content gives a tag with children, so
    `.string` is None and the old code raised. A parser that treats it as RCDATA
    — which is what the HTML5 spec asks for — gives ONE string still containing
    "<em>name</em>", where nothing raises and the raw markup simply becomes the
    Notion page's name.

    CPython's html.parser changed from the first to the second WITHIN 3.12 patch
    releases: this suite was green on 3.12.3 and red on 3.12.13 for exactly this
    reason. Asserting on the outcome rather than on either shape is what keeps
    that from deciding whether the test passes.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(f"<html><head><title>{title_markup}</title></head></html>",
                         "html.parser")

    assert learn._title_from_html(soup) == "Post name"


def test_the_rcdata_title_shape_is_covered_on_every_machine():
    """The test above only exercises whatever shape THIS parser produces.

    Which means it proves one of the two cases on any given machine and quietly
    leaves the other one unguarded — the precise way this bug reached CI green
    from a laptop. Building the RCDATA shape by hand covers it regardless of the
    CPython patch release the runner happens to have.
    """
    from bs4 import BeautifulSoup, NavigableString

    soup = BeautifulSoup("<html><head><title>x</title></head></html>", "html.parser")
    soup.title.string.replace_with(NavigableString("Post <em>name</em>"))
    assert soup.title.contents == ["Post <em>name</em>"], "not the RCDATA shape"

    assert learn._title_from_html(soup) == "Post name"


def test_a_title_may_still_contain_a_less_than_sign():
    """The flattening must not eat punctuation. "3 < 5" is not a tag."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<html><head><title>Why 3 < 5 matters</title></head></html>",
                         "html.parser")

    assert learn._title_from_html(soup) == "Why 3 < 5 matters"


@responses.activate
def test_an_empty_title_falls_back_to_the_url():
    """A present-but-empty <title> must not become an empty page name in Notion."""
    serve(html_page(title="", body=paragraphs("Article body text here.", 6)))

    result, err = learn.extract_article(URL)

    assert err is None
    assert result["title"] == URL, (
        f"empty title should fall back to the URL, got {result['title']!r}")


# ─── 2. NO SILENT TRUNCATION ───────────────────────────────────────────────────

@responses.activate
def test_extraction_no_longer_cuts_the_body_at_12000_characters():
    """The old cap. `text[:12000]` against a summariser that accepts 100,000.

    An article of 40k characters was summarised from its first eighth, and
    nothing in the reply or on the Notion page said so.
    """
    sentence = "This sentence is part of a long and substantive article body. "
    long_body = paragraphs(sentence * 40, count=20)   # comfortably over 12k chars
    serve(html_page(body=long_body))

    result, err = learn.extract_article(URL)

    assert err is None
    assert len(result["text"]) > 12_000, (
        f"body was cut at the old 12k limit — got {len(result['text'])} chars")


@responses.activate
def test_extraction_does_not_pre_truncate_to_the_summariser_budget_either():
    """Extraction must return the text WHOLE, cap included.

    If extraction capped at exactly SUMMARY_INPUT_CHARS, run_learn could not tell
    a source of exactly the budget from one already cut down to it, and the
    partial-summary warning would be unreliable at its own boundary. The cut
    belongs at the single point that can report it.
    """
    huge = paragraphs("word " * 2_000, count=60)      # > SUMMARY_INPUT_CHARS
    serve(html_page(body=huge))

    result, err = learn.extract_article(URL)

    assert err is None
    assert len(result["text"]) > SUMMARY_INPUT_CHARS, (
        "extraction truncated to the summariser budget, which makes the "
        f"truncation warning undetectable — got {len(result['text'])} chars")


# ─── 3. THE REPLY SAYS WHEN A SUMMARY IS PARTIAL ───────────────────────────────

@pytest.fixture
def summarised(monkeypatch):
    """Drive run_learn with Claude and Notion stubbed, capturing the text sent.

    Returns the list the summariser was called with, so a test can assert on the
    text that actually reached it rather than on what it hoped was sent.
    """
    seen = []

    def fake_summarise(content_type, text, title="", source=""):
        seen.append(text)
        return {"title": "T", "tldr": "A summary.", "sections": []}, None

    monkeypatch.setattr(learn, "summarize_with_claude", fake_summarise)
    monkeypatch.setattr(learn, "create_learn_page",
                        lambda *a, **k: (True, "page-1"))
    return seen


def test_an_oversized_source_is_flagged_as_partial_in_the_reply(monkeypatch, summarised):
    """THE POINT OF THE WHOLE MILESTONE.

    A summary built from a fraction of the source is indistinguishable, in the
    reply and on the Notion page, from one built from all of it. Being told at
    the time is the only thing that separates them.
    """
    oversized = "x" * (SUMMARY_INPUT_CHARS + 5_000)
    monkeypatch.setattr(learn, "extract_article",
                        lambda url: ({"title": "T", "author": "", "text": oversized}, None))

    update = FakeUpdate()
    run(learn.run_learn(f"Learn article {URL}", **with_update(update)))

    assert any("partial" in text.lower() for text in update.message.reply_texts), (
        f"nothing warned that the summary was partial: {update.message.reply_texts}")
    assert len(summarised[0]) == SUMMARY_INPUT_CHARS, (
        f"the summariser was given {len(summarised[0])} chars, not the budget")


def test_a_source_within_budget_is_never_flagged(monkeypatch, summarised):
    """The mirror. A warning that always fires is not a warning."""
    monkeypatch.setattr(learn, "extract_article",
                        lambda url: ({"title": "T", "author": "", "text": "short body"}, None))

    update = FakeUpdate()
    run(learn.run_learn(f"Learn article {URL}", **with_update(update)))

    assert not any("partial" in text.lower() for text in update.message.reply_texts), (
        f"a short source was reported as truncated: {update.message.reply_texts}")
    assert summarised[0] == "short body"


def test_the_truncation_warning_covers_every_content_type(monkeypatch, summarised):
    """A 3-hour transcript and a 400-page PDF hit the same cap as an article.

    The check lives in run_learn rather than in each extractor precisely so that
    adding a content type cannot quietly opt out of it.
    """
    oversized = "y" * (SUMMARY_INPUT_CHARS + 1)
    monkeypatch.setattr(learn, "extract_youtube", lambda url: (oversized, None))
    monkeypatch.setattr(learn, "extract_pdf", lambda b: (oversized, None))

    for command, kwargs in [
        ("Learn video https://youtu.be/abc", {}),
        ("Learn pdf", {"file_bytes": b"%PDF-1.4 fake"}),
    ]:
        update = FakeUpdate()
        run(learn.run_learn(command, **kwargs, **with_update(update)))
        assert any("partial" in t.lower() for t in update.message.reply_texts), (
            f"{command!r} was truncated without saying so: "
            f"{update.message.reply_texts}")


# ─── 4. AN INTERNAL BUG IS NOT REPORTED AS THE SITE'S FAULT ────────────────────

@responses.activate
def test_an_internal_error_is_named_as_ours_not_as_a_fetch_failure(monkeypatch, caplog):
    """The class of bug the nested <title> belonged to.

    Both parsers raising is a bug in this module. Reporting it in the same words
    as a 403 or a DNS failure is what made the AttributeError above take so long
    to find — the message pointed at the website every time.
    """
    serve(html_page(body=paragraphs("Body text.", 6)))
    monkeypatch.setattr(learn, "_extract_trafilatura",
                        lambda html, url: (_ for _ in ()).throw(ValueError("boom")))

    with caplog.at_level(logging.ERROR):
        result, err = learn.extract_article(URL)

    assert result is None
    assert "ValueError" in err, f"the error does not name the exception type: {err}"
    assert "fetch" not in err.lower(), f"an internal bug is dressed as a fetch failure: {err}"
    assert any(rec.exc_info for rec in caplog.records), (
        "no traceback was logged, so the bug is invisible in the Railway log")


@responses.activate
def test_a_real_fetch_failure_is_named_as_one():
    """The other half — a genuine site problem must still read as a site problem."""
    serve("nope", status=503)

    result, err = learn.extract_article(URL)

    assert result is None
    assert "fetch" in err.lower(), f"a 503 was not reported as a fetch problem: {err}"
    assert "Internal extraction error" not in err


# ─── 5. TRAFILATURA PRIMARY, BS4 FALLBACK ──────────────────────────────────────

@responses.activate
def test_boilerplate_does_not_reach_the_summariser():
    """The quality defect. BS4's get_text() cannot tell an article from a footer.

    Every string in the document is equally text to it, so navigation, the cookie
    banner and the footer were all sent to Claude — tokens paid for, and context
    reasoned over on the way to a Manual entry.
    """
    serve(html_page(body=paragraphs(
        "The substantive body of the article, long enough to be scored as the "
        "main content of this page by a density-based extractor. ", 8)))

    result, err = learn.extract_article(URL)

    assert err is None
    assert "substantive body" in result["text"]
    for noise in ("Accept all cookies", "Privacy policy", "Subscribe"):
        assert noise not in result["text"], (
            f"boilerplate {noise!r} reached the summariser")


@responses.activate
def test_bs4_catches_what_trafilatura_declines(monkeypatch):
    """trafilatura is precise by design and returns nothing rather than junk.

    That makes a fallback mandatory, not optional: without one, every page it
    declines becomes "no content could be extracted".
    """
    serve(html_page(title="Fallback Title", body=paragraphs("Body text here.", 3)))
    monkeypatch.setattr(learn, "_extract_trafilatura", lambda html, url: None)

    result, err = learn.extract_article(URL)

    assert err is None
    assert result["title"] == "Fallback Title"
    assert "Body text here." in result["text"]


@responses.activate
def test_a_short_trafilatura_result_is_treated_as_a_miss(monkeypatch):
    """Its failure mode on an odd layout is a FRAGMENT, not an empty result.

    A caption or a standfirst comes back looking like a successful extraction, so
    `is None` alone cannot catch it — and a page summarised from its own caption
    produces a confident, wrong Manual entry.
    """
    serve(html_page(title="Real Title", body=paragraphs("The full body text.", 5)))
    monkeypatch.setattr(learn, "trafilatura", type("T", (), {
        "extract": staticmethod(lambda html, url=None: "A caption."),
        "extract_metadata": staticmethod(lambda html, default_url=None: None),
    }))

    result, err = learn.extract_article(URL)

    assert err is None
    assert result["text"] != "A caption.", "a fragment was accepted as the article"
    assert "The full body text." in result["text"]


@responses.activate
def test_a_missing_metadata_title_falls_back_to_the_page_title_not_the_url(monkeypatch):
    """The title falls back on its own, independently of the body.

    trafilatura can score the article correctly and still return no title, and
    the page's own <title> is a far better Notion page name than a raw URL. Going
    straight to the URL would name pages after their address for no reason.
    """
    serve(html_page(title="The Real Headline",
                    body=paragraphs("A long substantive article body. ", 8)))
    real_extract = learn.trafilatura.extract
    monkeypatch.setattr(learn, "trafilatura", type("T", (), {
        "extract": staticmethod(real_extract),
        "extract_metadata": staticmethod(lambda html, default_url=None: None),
    }))

    result, err = learn.extract_article(URL)

    assert err is None
    assert result["title"] == "The Real Headline", (
        f"fell past the page's own <title> — got {result['title']!r}")


@responses.activate
def test_the_author_is_populated_from_metadata():
    """`author` was hardcoded to "" and the Notion Author column went unfilled."""
    serve(html_page(head_extra='<meta name="author" content="Jane Doe">',
                    body=paragraphs("A long substantive article body. ", 8)))

    result, err = learn.extract_article(URL)

    assert err is None
    assert result["author"] == "Jane Doe"


@pytest.mark.parametrize("meta_author, expected", [
    # Kept — these are bylines.
    ("Jane Doe",                                              "Jane Doe"),
    ("Jane Doe; John Smith",                                  "Jane Doe; John Smith"),
    ("  Jane Doe  ",                                          "Jane Doe"),
    ("J. R. R. Tolkien",                                      "J. R. R. Tolkien"),
    ("Anne-Marie Slaughter",                                  "Anne-Marie Slaughter"),
    ("Gabriel García Márquez",                                "Gabriel García Márquez"),
    # Lowercase particles are part of a real name and must not count against it.
    ("Ludwig van Beethoven",                                  "Ludwig van Beethoven"),
    ("Charles de Gaulle",                                     "Charles de Gaulle"),
    ("NASA",                                                  "NASA"),

    # Dropped. BOTH of these came out of the same Wikipedia authority-control
    # footer box on two different articles, and the pair is the whole reason the
    # size bounds are not enough on their own: 27 characters over 3 words is
    # exactly the shape of a real name, so only the lowercase words give it away.
    ("Authority control databases International GND National "
     "United States Japan Israel",                            ""),   # /wiki/Espresso
    ("Authority control databases",                           ""),   # /wiki/World_War_II
    ("Skip to main content",                                  ""),
    ("Posted in technology and culture",                      ""),
    ("word " * 40,                                            ""),
    ("x" * 200,                                               ""),
    (None,                                                    ""),
])
@responses.activate
def test_an_implausible_author_is_dropped_rather_than_written(meta_author, expected):
    """Notion is the source of truth, so a fabricated Author is worse than none.

    An empty Author is visibly empty. A wrong one is a fact you later act on —
    and this field was hardcoded "" until the metadata was wired up, so anything
    junk arriving here is a regression introduced by that wiring, not a
    pre-existing gap.
    """
    serve(html_page(body=paragraphs("A long substantive article body. ", 8)))
    real_extract = learn.trafilatura.extract
    monkeypatch_meta = type("Meta", (), {"title": "T", "author": meta_author})
    learn_traf = type("T", (), {
        "extract": staticmethod(real_extract),
        "extract_metadata": staticmethod(lambda html, default_url=None: monkeypatch_meta),
    })
    original = learn.trafilatura
    learn.trafilatura = learn_traf
    try:
        result, err = learn.extract_article(URL)
    finally:
        learn.trafilatura = original

    assert err is None
    assert result["author"] == expected


@responses.activate
def test_which_parser_won_is_logged(caplog):
    """A permanent silent downgrade to the fallback is the thing to notice.

    If trafilatura started declining every page — a bad upgrade, a changed
    default — the only symptom would be summaries slowly getting worse. The log
    line is what makes that visible instead of inferred.
    """
    serve(html_page(body=paragraphs("A long substantive article body here. ", 8)))

    with caplog.at_level(logging.INFO, logger="services.learn"):
        learn.extract_article(URL)

    assert any("trafilatura" in rec.message for rec in caplog.records), (
        "nothing recorded which parser produced the text")
