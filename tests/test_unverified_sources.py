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


QUOTED = {**SUMMARY, "sections": [{"heading": "Cues", "content": "Make it obvious.",
                                   "quotes": ["You do not rise to the level of your goals."]}]}


def quote_blocks(blocks):
    return [b for b in blocks if b["type"] == "quote"]


def _section_properties(schema: dict) -> dict:
    return schema["properties"]["sections"]["items"]["properties"]


def _schema_sent(monkeypatch, content_type: str) -> dict:
    """The schema `summarize_with_claude` actually sends for this type."""
    sent = {}

    def spy(system, user, schema, **kwargs):
        sent["schema"] = schema
        return SUMMARY, None

    monkeypatch.setattr(learn, "complete_json", spy)
    learn.summarize_with_claude(content_type, "text", title="T", source="s")
    return sent["schema"]


def test_a_recollection_is_not_asked_for_quotes(monkeypatch):
    """RED before the fix. The summary schema asked every type for "Verbatim
    quotes worth preserving" — including `Learn book`, where nothing was read and
    a "verbatim" quote can only come from memory. A plausible sentence the author
    never wrote, set in a quote block, is the most convincing kind of invention.

    Structured output makes this a guarantee rather than a request: the schema
    for a recollection has no `quotes` field and every object is closed, so the
    API cannot return one."""
    schema = _schema_sent(monkeypatch, "book")

    assert "quotes" not in _section_properties(schema)
    assert schema["properties"]["sections"]["items"]["additionalProperties"] is False


@pytest.mark.parametrize("content_type", ["article", "video", "podcast", "pdf"])
def test_a_source_that_was_read_is_still_asked_for_quotes(monkeypatch, content_type):
    """A quote lifted from a transcript that arrived is worth keeping."""
    assert "quotes" in _section_properties(_schema_sent(monkeypatch, content_type))


def test_a_recollection_page_never_shows_a_quote_block():
    """RED before the fix. The second, independent guard: whatever a summary
    holds, a recollection's page renders no quote blocks. It does not depend on
    the schema above, so loosening that one alone does not bring quotes back."""
    blocks = learn.build_notion_blocks(QUOTED, "", "book")

    assert quote_blocks(blocks) == []
    assert any(b["type"] == "paragraph" for b in blocks), "dropped the section too"


def test_a_page_built_from_real_text_keeps_its_quotes():
    blocks = learn.build_notion_blocks(QUOTED, "https://example.com/post", "article")

    assert len(quote_blocks(blocks)) == 1


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
             # One addition, which the additions-only rule lets through. A test
             # that wants the rule to bite overwrites this.
             "ops": [op("add", 1, "Stack a cue onto an old habit")],
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
                                          "ops": state["ops"]}]}, None))
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: (
                            [implement.SectionEdit("Perfect Process")], [], []))
    monkeypatch.setattr(implement, "delete_block", lambda block_id: None)
    # The Sources ledger write. Stubbed rather than left live, or it would reach
    # the real append_children and, through it, the network.
    monkeypatch.setattr(implement, "append_children",
                        lambda block_id, blocks, after=None: (
                            state["ledger"].append(blocks) or written_ok(len(blocks)), None))
    monkeypatch.setattr(implement, "update_page", lambda page_id, props: (True, None))
    return state


def op(kind, line, text="", why=""):
    return {"op": kind, "line": line, "text": text, "why": why}


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
                            (None, None) if db == "area-db-1"     # no Manual yet
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
# whole value of these tests: for an unverified source, only `add` operations are
# written. Every replace and remove is held back and named, so no existing line
# can be changed or deleted on the authority of a recollection.
#
# WHAT IS NOT ENFORCED, by this or by anything: whether an ADDED line is true.
# Nothing can check that, which is why the Sources ledger exists.
#
# The merge prompt is told the same rule (_UNVERIFIED_MERGE_RULE), but a prompt is
# a request a model can decline silently. These tests drive the CHECK.

UNVERIFIED_SOURCE = f"⚠️ {UNVERIFIED_MARKER}. No source text was read."


@pytest.fixture
def writer(wired, monkeypatch):
    """What reached apply_section_updates, the one function that writes."""
    written = []
    monkeypatch.setattr(implement, "apply_section_updates",
                        lambda page_id, updates, sections, new_paths=None: (
                            written.extend(updates)
                            or ([implement.SectionEdit(u["path"]) for u in updates], [], [])))
    return written


@pytest.mark.parametrize("kind", ["replace", "remove"])
def test_an_unverified_edit_to_an_existing_line_is_held_back(wired, writer, kind):
    """Named in the reply by the line it would have changed: "an edit was held
    back" is not something you can act on, "Prime the material" is."""
    wired["source"] = UNVERIFIED_SOURCE
    wired["ops"] = [op(kind, 1, "Prime the material thoroughly first" if kind == "replace" else "")]

    update = implement_it()

    assert writer == [], "an unverified edit reached the writer"
    assert update.message.replied_with("Held back")
    assert update.message.replied_with("“Prime the material”")


def test_the_additions_of_an_unverified_merge_still_go_through(wired, writer):
    """THE CHANGE FROM THE OLD RULE. It held back the whole section, additions
    included, the moment one existing line was missing or reworded; the per-op
    filter keeps what it may and drops only what it must."""
    wired["source"] = UNVERIFIED_SOURCE
    wired["ops"] = [op("replace", 1, "Prime it harder"), op("add", 1, "Stack a cue")]

    update = implement_it()

    assert writer == [{"path": "Perfect Process", "ops": [op("add", 1, "Stack a cue")]}]
    assert update.message.replied_with("Held back")


def test_an_unverified_merge_of_additions_only_goes_through(wired, writer):
    """THE MIRROR, and the reason the rule is worth having rather than a blanket
    refusal: without this the check could be "hold back everything" and every
    other test here would still pass."""
    wired["source"] = UNVERIFIED_SOURCE

    update = implement_it()

    assert [u["path"] for u in writer] == ["Perfect Process"]
    assert not update.message.replied_with("Held back")


def test_a_verified_source_may_rewrite_whatever_it_likes(wired, writer):
    """The rule is about UNVERIFIED sources only.

    An article you actually read is allowed to correct your Manual — that is what
    Implement is for. Applying the rule to everything would quietly turn every
    merge into an append.
    """
    wired["ops"] = [op("replace", 1, "Nothing of the original survives this"), op("remove", 1)]

    update = implement_it()

    assert writer == [{"path": "Perfect Process", "ops": wired["ops"]}]
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
    target = [{"path": "Perfect Process", "style": "numbered", "text": "[1] Prime the material"}]

    implement.merge_sections(target, "source", "T", True)
    implement.merge_sections(target, "source", "T", False)

    assert "UNVERIFIED" in prompts[0]
    assert "Use add only" in prompts[0]
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


def test_a_new_section_takes_its_additions(wired, writer, monkeypatch):
    """A brand-new step is nothing but additions, so an unverified source can
    still add one — otherwise it could never add anything at all, and the
    feature would be a refusal with extra steps."""
    monkeypatch.setattr(implement, "merge_sections",
                        lambda targets, text, title, unverified=False: (
                            {"updates": [{"path": "Step-by-Step Breakdown > Habit Stacking",
                                          "ops": [op("add", 0, "Attach it to an existing routine")]}]},
                            None))
    wired["source"] = UNVERIFIED_SOURCE

    implement_it()

    assert len(writer) == 1


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
                        lambda sections: ({"Goals>Fat Loss": []}, None))
    monkeypatch.setattr(implement_diet, "merge_sections",
                        lambda contents, text, title, unverified=False: ({"updates": []}, None))
    monkeypatch.setattr(implement_diet, "apply_updates",
                        lambda updates, targets, contents: ([], [], []))
    monkeypatch.setattr(implement_diet, "update_page", lambda page_id, props: (True, None))

    run(implement.run_implement(update.message.text, **with_update(update)))

    plan = next(t for t in update.message.reply_texts if "Plan" in t)
    assert "Unverified source" in plan
