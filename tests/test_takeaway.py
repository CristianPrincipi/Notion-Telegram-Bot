"""Takeaway of the week — one bullet from one Learn page, resurfaced.

THE TWO THINGS THIS FILE IS REALLY ABOUT, both of which are about telling apart
things that look identical from outside:

  1. A page with no takeaways and a page that could not be READ. Skipping both
     means a Notion outage — where every read fails — exhausts the attempts and
     returns (None, None): silence, meaning "quiet week". That is the collapse
     the rest of proactive/ spent a milestone removing, and it would arrive here
     for free if the read error were treated as "no takeaways".
  2. The heading string. The writer is services/learn.py and the reader is
     proactive/takeaway.py, and a heading reworded in one of them does not fail —
     it silently stops finding anything, which is a weekly job that goes quiet.
     `test_the_writer_and_the_reader_agree_on_the_heading` builds a page with the
     REAL writer and reads it with the REAL reader, so neither can drift.

Randomness is injected, never seeded: `choose` is a parameter, so every test here
picks an exact page rather than hoping.
"""

import pytest

from config import TAKEAWAY_MAX_ATTEMPTS, TAKEAWAYS_HEADING
import proactive.takeaway as takeaway

FIRST = lambda seq: seq[0]        # noqa: E731 — the deterministic chooser


def heading(text, level=2):
    btype = f"heading_{level}"
    return {"type": btype, btype: {"rich_text": [{"plain_text": text}]}}


def bullet(text):
    return {"type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"plain_text": text}]}}


def paragraph(text):
    return {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": text}]}}


def page(page_id, title):
    return {"id": page_id,
            "properties": {"Name": {"type": "title",
                                    "title": [{"plain_text": title}]}}}


def as_notion_returns_it(blocks):
    """The writer's blocks in the shape a READ hands them back.

    A REAL asymmetry, not test scaffolding, and worth knowing before writing any
    other reader/writer pair: `notion_client.rich()` emits
    `{"text": {"content": …}}` on the way OUT, and Notion's response carries
    `{"plain_text": …}` on the way BACK — which is the field `extract_rich_text`
    reads. A cross-check that skipped this step would be handing the reader a
    shape production never gives it, and the honest fix is to model the round
    trip here rather than to widen the reader to accept both.
    """
    converted = []
    for block in blocks:
        btype = block.get("type", "")
        body = dict(block.get(btype, {}))
        if "rich_text" in body:
            body["rich_text"] = [
                {**rt, "plain_text": rt.get("text", {}).get("content", "")}
                for rt in body["rich_text"]
            ]
        converted.append({**block, btype: body})
    return converted


TAKEAWAY_PAGE = [
    heading("📖 Summary"),
    paragraph("Some prose about the book."),
    heading(TAKEAWAYS_HEADING),
    bullet("Schedule deep work in blocks."),
    bullet("Shallow work expands to fill the day."),
]


@pytest.fixture
def notion(monkeypatch):
    """A Learn database of pages, and the blocks each one holds."""
    state = {
        "pages": [page("p-1", "Deep Work")],
        "blocks": {"p-1": TAKEAWAY_PAGE},
        "query_error": None,
        "read_errors": {},          # page id → error string
        "opened": [],               # which pages were read, in order
    }

    def query_database(db_id, filter_obj=None, sorts=None, page_size=100):
        if state["query_error"]:
            return [], state["query_error"]
        return list(state["pages"]), None

    def get_children(page_id):
        state["opened"].append(page_id)
        if page_id in state["read_errors"]:
            return [], state["read_errors"][page_id]
        return list(state["blocks"].get(page_id, [])), None

    monkeypatch.setattr(takeaway, "query_database", query_database)
    monkeypatch.setattr(takeaway, "get_children", get_children)
    return state


# ─── THE MESSAGE ───────────────────────────────────────────────────────────────

def test_it_sends_a_takeaway_and_names_the_page(notion):
    """The title is what makes it traceable — a bullet with no source is a
    fortune cookie."""
    text, err = takeaway.build_takeaway(choose=FIRST)

    assert err is None
    assert "Schedule deep work in blocks." in text
    assert "Deep Work" in text


def test_the_bullets_stop_at_the_next_heading():
    """Not "everything after the heading". On a David-written page the takeaways
    are last, so the two rules agree today — which is exactly why the strict one
    has to be asserted now, before something starts depending on the accident.

    ASSERTED ON THE COLLECTED LIST, NOT ON THE MESSAGE, and that is the point of
    the test rather than a detail of it. Written the other way first — build the
    whole message and check the stray line is absent — it stayed GREEN with the
    boundary removed: the deterministic chooser takes the first bullet, so a
    wrongly-collected extra one at the END is invisible from the message. The
    assertion has to sit on the thing the guard produces.
    """
    blocks = TAKEAWAY_PAGE + [
        heading("📝 My own notes"),
        bullet("A note I wrote myself, which is not a takeaway."),
    ]

    assert takeaway.takeaways_in(blocks) == [
        "Schedule deep work in blocks.",
        "Shallow work expands to fill the day.",
    ]


def test_a_bullet_before_the_heading_is_not_a_takeaway():
    """The section is bounded at BOTH ends. Same reasoning as above: asserted on
    the list, so it cannot pass because of which bullet happened to be picked."""
    blocks = [bullet("An early bullet.")] + TAKEAWAY_PAGE

    assert "An early bullet." not in takeaway.takeaways_in(blocks)


def test_the_stray_bullet_does_not_reach_the_message_either(notion):
    """The end-to-end half, with a chooser that takes the LAST bullet — which is
    where a collection bug lands."""
    notion["blocks"]["p-1"] = TAKEAWAY_PAGE + [
        heading("📝 My own notes"),
        bullet("A note I wrote myself, which is not a takeaway."),
    ]

    text, _ = takeaway.build_takeaway(choose=lambda seq: seq[-1])

    assert "A note I wrote myself" not in text
    assert "Shallow work expands to fill the day." in text


# ─── CHOOSING ──────────────────────────────────────────────────────────────────

def test_a_page_with_no_takeaways_is_skipped_not_reported(notion):
    """Older Learn pages, and every `Learn book` page whose summary came back
    without any, have no takeaways section. That is normal, not an error."""
    notion["pages"] = [page("p-empty", "No Takeaways Here"), page("p-1", "Deep Work")]
    notion["blocks"]["p-empty"] = [heading("📖 Summary"), paragraph("Prose only.")]

    text, err = takeaway.build_takeaway(choose=FIRST)

    assert err is None, "a page without takeaways is not a failure"
    assert "Deep Work" in text


def test_the_same_page_is_never_opened_twice(notion):
    """Sampling is without replacement, so the attempt bound counts distinct
    PAGES. With replacement, a three-page database could spend every attempt on
    the same one — and the bound would stop meaning what config.py says."""
    notion["pages"] = [page(f"p-{i}", f"Book {i}") for i in range(3)]
    for i in range(3):
        notion["blocks"][f"p-{i}"] = [paragraph("no takeaways")]

    takeaway.build_takeaway(choose=FIRST)

    assert notion["opened"] == sorted(set(notion["opened"])), (
        f"a page was opened twice: {notion['opened']}")


def test_a_database_with_no_takeaways_anywhere_gives_up_quietly(notion):
    """It must TERMINATE, and it must terminate silently: nothing failed, there
    is simply nothing to resurface."""
    notion["pages"] = [page(f"p-{i}", f"Book {i}") for i in range(50)]
    for i in range(50):
        notion["blocks"][f"p-{i}"] = [paragraph("no takeaways")]

    assert takeaway.build_takeaway(choose=FIRST) == (None, None)


def test_it_stops_at_the_attempt_bound(notion):
    """The bound is what keeps a database of hundreds of takeaway-less pages from
    being opened one at a time on a worker thread."""
    notion["pages"] = [page(f"p-{i}", f"Book {i}") for i in range(50)]
    for i in range(50):
        notion["blocks"][f"p-{i}"] = [paragraph("no takeaways")]

    takeaway.build_takeaway(choose=FIRST)

    assert len(notion["opened"]) == TAKEAWAY_MAX_ATTEMPTS


def test_an_empty_learn_database_is_silent(notion):
    notion["pages"] = []

    assert takeaway.build_takeaway(choose=FIRST) == (None, None)


def test_the_chooser_decides_which_page(notion):
    """The whole reason it is a parameter: a test asserts an exact page rather
    than a seeded algorithm's current output."""
    notion["pages"] = [page("p-1", "Deep Work"), page("p-2", "Atomic Habits")]
    notion["blocks"]["p-2"] = [heading(TAKEAWAYS_HEADING), bullet("Habits stack.")]

    text, _ = takeaway.build_takeaway(choose=lambda seq: seq[-1])

    assert "Atomic Habits" in text
    assert "Habits stack." in text


# ─── A FAILED READ IS NOT A QUIET WEEK ─────────────────────────────────────────
# The trap this milestone sits over. Skipping a failed read like a page with no
# takeaways means an outage — where EVERY read fails — returns (None, None).


def test_a_page_that_cannot_be_read_is_reported_not_skipped(notion):
    """No takeaway found AND something failed → (None, error). Silence here would
    read as "quiet week" for as long as Notion was unhappy."""
    notion["read_errors"]["p-1"] = "Notion 503: service unavailable"

    text, err = takeaway.build_takeaway(choose=FIRST)

    assert text is None
    assert "503" in err
    assert "Deep Work" in err, "the report does not say which page failed"


def test_a_takeaway_is_still_sent_when_another_page_failed(notion):
    """(text, error) — both. The scheduler sends the text AND reports the error;
    one broken page must not cost the week's takeaway, and must not be hidden by
    it either."""
    notion["pages"] = [page("p-broken", "Unreadable"), page("p-1", "Deep Work")]
    notion["read_errors"]["p-broken"] = "Notion 404: not found"

    text, err = takeaway.build_takeaway(choose=FIRST)

    assert "Schedule deep work in blocks." in text
    assert "404" in err


def test_a_query_failure_reports(notion):
    notion["query_error"] = "Notion 401: API token is invalid"

    text, err = takeaway.build_takeaway(choose=FIRST)

    assert text is None
    assert "401" in err


def test_no_learn_database_is_reported(notion, monkeypatch):
    monkeypatch.setattr(takeaway, "LEARN_ID", None)

    text, err = takeaway.build_takeaway(choose=FIRST)

    assert text is None
    assert "LEARN_ID" in err


# ─── PROVENANCE TRAVELS WITH THE BULLET ────────────────────────────────────────

def test_a_takeaway_from_a_recollection_says_so(notion):
    """`Learn book X` pages are Claude's recollection, marked in the page body. A
    bullet lifted out of one and sent alone is a claim about a book nobody read,
    with the warning left behind on the page."""
    from config import UNVERIFIED_NOTE

    notion["blocks"]["p-1"] = [
        {"type": "callout", "callout": {"rich_text": [{"plain_text": UNVERIFIED_NOTE}]}},
    ] + TAKEAWAY_PAGE

    text, _ = takeaway.build_takeaway(choose=FIRST)

    assert "recollection" in text.lower()


def test_a_takeaway_from_a_real_source_is_not_marked(notion):
    """The mirror. A warning on every message is a warning on none."""
    text, _ = takeaway.build_takeaway(choose=FIRST)

    assert "recollection" not in text.lower()


# ─── THE WRITER AND THE READER USE ONE STRING ──────────────────────────────────

def test_the_writer_and_the_reader_agree_on_the_heading():
    """THE ONE THAT STOPS THE HEADING DRIFTING.

    Built by the REAL services/learn.build_notion_blocks and read by the REAL
    reader — not by a fixture that hard-codes the heading, which would agree with
    itself while production quietly stopped matching. Reword TAKEAWAYS_HEADING in
    one place only and this goes red.
    """
    from services.learn import build_notion_blocks

    blocks = build_notion_blocks(
        {"title": "T", "tldr": "A summary.", "sections": [],
         "key_takeaways": ["Deliberate practice beats repetition."]},
        source="https://example.com/x", content_type="article")

    assert takeaway.takeaways_in(as_notion_returns_it(blocks)) == [
        "Deliberate practice beats repetition."]


def test_the_reader_finds_nothing_when_the_writer_wrote_no_takeaways():
    """The mirror, through the same pair — a reader that matched anything would
    pass the test above and resurface the TL;DR."""
    from services.learn import build_notion_blocks

    blocks = build_notion_blocks(
        {"title": "T", "tldr": "A summary.", "sections": [{"heading": "Part one",
                                                           "content": "Prose."}]},
        source="https://example.com/x", content_type="article")

    assert takeaway.takeaways_in(as_notion_returns_it(blocks)) == []


# ─── IT SURVIVES REAL TEXT ─────────────────────────────────────────────────────

def test_markdown_in_a_takeaway_does_not_break_the_send(notion):
    """Driven through the real scheduler job, because that is where a parse_mode
    would be added."""
    import proactive.scheduler as scheduler
    from conftest import FakeContext, run

    notion["blocks"]["p-1"] = [
        heading(TAKEAWAYS_HEADING),
        bullet("Use *emphasis* and _underscores_ and `code` freely."),
    ]

    context = FakeContext()
    context.job = type("Job", (), {"chat_id": "chat-1"})()

    run(scheduler._takeaway_job(context))

    _, text, kwargs = context.bot.sent_full[0]
    assert "*emphasis*" in text
    assert kwargs.get("parse_mode") is None, (
        "the takeaway is a whole sentence of someone else's prose — Markdown "
        "would reject the message on exactly the interesting ones")
