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
from conftest import FakeUpdate, run, with_update, written_ok
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
    state = {"source": "An ordinary summary of an ordinary article.",
             # Reproduces the Manual's existing line and adds one, so the
             # additions-only rule lets it through by default. A test that wants
             # the rule to bite overwrites this.
             "merged_lines": ["Prime the material", "Stack a cue onto an old habit"],
             "manual": list(MANUAL),
             "ledger": []}

    def get_children(page_id):
        if page_id == "source-1":
            return [{"id": "s-1", "type": "paragraph",
                     "paragraph": {"rich_text": [{"plain_text": state["source"]}]}}], None
        return list(state["manual"]), None

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
                        lambda targets, text, title, unverified=False: (
                            {"updates": [{"path": "Perfect Process",
                                          "lines": state["merged_lines"]}]}, None))
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: (1, [], []))
    monkeypatch.setattr(implement, "delete_block", lambda block_id: None)
    # The Sources ledger write. Stubbed rather than left live, or it would reach
    # the real append_children and, through it, the network.
    monkeypatch.setattr(implement, "append_children",
                        lambda block_id, blocks, after=None: (
                            state["ledger"].append(blocks) or written_ok(len(blocks)), None))
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


# ─── 3. THE ADDITIONS-ONLY RULE ────────────────────────────────────────────────
# WHAT IS ENFORCED HERE, and it is worth being exact because the difference is the
# whole value of these tests: for an unverified source, a section is written only
# if every one of its existing lines survives verbatim. No existing line can be
# deleted or reworded on the authority of a recollection.
#
# WHAT IS NOT ENFORCED, by this or by anything: whether an ADDED line is true.
# Nothing can check that, which is why the Sources ledger exists.
#
# The merge prompt is told the same rule (_UNVERIFIED_MERGE_RULE), but a prompt is
# a request a model can decline silently. These tests drive the CHECK.

UNVERIFIED_SOURCE = f"⚠️ {UNVERIFIED_MARKER}. No source text was read."


def test_a_merge_that_would_reword_an_existing_line_is_held_back(wired):
    """The line is not gone from the model's answer — it is REWORDED, which is
    the failure mode a "did it delete anything" check would wave through."""
    wired["source"] = UNVERIFIED_SOURCE
    wired["merged_lines"] = ["Prime the material thoroughly first",
                             "Stack a cue onto an old habit"]

    update = implement_it()

    assert update.message.replied_with("Held back")
    assert update.message.replied_with("Prime the material")


def test_a_held_back_section_is_not_written_at_all(wired, monkeypatch):
    written = []
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: (
                            written.extend(updates) or (0, [], [])))
    wired["source"] = UNVERIFIED_SOURCE
    wired["merged_lines"] = ["Something else entirely"]

    implement_it()

    assert written == [], "an unverified rewrite reached the writer"


def test_an_addition_that_keeps_every_existing_line_goes_through(wired, monkeypatch):
    """THE MIRROR, and the reason the rule is worth having rather than a blanket
    refusal: without this the check could be "hold back everything" and every
    other test here would still pass."""
    written = []
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: (
                            written.extend(updates) or (1, [], [])))
    wired["source"] = UNVERIFIED_SOURCE
    wired["merged_lines"] = ["Prime the material", "Stack a cue onto an old habit"]

    update = implement_it()

    assert [u["path"] for u in written] == ["Perfect Process"]
    assert not update.message.replied_with("Held back")


def test_whitespace_alone_does_not_hold_a_section_back(wired):
    """A model reflowing "Prime  the material" is not rewriting it, and a rule
    that fires on that would be turned off within a week."""
    wired["source"] = UNVERIFIED_SOURCE
    wired["merged_lines"] = ["Prime   the material", "Stack a cue onto an old habit"]

    update = implement_it()

    assert not update.message.replied_with("Held back")


def test_reordering_existing_lines_is_a_rewrite(wired):
    """Order carries meaning in a numbered routine, so a merge that keeps both
    lines and swaps them has changed the Manual. A membership check would pass it."""
    wired["source"] = UNVERIFIED_SOURCE
    wired["merged_lines"] = ["A new first step", "Prime the material"]
    wired["manual"] = MANUAL + [
        {"id": "p-2", "type": "numbered_list_item",
         "numbered_list_item": {"rich_text": [{"plain_text": "A new first step"}]}}]

    update = implement_it()

    assert update.message.replied_with("Held back")


def test_a_verified_source_may_rewrite_whatever_it_likes(wired, monkeypatch):
    """The rule is about UNVERIFIED sources only.

    An article you actually read is allowed to correct your Manual — that is what
    Implement is for. Applying the rule to everything would quietly turn every
    merge into an append.
    """
    written = []
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: (
                            written.extend(updates) or (1, [], [])))
    wired["merged_lines"] = ["Nothing of the original survives this"]

    update = implement_it()

    assert len(written) == 1, "a verified source was held back"
    assert not update.message.replied_with("Held back")


def test_the_merge_prompt_carries_the_additions_only_rule(monkeypatch):
    """The hint itself, asserted on the PROMPT and not on the call.

    The test below only proves the flag reaches merge_sections; deleting the text
    it appends leaves that one green, which is the difference between guarding the
    plumbing and guarding what goes down it. The real function is driven here,
    with only the Anthropic call replaced.
    """
    prompts = []
    monkeypatch.setattr(implement, "complete_json",
                        lambda system, user, schema, **kw: prompts.append(user) or ({}, None))
    target = [{"path": "Perfect Process", "style": "numbered", "text": "Prime the material"}]

    implement.merge_sections(target, "source", "T", True)
    implement.merge_sections(target, "source", "T", False)

    assert "UNVERIFIED" in prompts[0]
    assert "EXACTLY as it is given" in prompts[0]
    assert "UNVERIFIED" not in prompts[1], (
        "a verified merge was told it was unverified")


def test_the_merge_call_is_told_when_the_source_is_unverified(wired, monkeypatch):
    """The plumbing: the flag has to reach merge_sections at all. Not a guarantee
    on its own — a model can ignore the instruction — which is exactly why
    _hold_back_rewrites exists as well."""
    seen = {}
    monkeypatch.setattr(implement, "merge_sections",
                        lambda targets, text, title, unverified=False: (
                            seen.update(unverified=unverified)
                            or ({"updates": []}, None)))
    wired["source"] = UNVERIFIED_SOURCE

    implement_it()

    assert seen["unverified"] is True


def test_a_new_section_has_nothing_to_protect(wired, monkeypatch):
    """A brand-new step has no existing lines, so the rule has nothing to say and
    must not hold it back — otherwise an unverified source could never add
    anything at all, and the feature would be a refusal with extra steps."""
    written = []
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: (
                            written.extend(updates) or (1, [], [])))
    monkeypatch.setattr(implement, "merge_sections",
                        lambda targets, text, title, unverified=False: (
                            {"updates": [{"path": "Step-by-Step Breakdown > Habit Stacking",
                                          "lines": ["Attach it to an existing routine"]}]}, None))
    wired["source"] = UNVERIFIED_SOURCE

    implement_it()

    assert len(written) == 1


# ─── 4. THE DIET PATH ──────────────────────────────────────────────────────────

def test_the_diet_path_warns_too(monkeypatch):
    """run_implement branches to the Diet handler BEFORE it reads the source page,
    so the detection it does never ran for Diet at all — this warning was missing
    on that path for its whole life."""
    from services import implement_diet

    update = FakeUpdate(text="Implement Atomic Habits - Diet")
    monkeypatch.setattr(implement_diet, "search_page_in_db",
                        lambda db, name, exact=False: ({"id": "summary-1", "properties": {
                            "Name": {"type": "title",
                                     "title": [{"plain_text": "Atomic Habits"}]}}}, None))
    monkeypatch.setattr(implement_diet, "get_children",
                        lambda block_id: ([{"id": "s-1", "type": "paragraph", "paragraph": {
                            "rich_text": [{"plain_text": UNVERIFIED_SOURCE}]}}], None))
    monkeypatch.setattr(implement_diet, "find_or_create_diet_page",
                        lambda: ("diet-1", False, None))
    monkeypatch.setattr(implement_diet, "read_diet_structure",
                        lambda page_id: ({"Goals": {"Fat Loss": {}}},
                                         {"Goals>Fat Loss": "block-1"}, None))
    monkeypatch.setattr(implement_diet, "route_sections",
                        lambda paths, text, title: (
                            {"affected": [{"path": "Goals>Fat Loss"}]}, None))
    monkeypatch.setattr(implement_diet, "read_section_contents",
                        lambda sections: ({"Goals>Fat Loss": "current"}, None))
    monkeypatch.setattr(implement_diet, "merge_sections",
                        lambda contents, text, title: ({"updates": []}, None))
    monkeypatch.setattr(implement_diet, "apply_updates",
                        lambda updates, block_map: (0, []))
    monkeypatch.setattr(implement_diet, "update_page", lambda page_id, props: (True, None))

    run(implement.run_implement(update.message.text, **with_update(update)))

    plan = next(t for t in update.message.reply_texts if "Plan" in t)
    assert "Unverified source" in plan
