"""Learn does not summarise the same URL twice.

THE BUG THIS LOCKS DOWN
-----------------------
Learn had no memory of what it had already saved. Sending the same link again —
which is what you do after a timeout, a 502, or a Railway restart mid-run — made
a SECOND Notion page and paid for a second summarisation. Neither was reported:
both runs end in "✅ Saved to Notion!", and the only way to find out is to open
Notion and see two pages with almost the same title.

"Almost the same title" is the part that keeps costing after the fact.
`search_page_in_db` matches with a `contains` filter, so when Implement goes
looking for that source it has two candidates and picks by created_time. The
duplicate does not sit quietly next to the original; it competes with it.

WHAT IS ASSERTED, and why each one is not covered by the others:

  the normalisation table    the comparison itself. A dedup that only catches
                             byte-identical URLs catches nothing real — the link
                             from a newsletter and the link off the site are
                             never byte-identical.
  the short circuit          that the check happens BEFORE the fetch and before
                             Claude. A duplicate detected after summarising has
                             already cost the money it exists to save.
  the reply                  that it says WHEN, and how to override. A refusal
                             with no way past it is a worse command.
  the force token            that ` !` overrides, and that a bang inside a title
                             does not.
  the degradations           that a missing property and a failed query are told
                             apart, are both said out loud, and neither one
                             blocks the command.
"""

import pytest

from clients import notion_client
from conftest import FakeUpdate, run, with_update
from services import learn

LEARN_DB = "test-learn-id"          # LEARN_ID in the fake environment


# ─── 1. THE COMPARISON ─────────────────────────────────────────────────────────
# Each row is a pair that must reduce to the same string, with the reason it
# arrives differently in the first place. These are not hypotheticals: every one
# is a way the same article reaches Telegram twice.

SAME_SOURCE = [
    ("a link from an old post",
     "http://example.com/post", "https://example.com/post"),
    ("a host typed by hand",
     "https://WWW.Example.COM/post", "https://example.com/post"),
    ("a port spelled out by a tool",
     "https://example.com:443/post", "https://example.com/post"),
    ("a trailing slash",
     "https://example.com/post/", "https://example.com/post"),
    ("a link to one of its headings",
     "https://example.com/post#conclusion", "https://example.com/post"),
    ("a newsletter's campaign tags",
     "https://example.com/post?utm_source=news&utm_medium=email",
     "https://example.com/post"),
    ("a Facebook click id",
     "https://example.com/post?fbclid=IwAR123", "https://example.com/post"),
    ("a YouTube share id",
     "https://youtu.be/abc?si=xyz123", "https://youtu.be/abc"),
    ("the same parameters in the other order",
     "https://youtube.com/watch?list=PL1&v=abc",
     "https://youtube.com/watch?v=abc&list=PL1"),
    ("tracking mixed in with a real parameter",
     "https://youtube.com/watch?v=abc&utm_campaign=x&si=y",
     "https://youtube.com/watch?v=abc"),
]


@pytest.mark.parametrize("why, left, right", SAME_SOURCE,
                         ids=[why for why, _, _ in SAME_SOURCE])
def test_two_spellings_of_one_source_normalise_together(why, left, right):
    assert learn.normalise_source_url(left) == learn.normalise_source_url(right)
    assert learn.normalise_source_url(left), "normalised to nothing at all"


DIFFERENT_SOURCES = [
    ("two videos", "https://youtube.com/watch?v=abc", "https://youtube.com/watch?v=def"),
    ("two posts",  "https://example.com/one",         "https://example.com/two"),
    ("two hosts",  "https://example.com/post",        "https://example.org/post"),
    # `t` is a timestamp into a video and `page` is a real route. Neither is in
    # the tracking list, and this is the assertion that keeps them out of it: an
    # over-eager stripper collapses genuinely different sources into one.
    ("a paginated article", "https://example.com/post?page=2", "https://example.com/post?page=3"),
]


@pytest.mark.parametrize("why, left, right", DIFFERENT_SOURCES,
                         ids=[why for why, _, _ in DIFFERENT_SOURCES])
def test_two_different_sources_stay_different(why, left, right):
    assert learn.normalise_source_url(left) != learn.normalise_source_url(right)


def test_a_book_title_has_no_url_identity():
    """`Learn book Atomic Habits` has nothing to compare, and must not pretend to.

    "" is the signal that switches the whole check off for that run — a book
    title matched as if it were a URL would either never match or match
    everything.
    """
    assert learn.normalise_source_url("Atomic Habits") == ""
    assert learn.normalise_source_url("") == ""
    assert learn.normalise_source_url("ftp://example.com/file") == ""


# ─── 2. THE SHORT CIRCUIT ──────────────────────────────────────────────────────

def existing_page(created_time="2026-08-07T09:00:00.000Z", title="Deep Work"):
    return {
        "id": "learn-page-1",
        "created_time": created_time,
        "properties": {"Name": {"type": "title",
                                "title": [{"plain_text": title}]}},
    }


@pytest.fixture
def learn_calls(monkeypatch):
    """Every expensive step of Learn, recorded rather than performed.

    The fetch and the summariser are what the check exists to avoid, so they are
    recorded by NAME: asserting they are absent is the whole test.
    """
    calls = {"queries": []}

    def find_page_by_source_url(db_id, prop_type, normalised):
        calls["queries"].append({"db_id": db_id, "prop_type": prop_type,
                                 "url": normalised})
        return calls.get("existing"), calls.get("query_error")

    def create_learn_page(content_type, title, blocks, metadata=None):
        calls["created"] = {"content_type": content_type, "title": title,
                            "metadata": metadata or {}}
        return True, "new-page-1", None

    def extract_article(url):
        calls["fetched"] = url
        return {"title": "T", "author": "", "text": "body"}, None

    def summarize_with_claude(content_type, text, title="", source=""):
        calls["summarised"] = text
        return {"title": "T", "tldr": "gist"}, None

    monkeypatch.setattr(learn, "database_property_type",
                        lambda db_id, prop: (calls.get("prop_type", "url"),
                                             calls.get("schema_error")))
    monkeypatch.setattr(learn, "find_page_by_source_url", find_page_by_source_url)
    monkeypatch.setattr(learn, "extract_article", extract_article)
    monkeypatch.setattr(learn, "summarize_with_claude", summarize_with_claude)
    monkeypatch.setattr(learn, "create_learn_page", create_learn_page)
    return calls


def learn_it(text="Learn article https://example.com/post?utm_source=news"):
    update = FakeUpdate(text=text)
    run(learn.run_learn(text, **with_update(update)))
    return update


def test_a_url_already_saved_is_not_fetched_and_not_summarised(learn_calls):
    """THE POINT OF THE WHOLE MILESTONE.

    Detecting the duplicate after the summarisation would be a tidier page count
    and the same invoice. Both of these absences are the feature.
    """
    learn_calls["existing"] = existing_page()

    update = learn_it()

    assert "fetched" not in learn_calls, "fetched a page it had already saved"
    assert "summarised" not in learn_calls, "paid Claude for a duplicate"
    assert "created" not in learn_calls, "wrote a second page"
    assert update.message.replied_with("Already saved")


def test_the_duplicate_reply_says_when_it_was_saved(learn_calls, monkeypatch):
    """A date is what makes the reply actionable.

    "You already have this" leaves you no way to tell this morning's failed run
    from something you saved last spring, and those want opposite next steps.
    """
    monkeypatch.setattr(learn, "now_local", lambda: _local(2026, 8, 10))
    learn_calls["existing"] = existing_page(created_time="2026-08-07T09:00:00.000Z")

    update = learn_it()

    assert update.message.replied_with("3 days ago")
    assert update.message.replied_with("07 August 2026")


def test_the_duplicate_reply_names_the_page_and_the_matched_url(learn_calls):
    learn_calls["existing"] = existing_page(title="Deep Work")

    update = learn_it()

    assert update.message.replied_with("Deep Work")
    # The NORMALISED url, not what was typed: it is the thing that matched, and
    # seeing it is how you find out the tracking parameters were stripped.
    assert update.message.replied_with("https://example.com/post")


def test_the_duplicate_reply_offers_a_way_through(learn_calls):
    """A guard with no override is a guard you route around by deleting the page."""
    learn_calls["existing"] = existing_page()

    update = learn_it()

    assert update.message.replied_with("!")
    assert update.message.replied_with("Learn article")


def test_a_trailing_bang_re_summarises_anyway(learn_calls):
    learn_calls["existing"] = existing_page()

    update = learn_it("Learn article https://example.com/post !")

    assert "summarised" in learn_calls, "the force token did not force anything"
    assert learn_calls["created"]["content_type"] == "article"
    assert update.message.replied_with("re-summarising")


def test_the_force_token_does_not_eat_the_end_of_a_title(learn_calls):
    """`Learn book Wow!` is a book called "Wow!", not a forced run.

    The same lesson `Remind`'s `t` lookahead carries: a token that can swallow
    the end of a real value will, on the one input that ends that way. Here the
    cost of getting it wrong is a book filed under the wrong name.
    """
    learn_it("Learn book Wow!")

    assert learn_calls["created"]["title"] == "T"
    # What reached the summariser is what matters: the source text is built from
    # the title, so a stripped "!" shows up there.
    assert "Wow!" in learn_calls["summarised"]


# ─── 3. THE WRITE SIDE ─────────────────────────────────────────────────────────

def test_the_normalised_url_is_stored_on_the_new_page(learn_calls):
    """Nothing to compare against tomorrow unless today's run writes the key.

    The NORMALISED form is stored, so the match is an `equals` filter rather than
    a scan. The original stays in the page's 🔗 Source link — see
    build_notion_blocks — so nothing clickable is lost to this.
    """
    learn_it("Learn article https://www.example.com/post/?utm_source=news")

    assert learn_calls["created"]["metadata"]["source_url"] == "https://example.com/post"
    assert learn_calls["created"]["metadata"]["source_property_type"] == "url"


def test_a_rich_text_property_is_written_and_matched_as_rich_text(learn_calls):
    """Either column type is a reasonable thing to have made by hand.

    A filter that names the wrong one is a Notion 400, and a 400 on this path
    arrives as "no duplicate found" — the same shape of lie the hardcoded "Name"
    in search_page_in_db used to produce.
    """
    learn_calls["prop_type"] = "rich_text"

    learn_it()

    assert learn_calls["queries"][0]["prop_type"] == "rich_text"
    assert learn_calls["created"]["metadata"]["source_property_type"] == "rich_text"


def test_the_filter_matches_the_property_type():
    assert learn._source_url_filter("url", "https://x.com") == {
        "property": "Source URL", "url": {"equals": "https://x.com"}}
    assert learn._source_url_filter("rich_text", "https://x.com") == {
        "property": "Source URL", "rich_text": {"equals": "https://x.com"}}


# ─── 4. DEGRADING, OUT LOUD ────────────────────────────────────────────────────
# Both of these used to be impossible states because the feature did not exist.
# Now they are the two ways it can be unavailable, and they need opposite fixes:
# one is "add a column", the other is "wait for Notion".

def test_a_missing_property_disables_the_check_and_says_so(learn_calls):
    learn_calls["prop_type"] = None

    update = learn_it()

    assert update.message.replied_with("Source URL")
    assert "summarised" in learn_calls, "refused to Learn over a missing column"


def test_a_missing_property_is_not_written_either(learn_calls):
    """The write is what would BREAK, not just degrade.

    Notion answers a 400 for a property it does not know, and that 400 is on the
    page CREATE — so writing the key regardless would turn "you have not added
    the column yet" into "Learn does not work any more".
    """
    learn_calls["prop_type"] = None

    learn_it()

    assert learn_calls["created"]["metadata"]["source_property_type"] == ""


def sent_properties(monkeypatch, source_type):
    """The properties dict create_learn_page hands to Notion."""
    sent = {}
    monkeypatch.setattr(learn, "create_page",
                        lambda db, props, children=None, icon=None: (
                            sent.update(props) or ("page-1", None)))
    learn.create_learn_page("article", "T", [], metadata={
        "source_url": "https://example.com/post", "source_property_type": source_type})
    return sent


def test_create_learn_page_writes_nothing_when_it_has_no_property_type(monkeypatch):
    """The other half of the guard above, at the call that actually sends it.

    run_learn passing "" is one line; this is the line that has to honour it, and
    the test above cannot see it because it stubs create_learn_page out. A
    property Notion has never heard of is a 400 on the page CREATE — the write
    fails entirely, rather than the dedup quietly not working.
    """
    assert "Source URL" not in sent_properties(monkeypatch, "")


def test_create_learn_page_writes_the_property_in_its_own_type(monkeypatch):
    assert sent_properties(monkeypatch, "url")["Source URL"] == {
        "url": "https://example.com/post"}

    as_rich_text = sent_properties(monkeypatch, "rich_text")["Source URL"]
    assert as_rich_text["rich_text"][0]["text"]["content"] == "https://example.com/post"


def test_a_failed_check_is_reported_and_not_treated_as_no_duplicate(learn_calls):
    """An error is never the same value as an empty result.

    The check cannot rule the duplicate out, so it says the duplicate is possible
    rather than reporting the silence as a clean bill of health.
    """
    learn_calls["query_error"] = "Notion 502: bad gateway"

    update = learn_it()

    assert update.message.replied_with("Could not check for duplicates")
    assert update.message.replied_with("502")
    assert "summarised" in learn_calls, "refused to Learn over a failed check"


def test_a_failed_query_still_stores_the_key_for_next_time(learn_calls):
    """The SCHEMA read succeeded; only the query failed.

    Dropping the property here would let one bad minute at Notion cost the next
    run its match as well, which is the failure quietly compounding.
    """
    learn_calls["query_error"] = "Notion 502: bad gateway"

    learn_it()

    assert learn_calls["created"]["metadata"]["source_property_type"] == "url"


def test_an_unreadable_schema_disables_both_halves(learn_calls):
    learn_calls["schema_error"] = "Notion 401: unauthorized"

    update = learn_it()

    assert update.message.replied_with("Could not check for duplicates")
    assert learn_calls["created"]["metadata"]["source_property_type"] == ""


def test_a_book_never_reaches_the_duplicate_check(learn_calls):
    """No URL, no identity, no query — rather than a query on a weaker key."""
    learn_it("Learn book Sapiens")

    assert learn_calls["queries"] == []
    assert "summarised" in learn_calls


# ─── 5. THE SCHEMA LOOKUP ITSELF ───────────────────────────────────────────────


@pytest.fixture(autouse=True)
def forget_database_schemas():
    """Empty the schema cache around every test in this file.

    Same reasoning as test_notion_client's fixture for _title_props: the cache is
    module-level and lives for the whole process, so whichever test ran first
    would answer every later test's lookup, and the "it is fetched" assertions
    would pass without anything being fetched.
    """
    notion_client._db_schemas.clear()
    yield
    notion_client._db_schemas.clear()


def test_a_property_that_exists_reports_its_type(monkeypatch):
    monkeypatch.setattr(notion_client, "get_database", lambda db_id: (
        {"properties": {"Name": {"type": "title"},
                        "Source URL": {"type": "url"}}}, None))

    assert notion_client.database_property_type(LEARN_DB, "Source URL") == ("url", None)


def test_a_property_that_is_absent_is_not_an_error(monkeypatch):
    """(None, None) and (None, error) are different answers.

    Collapsing them is the bug this whole codebase keeps re-learning: "you have
    not made the column" and "Notion is down" want opposite handling, and
    `if not prop_type` cannot tell them apart.
    """
    monkeypatch.setattr(notion_client, "get_database", lambda db_id: (
        {"properties": {"Name": {"type": "title"}}}, None))

    assert notion_client.database_property_type(LEARN_DB, "Source URL") == (None, None)


def test_an_unreadable_schema_is_an_error_not_an_absence(monkeypatch):
    monkeypatch.setattr(notion_client, "get_database",
                        lambda db_id: (None, "Notion 502: bad gateway"))

    prop_type, err = notion_client.database_property_type(LEARN_DB, "Source URL")

    assert prop_type is None
    assert "502" in err


def test_the_schema_is_read_once_per_database(monkeypatch):
    reads = []
    monkeypatch.setattr(notion_client, "get_database", lambda db_id: (
        reads.append(db_id) or ({"properties": {"Source URL": {"type": "url"}}}, None)))

    for _ in range(3):
        notion_client.database_property_type(LEARN_DB, "Source URL")

    assert reads == [LEARN_DB], "asked Notion for the schema on every lookup"


def test_a_failed_schema_read_is_not_cached(monkeypatch):
    """Caching a failure would make one bad minute permanent for the process.

    The cache is populated only on success and read before any network call, so a
    database read once keeps working through a later outage and one that has
    never been read successfully retries.
    """
    answers = [(None, "Notion 502: bad gateway"),
               ({"properties": {"Source URL": {"type": "url"}}}, None)]
    monkeypatch.setattr(notion_client, "get_database", lambda db_id: answers.pop(0))

    assert notion_client.database_property_type(LEARN_DB, "Source URL")[1]
    assert notion_client.database_property_type(LEARN_DB, "Source URL") == ("url", None)


def _local(year, month, day):
    """A tz-aware "now" in the project's timezone, for a stubbed clock."""
    from clients.calendar_client import TIMEZONE
    from datetime import datetime

    return TIMEZONE.localize(datetime(year, month, day, 12, 0))
