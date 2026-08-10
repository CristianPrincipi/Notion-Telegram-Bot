"""A half-written page is reported as a half-written page.

THE BUG THIS LOCKS DOWN
-----------------------
Notion caps an append at 100 blocks, so anything longer is several requests —
and Notion has no transactions across them. Batch 3 of 5 failing leaves batches
1 and 2 ON THE PAGE, permanently, with no way to roll them back.

Every caller reported that as a flat failure:

  add_Quote              returned a bare False → "❌ Error during quote
                         transcription", while two fifths of the quote sat in
                         the book.
  create_page            returned the new page's id together with the append
                         error, and `if not page_id` cannot see the difference →
                         "✅ Saved to Notion!" for a page holding its first 100
                         blocks and nothing else.
  apply_section_updates  filed the section under `skipped`, which the reply
                         prints as "these are unchanged" — true when the FIRST
                         batch failed, false when a later one did, and the reply
                         cannot tell which.

Each of those makes the same wrong thing happen next: you re-run, and the part
that already landed is appended a second time.

WHAT IS ASSERTED. The MESSAGE, at every call site, not just the return value. A
`Written` that carries an accurate tally nobody prints is the same bug with more
bookkeeping — and the tally is only worth carrying because of the sentence it
lets each caller write.
"""

import pytest
import responses

from clients import notion_client
from clients.notion_client import Written, append_children, create_page
from conftest import NOTION_BASE, FakeUpdate, run, with_update, written_half, written_ok
from services import books, implement, learn

BLOCK_ID     = "block-under-test"
CHILDREN_URL = f"{NOTION_BASE}/blocks/{BLOCK_ID}/children"
PAGES_URL    = f"{NOTION_BASE}/pages"


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Retry backoff sleeps 1s/2s/4s — skip it so the suite stays fast."""
    monkeypatch.setattr(notion_client.time, "sleep", lambda seconds: None)


def block(index):
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": f"line {index}"}}]}}


def created(count):
    return {"results": [{"id": f"new-{i}"} for i in range(count)]}


# ─── 1. THE TALLY ──────────────────────────────────────────────────────────────

@responses.activate
def test_a_failed_third_batch_reports_the_two_that_landed():
    responses.add(responses.PATCH, CHILDREN_URL, status=200, json=created(100))
    responses.add(responses.PATCH, CHILDREN_URL, status=200, json=created(100))
    responses.add(responses.PATCH, CHILDREN_URL, status=502, body="bad gateway")

    written, err = append_children(BLOCK_ID, [block(i) for i in range(430)])

    assert "502" in err
    assert written.batches_done  == 2
    assert written.batches_total == 5
    assert written.blocks_total  == 430
    assert written.partial, "two batches are on the page and this says otherwise"
    assert "2 of 5" in written.summary


@responses.activate
def test_a_failure_on_the_first_batch_is_not_partial():
    """Nothing landed, so "unchanged" is the true report for this one.

    The distinction is the whole feature: `partial` has to be FALSE here or every
    ordinary failure starts telling you to go and check Notion by hand.
    """
    responses.add(responses.PATCH, CHILDREN_URL, status=400, body="bad block")

    written, err = append_children(BLOCK_ID, [block(i) for i in range(150)])

    assert err and not written.partial
    assert written.batches_done == 0


@responses.activate
def test_a_complete_write_is_never_partial():
    for _ in range(3):
        responses.add(responses.PATCH, CHILDREN_URL, status=200, json=created(100))

    written, err = append_children(BLOCK_ID, [block(i) for i in range(250)])

    assert err is None
    assert not written.partial
    assert written.batches_done == written.batches_total == 3


def test_written_is_still_a_list():
    """Every existing caller reads this value as the blocks that were created.

    implement_diet takes `created[0]["id"]` off it, append_children itself takes
    `[-1]`, and half a dozen call sites unpack it and throw it away. A NamedTuple
    would have broken each of those at a different place; a list subclass breaks
    none of them.
    """
    written = written_ok(3)

    assert isinstance(written, list)
    assert len(written) == 3
    assert written[0]["id"] == "new-0"
    assert not Written(), "an empty append must stay falsy"


# ─── 2. QUOTES ─────────────────────────────────────────────────────────────────
# The user-facing case: a long quote is one heading plus one block per 1800
# characters, so a book-length passage is genuinely several batches.

@responses.activate
def test_add_quote_reports_what_reached_the_page():
    responses.add(responses.PATCH, CHILDREN_URL, status=200, json=created(100))
    responses.add(responses.PATCH, CHILDREN_URL, status=502, body="bad gateway")

    written, err = books.add_Quote(BLOCK_ID, "On Fear", "x" * (1800 * 150))

    assert err and written.partial
    assert written.batches_done == 1


@pytest.fixture
def book_found(monkeypatch):
    monkeypatch.setattr(books, "find_Book_Page", lambda name: "book-page-1")


def quote_it(monkeypatch, result):
    monkeypatch.setattr(books, "add_Quote", lambda page_id, title, text: result)
    update = FakeUpdate(text="Add q Dune - On Fear - Fear is the mind-killer")
    run(books.run_add_quote("Dune", "On Fear", "Fear is the mind-killer",
                            **with_update(update)))
    return update


def test_a_partly_saved_quote_says_so_and_says_how_much(book_found, monkeypatch):
    update = quote_it(monkeypatch, (written_half(2, 5, 430), "Notion 502: bad gateway"))

    assert update.message.replied_with("partly")
    assert update.message.replied_with("2 of 5")


def test_a_partly_saved_quote_warns_that_re_running_duplicates(book_found, monkeypatch):
    """The advice is the point.

    Told "it failed", you re-send the command — and the part that landed is
    appended a second time, which is a worse page than either outcome on its own.
    """
    update = quote_it(monkeypatch, (written_half(2, 5, 430), "Notion 502: bad gateway"))

    assert update.message.replied_with("SECOND copy")


def test_a_quote_that_wrote_nothing_still_reads_as_a_plain_failure(book_found, monkeypatch):
    """The mirror: nothing landed, so nothing needs checking in Notion."""
    from conftest import written_nothing

    update = quote_it(monkeypatch, (written_nothing(), "Notion 400: bad block"))

    assert update.message.replied_with("Error during quote transcription")
    assert not update.message.replied_with("partly")


def test_a_fully_saved_quote_is_unchanged(book_found, monkeypatch):
    update = quote_it(monkeypatch, (written_ok(2), None))

    assert update.message.replied_with("Quote added")
    assert not update.message.replied_with("partly")


# ─── 3. LEARN PAGES ────────────────────────────────────────────────────────────

@responses.activate
def test_create_page_says_the_page_exists_and_is_incomplete():
    responses.add(responses.POST, PAGES_URL, status=200, json={"id": "page-1"})
    responses.add(responses.PATCH, f"{NOTION_BASE}/blocks/page-1/children",
                  status=502, body="bad gateway")

    page_id, err = create_page("db-1", {}, children=[block(i) for i in range(150)])

    assert page_id == "page-1", "the page exists — reporting no id would lose it"
    assert err, "a page missing 50 of its blocks came back as a clean success"
    assert "100 of 150" in err


def test_create_learn_page_returns_the_incomplete_state_separately(monkeypatch):
    """`ok` stays True: the page IS there, and a caller that deletes-and-retries
    on False would be told to throw away a page holding most of its content."""
    monkeypatch.setattr(learn, "create_page",
                        lambda db, props, children=None, icon=None: (
                            "page-1", "page created with 100 of 150 blocks"))

    ok, result, incomplete = learn.create_learn_page("article", "T", [])

    assert (ok, result) == (True, "page-1")
    assert "100 of 150" in incomplete


@pytest.fixture
def learn_stubs(monkeypatch):
    state = {"create": (True, "page-1", None)}
    monkeypatch.setattr(learn, "extract_article",
                        lambda url: ({"title": "T", "author": "", "text": "body"}, None))
    monkeypatch.setattr(learn, "summarize_with_claude",
                        lambda ctype, text, title="", source="": (
                            {"title": "T", "tldr": "gist"}, None))
    monkeypatch.setattr(learn, "database_property_type", lambda db, prop: ("url", None))
    monkeypatch.setattr(learn, "find_page_by_source_url",
                        lambda db, prop_type, url: (None, None))
    monkeypatch.setattr(learn, "create_learn_page", lambda *a, **k: state["create"])
    return state


def learn_it():
    text = "Learn article https://example.com/post"
    update = FakeUpdate(text=text)
    run(learn.run_learn(text, **with_update(update)))
    return update


def test_a_partly_written_learn_page_is_not_reported_as_saved(learn_stubs):
    learn_stubs["create"] = (True, "page-1", "page created with 100 of 150 blocks")

    update = learn_it()

    assert update.message.replied_with("Saved partially")
    assert not update.message.replied_with("✅ Saved to Notion!")
    assert update.message.replied_with("missing content")


def test_a_partly_written_learn_page_warns_against_re_running(learn_stubs):
    learn_stubs["create"] = (True, "page-1", "page created with 100 of 150 blocks")

    update = learn_it()

    assert update.message.replied_with("SECOND page")


def test_a_complete_learn_page_still_reports_a_clean_save(learn_stubs):
    """The mirror. A partial warning that fires on every save is not a warning."""
    update = learn_it()

    assert update.message.replied_with("Saved to Notion")
    assert not update.message.replied_with("Saved partially")


# ─── 4. MANUAL SECTIONS ────────────────────────────────────────────────────────

def heading(block_id, text):
    return {"id": block_id, "type": "heading_2",
            "heading_2": {"rich_text": [{"plain_text": text}]}}


def leaf(block_id, text):
    return {"id": block_id, "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": [{"plain_text": text}]}}


SECTIONS_PAGE = [heading("h-1", "⚙️ Perfect Process"),
                 leaf("old-1", "Prime the material")]


@pytest.fixture
def manual(monkeypatch):
    state = {"append": (written_half(2, 5, 430), "Notion 502: bad gateway"),
             "deletes": []}
    monkeypatch.setattr(implement, "get_children", lambda page_id: (SECTIONS_PAGE, None))
    monkeypatch.setattr(implement, "append_children",
                        lambda block_id, blocks, after=None: state["append"])
    monkeypatch.setattr(implement, "delete_block", state["deletes"].append)
    return state


def apply_it():
    sections, _ = implement.read_manual_sections("manual-1")
    return implement.apply_section_updates(
        "manual-1", [{"path": "Perfect Process", "lines": ["merged"]}], sections)


def test_a_half_written_section_is_not_reported_as_unchanged(manual):
    """THE ONE THAT WAS WRONG.

    The old content is still there — the delete never ran, per Hard Rule 2 — and
    part of the replacement is now sitting next to it. Both copies are on the
    page. "Unchanged" is the one thing that section is not.
    """
    applied, skipped, partial = apply_it()

    assert applied == 0
    assert skipped == [], "a half-written section was filed as unchanged"
    assert len(partial) == 1
    assert "2 of 5" in partial[0]


def test_a_section_whose_first_batch_failed_is_genuinely_unchanged(manual):
    """The mirror, and the reason the two buckets are separate.

    Nothing was appended and nothing was deleted, so this section really is
    byte-identical — folding it in with the half-written one would send you to
    Notion to check a page that does not need checking.
    """
    from conftest import written_nothing

    manual["append"] = (written_nothing(), "Notion 400: bad block")

    applied, skipped, partial = apply_it()

    assert (applied, partial) == (0, [])
    assert len(skipped) == 1


def test_a_half_written_section_still_deletes_nothing(manual):
    """Hard Rule 2 holds regardless: the old content is the only complete copy."""
    apply_it()

    assert manual["deletes"] == []
