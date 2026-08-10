"""A page built from recollection says so, and Implement repeats it.

THE BUG THIS LOCKS DOWN
-----------------------
`Learn book Atomic Habits` sends "Please summarise the book: X" and files the
answer in Notion. No text was fetched, no text was read, and nothing on the page
recorded that — so it sits next to pages built from a real transcript, looking
identical. For anything obscure, translated, or published recently, the model
will produce confident, plausible, wrong detail: a quote that is not in the
book, a study that does not exist, a date off by a decade.

That content does not stay on that page. `Implement [Page] - [Area]` merges it
into a Manual, where it loses even the context of having come from a `Learn
book` command, and the Manual is what gets read years later as settled.

WHY THE MARKER IS A SENTENCE AND NOT A PROPERTY
-----------------------------------------------
A Notion checkbox would be invisible in three places that matter: on the page
itself unless you open the properties panel, in `blocks_to_text` (which is how
Implement reads a source), and therefore in the prompt the merge call is given.
A sentence in the body is visible in all three and costs no extra read to
detect. `test_the_marker_survives_the_flattening_implement_reads_through` is the
one that would go red if it were ever moved to a property.
"""

import pytest

from config import UNVERIFIED_MARKER, is_unverified_source
from clients.notion_client import blocks_to_text
from conftest import FakeUpdate, run, with_update
from services import implement, learn

SUMMARY = {"title": "Atomic Habits", "tldr": "Small habits compound.",
           "sections": [{"heading": "Cues", "content": "Make it obvious."}],
           "key_takeaways": ["Stack a new habit onto an old one."]}


# ─── 1. THE MARKER IS WRITTEN, AND ONLY WHERE IT BELONGS ───────────────────────

def callouts(blocks):
    return [b for b in blocks if b["type"] == "callout"]


def test_a_book_page_opens_with_the_unverified_callout():
    blocks = learn.build_notion_blocks(SUMMARY, "", "book")

    first = blocks[0]
    assert first["type"] == "callout"
    assert UNVERIFIED_MARKER in first["callout"]["rich_text"][0]["text"]["content"]


def test_the_warning_comes_before_the_tldr():
    """The TL;DR is the one part of the page anybody reads in a hurry.

    A warning underneath it is a warning you meet after you have already believed
    the summary.
    """
    blocks = learn.build_notion_blocks(SUMMARY, "", "book")

    assert UNVERIFIED_MARKER in blocks[0]["callout"]["rich_text"][0]["text"]["content"]
    assert "Small habits compound." in blocks[1]["callout"]["rich_text"][0]["text"]["content"]


@pytest.mark.parametrize("content_type", ["article", "video", "podcast", "pdf"])
def test_a_page_built_from_real_text_is_not_marked(content_type):
    """A warning on everything is a warning on nothing.

    These four types are summarised from bytes that actually arrived. Marking
    them too would make the marker meaningless in exactly the place it has to
    carry meaning — the Manual it flows into.
    """
    blocks = learn.build_notion_blocks(SUMMARY, "https://example.com/post", content_type)

    assert not any(UNVERIFIED_MARKER in c["callout"]["rich_text"][0]["text"]["content"]
                   for c in callouts(blocks))


def as_notion_returns(block: dict) -> dict:
    """A written block as it comes BACK from Notion.

    Two shapes, and only one of them is the one Implement ever sees. A block on
    the way IN carries `{"text": {"content": …}}`; the same block read back
    carries a `plain_text` alongside it, and `extract_rich_text` — so
    `blocks_to_text`, so everything Implement does — reads only `plain_text`.
    Asserting against the request shape would pass on a builder whose output
    flattens to nothing at all, which is precisely the mistake worth guarding.
    """
    btype = block["type"]
    body  = dict(block.get(btype, {}))
    body["rich_text"] = [dict(rt, plain_text=rt.get("text", {}).get("content", ""))
                         for rt in body.get("rich_text", [])]
    return dict(block, **{btype: body})


def test_the_marker_survives_the_flattening_implement_reads_through():
    """THE LOAD-BEARING ONE.

    Implement never sees the blocks — it sees blocks_to_text(blocks), and that is
    also what it sends to Claude. A marker that does not survive this function is
    a marker Implement cannot act on and the merge call is never told about, no
    matter how visible it is in Notion. This is what a property would fail.
    """
    blocks = learn.build_notion_blocks(SUMMARY, "", "book")

    assert is_unverified_source(blocks_to_text([as_notion_returns(b) for b in blocks]))


def test_one_predicate_answers_for_both_sides():
    """The writer and the reader share `is_unverified_source`.

    Two `in` checks in two modules is how a marker gets reworded in one of them
    and silently stops being detected in the other — with nothing failing, and
    every page written after the change quietly unmarked to Implement.
    """
    assert is_unverified_source(f"blah {UNVERIFIED_MARKER} blah")
    assert not is_unverified_source("An ordinary summary of an ordinary article.")
    assert not is_unverified_source("")
    assert not is_unverified_source(None)


def test_the_reply_says_it_too(monkeypatch):
    """Notion is where it is stored; Telegram is where it is read first."""
    monkeypatch.setattr(learn, "summarize_with_claude",
                        lambda *a, **k: (SUMMARY, None))
    monkeypatch.setattr(learn, "create_learn_page",
                        lambda *a, **k: (True, "page-1", None))
    update = FakeUpdate(text="Learn book Atomic Habits")

    run(learn.run_learn("Learn book Atomic Habits", **with_update(update)))

    assert update.message.replied_with("recollection")


# ─── 2. IMPLEMENT SURFACES IT ──────────────────────────────────────────────────

MANUAL = [
    {"id": "h-1", "type": "heading_2",
     "heading_2": {"rich_text": [{"plain_text": "⚙️ Perfect Process"}]}},
    {"id": "p-1", "type": "numbered_list_item",
     "numbered_list_item": {"rich_text": [{"plain_text": "Prime the material"}]}},
]


@pytest.fixture
def wired(monkeypatch):
    """The real run_implement, with a source page whose content the test picks."""
    state = {"source": "An ordinary summary of an ordinary article."}

    def get_children(page_id):
        if page_id == "source-1":
            return [{"id": "s-1", "type": "paragraph",
                     "paragraph": {"rich_text": [{"plain_text": state["source"]}]}}], None
        return list(MANUAL), None

    monkeypatch.setattr(implement, "get_area_db_id", lambda area: "area-db-1")
    monkeypatch.setattr(implement, "search_page_in_db",
                        lambda db, name, exact=False: (
                            {"id": "manual-1" if db == "area-db-1" else "source-1",
                             "properties": {}}, None))
    monkeypatch.setattr(implement, "get_children", get_children)
    monkeypatch.setattr(implement, "route_sections",
                        lambda paths, text, title: (
                            {"affected": [{"path": "Perfect Process"}], "new_steps": []}, None))
    monkeypatch.setattr(implement, "merge_sections",
                        lambda targets, text, title: (
                            {"updates": [{"path": "Perfect Process", "lines": ["merged"]}]}, None))
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: (1, [], []))
    monkeypatch.setattr(implement, "delete_block", lambda block_id: None)
    monkeypatch.setattr(implement, "update_page", lambda page_id, props: (True, None))
    return state


def implement_it():
    update = FakeUpdate(text="Implement Atomic Habits - Brain")
    run(implement.run_implement(update.message.text, **with_update(update)))
    return update


def unverified_replies(update):
    return [text for text in update.message.reply_texts if "Unverified source" in text]


def test_the_plan_message_warns_when_the_source_is_unverified(wired):
    """THE ONE THAT WAS ASKED FOR.

    The plan is sent BEFORE the merge call and before any write, which is the
    only moment the warning is actionable — after the write the Manual already
    holds the content and the message is a post-mortem.
    """
    wired["source"] = f"⚠️ {UNVERIFIED_MARKER}. No source text was read."

    update = implement_it()

    plan = next(t for t in update.message.reply_texts if "Plan" in t)
    assert "Unverified source" in plan


def test_a_verified_source_produces_no_warning_anywhere(wired):
    """The mirror. A warning that always fires is not a warning.

    Reverting the `if unverified` guard leaves the test above green and this one
    red, which is the only reason both exist.
    """
    update = implement_it()

    assert unverified_replies(update) == []


def test_the_final_message_repeats_it(wired):
    """The plan scrolls away; the confirmation is what you come back to."""
    wired["source"] = f"⚠️ {UNVERIFIED_MARKER}. No source text was read."

    update = implement_it()

    done = next(t for t in update.message.reply_texts if "Manual updated" in t)
    assert "Unverified source" in done


def test_the_first_run_warns_before_building_the_whole_manual(wired, monkeypatch):
    """The worst case: no Manual yet, so this one source becomes all of it."""
    wired["source"] = f"⚠️ {UNVERIFIED_MARKER}. No source text was read."
    monkeypatch.setattr(implement, "search_page_in_db",
                        lambda db, name, exact=False: (
                            (None, "not found") if db == "area-db-1"
                            else ({"id": "source-1", "properties": {}}, None)))
    monkeypatch.setattr(implement, "build_manual",
                        lambda text, topic, title="": (
                            {"title": "Manual: Habits", "routine": []}, None))
    monkeypatch.setattr(implement, "create_manual_page",
                        lambda db_id, blocks: ("manual-new", None))

    update = implement_it()

    warned_before_build = update.message.reply_texts.index(
        next(t for t in update.message.reply_texts if "Unverified source" in t))
    building = update.message.reply_texts.index(
        next(t for t in update.message.reply_texts if "building the Manual" in t))
    assert warned_before_build < building, "warned only after the build had run"
