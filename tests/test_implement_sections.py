"""Sectioned Manual updates — the drift and truncation fix.

THE BUG THIS LOCKS DOWN
-----------------------
`handle_implement` used to send the ENTIRE Manual plus the new source to Claude
and rebuild the whole page from the reply. Two consequences, both silent:

  • Sections the source said nothing about still went through the model and came
    back subtly reworded. Run it monthly and the Manual drifts away from what you
    wrote, one paraphrase at a time, with no diff to point at.
  • The Manual was truncated on the way IN at 40k characters
    (`manual_text[:40000]`), so past that the tail was dropped from the prompt —
    Claude merged against a Manual it could not see the end of, and the rebuilt
    page came back missing it.

Both die the same way: only the affected sections are ever sent, and only the
affected sections are ever written. The load-bearing assertion in this file is
`test_untouched_sections_are_never_sent_to_the_model` — everything else supports
it.

Claude is stubbed throughout; `tests/test_anthropic_client.py` covers the call
itself.
"""

import logging

import pytest

from config import MANUAL_SOURCE_CHARS, UNVERIFIED_MARKER, UNVERIFIED_NOTE
from services import implement
from conftest import FakeUpdate, run, with_update, written_nothing, written_ok

AREA_DB = "test-brain-id"          # BRAIN_ID in the fake environment

# Captured at import, before any fixture patches the module attribute away.
# The ledger tests below put it back: monkeypatch.undo() would drop every
# other patch the `manual` fixture installed along with it.
REAL_RECORD_SOURCE = implement.record_source


# ─── A FAKE MANUAL ─────────────────────────────────────────────────────────────
# Shaped exactly as build_manual_blocks emits one: decorated plain headings,
# content as following siblings, dividers between sections.

def heading(block_id, text, level=2):
    btype = f"heading_{level}"
    return {"id": block_id, "type": btype, btype: {"rich_text": [{"plain_text": text}]}}


def leaf(block_id, text, btype="bulleted_list_item"):
    return {"id": block_id, "type": btype, btype: {"rich_text": [{"plain_text": text}]}}


def divider(block_id):
    return {"id": block_id, "type": "divider", "divider": {}}


MANUAL_BLOCKS = [
    {"id": "c-1", "type": "callout",
     "callout": {"rich_text": [{"plain_text": "What this Manual covers"}]}},
    divider("d-1"),
    heading("h-process", "⚙️ Perfect Process"),
    leaf("p-1", "Prime the material", "numbered_list_item"),
    leaf("p-2", "Recall it cold", "numbered_list_item"),
    divider("d-2"),
    heading("h-improve", "🚀 Improvements & Optimizations"),
    leaf("i-1", "Spaced repetition beats massed practice"),
    divider("d-3"),
    heading("h-break", "📖 Step-by-Step Breakdown"),
    heading("h-recall", "→ Active Recall", level=3),
    leaf("r-1", "Force retrieval instead of review"),
    heading("h-interleave", "→ Interleaving", level=3),
    leaf("l-1", "Mix problem types within a session"),
    divider("d-4"),
    heading("h-sources", "📚 Sources"),
    leaf("s-1", "Make It Stick"),
]


@pytest.fixture
def manual(monkeypatch):
    """A Manual in Notion, plus a record of every Claude prompt and Notion write."""
    state = {
        "blocks": list(MANUAL_BLOCKS),
        "route_prompt": None,     # what the routing call was shown
        "merge_targets": None,    # what the merge call was shown
        "affected": [{"path": "Perfect Process"}],
        "new_steps": [],
        "updates": [{"path": "Perfect Process", "lines": ["Prime it", "Recall it cold"]}],
        "appends": [],            # [(after, blocks)]
        "deletes": [],
        "append_error": None,
        # What the failed append got as far as writing. Defaults to nothing —
        # a test that wants the half-written case sets it to written_half().
        "append_written": written_nothing(),
        "recorded": [],           # [(source_title, unverified)] per ledger write
        "source_text": "SOURCE BODY",
        "merge_source": None,     # what the merge call was given as the SOURCE
    }

    def get_children(page_id):
        # The Learn source page and the Manual are different pages; returning the
        # Manual for both would let a section's content leak in as "the source".
        if page_id == "source-1":
            return [leaf("src-1", state["source_text"])], None
        return list(state["blocks"]), None

    def route_sections(section_paths, source_text, source_title):
        state["route_prompt"] = {"paths": list(section_paths), "source": source_text}
        return {"affected": state["affected"], "new_steps": state["new_steps"]}, None

    def merge_sections(targets, source_text, source_title, unverified=False):
        state["merge_targets"] = [dict(t) for t in targets]
        state["merge_source"] = source_text
        state["merge_unverified"] = unverified
        return {"updates": state["updates"]}, None

    def append_children(block_id, blocks, after=None):
        if state["append_error"]:
            return state["append_written"], state["append_error"]
        state["appends"].append((after, blocks))
        return written_ok(1), None

    monkeypatch.setattr(implement, "get_area_db_id", lambda area: AREA_DB)
    monkeypatch.setattr(implement, "search_page_in_db",
                        lambda db, name, exact=False: (
                            {"id": "manual-1" if db == AREA_DB else "source-1",
                             "properties": {"Name": {"type": "title", "title": [
                                 {"plain_text": "Memory Techniques"}]}}}, None))
    monkeypatch.setattr(implement, "get_children", get_children)
    monkeypatch.setattr(implement, "route_sections", route_sections)
    monkeypatch.setattr(implement, "merge_sections", merge_sections)
    monkeypatch.setattr(implement, "append_children", append_children)
    monkeypatch.setattr(implement, "delete_block", state["deletes"].append)
    monkeypatch.setattr(implement, "update_page", lambda page_id, props: (True, None))
    # The Sources ledger runs AFTER the section writes and appends to a section
    # none of these tests touch. Stubbed here so `appends` and the write-ordering
    # assertions keep describing the section rewrite they exist for; the real
    # record_source is driven, unstubbed, in section 6 below.
    monkeypatch.setattr(implement, "record_source",
                        lambda page_id, sections, title, unverified: (
                            state["recorded"].append((title, unverified)) or (True, None)))
    return state


def implement_it():
    update = FakeUpdate(text="Implement Memory Techniques - Brain")
    run(implement.run_implement(update.message.text, **with_update(update)))
    return update


# ─── 1. THE INDEX ──────────────────────────────────────────────────────────────

def test_the_manual_is_indexed_by_heading(manual):
    sections, err = implement.read_manual_sections("manual-1")

    assert err is None
    assert [s.path for s in sections] == [
        "Perfect Process",
        "Improvements & Optimizations",
        "Step-by-Step Breakdown",
        "Step-by-Step Breakdown > Active Recall",
        "Step-by-Step Breakdown > Interleaving",
        "Sources",
    ]


def test_a_section_owns_the_blocks_between_its_heading_and_the_next(manual):
    sections, _ = implement.read_manual_sections("manual-1")
    process = next(s for s in sections if s.path == "Perfect Process")

    assert process.content_ids == ["p-1", "p-2"]


def test_dividers_are_structure_and_never_content(manual):
    """A divider swept into a section's content would be deleted on the first
    rewrite, so the page would lose its separators one section at a time."""
    sections, _ = implement.read_manual_sections("manual-1")

    for section in sections:
        assert "d-1" not in section.content_ids
        assert not any(i.startswith("d-") for i in section.content_ids)


def test_a_sections_style_is_read_from_its_existing_blocks(manual):
    """'Perfect Process' is a numbered routine. Rewriting it as bullets would
    quietly restyle the page every time it is merged."""
    sections, _ = implement.read_manual_sections("manual-1")
    by_path = {s.path: s for s in sections}

    assert by_path["Perfect Process"].style == "numbered"
    assert by_path["Improvements & Optimizations"].style == "bullet"


def test_heading_decoration_is_stripped_from_the_path(manual):
    """The model is shown 'Perfect Process' and answers with it, so '⚙️' never
    has to survive a round trip through the prompt."""
    sections, _ = implement.read_manual_sections("manual-1")

    assert all(not s.path.startswith(("⚙", "🚀", "📖", "📚", "→")) for s in sections)


# ─── 2. THE POINT: UNTOUCHED SECTIONS NEVER REACH THE MODEL ────────────────────

def test_untouched_sections_are_never_sent_to_the_model(manual):
    """THE test. Every section that reaches the model can come back reworded;
    the only guarantee that a section is unchanged is that it was never sent."""
    manual["affected"] = [{"path": "Step-by-Step Breakdown > Active Recall"}]
    manual["updates"] = [{"path": "Step-by-Step Breakdown > Active Recall",
                          "lines": ["Retrieve before reviewing"]}]

    implement_it()

    sent = [t["path"] for t in manual["merge_targets"]]
    assert sent == ["Step-by-Step Breakdown > Active Recall"]

    merged_text = " ".join(t["text"] for t in manual["merge_targets"])
    assert "Spaced repetition" not in merged_text, "an untouched section's content was sent"
    assert "Mix problem types" not in merged_text
    assert "Make It Stick" not in merged_text


def test_the_routing_call_sees_names_but_no_content(manual):
    """This is what makes routing cheap enough to be worth a second call."""
    implement_it()

    assert manual["route_prompt"]["paths"] == [
        "Perfect Process", "Improvements & Optimizations", "Step-by-Step Breakdown",
        "Step-by-Step Breakdown > Active Recall",
        "Step-by-Step Breakdown > Interleaving",
    ]
    assert "Prime the material" not in str(manual["route_prompt"])
    assert "Spaced repetition" not in str(manual["route_prompt"])


def test_only_the_affected_section_is_rewritten(manual):
    """The write half of the same property: untouched sections keep their blocks,
    so they stay byte-identical rather than merely similar."""
    implement_it()

    assert manual["deletes"] == ["p-1", "p-2"]
    assert len(manual["appends"]) == 1
    after, _ = manual["appends"][0]
    assert after == "h-process", "the replacement was not anchored under its heading"


def test_a_source_that_maps_to_nothing_changes_nothing(manual):
    manual["affected"] = []
    manual["updates"] = []

    update = FakeUpdate(text="Implement Memory Techniques - Brain")
    run(implement.run_implement(update.message.text, **with_update(update)))

    assert manual["merge_targets"] is None, "ran a merge with nothing to merge"
    assert manual["appends"] == []
    assert manual["deletes"] == []
    assert update.message.replied_with("doesn't map to anything")


# ─── 3. WRITE ORDERING (Hard Rule 2) ───────────────────────────────────────────

def test_the_replacement_is_appended_before_the_old_is_deleted(manual):
    order = []
    real_append = implement.append_children
    monkey_deletes = []

    def tracking_append(block_id, blocks, after=None):
        order.append("append")
        return real_append(block_id, blocks, after=after)

    def tracking_delete(block_id):
        order.append("delete")
        monkey_deletes.append(block_id)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(implement, "append_children", tracking_append)
        mp.setattr(implement, "delete_block", tracking_delete)
        implement_it()

    assert order[0] == "append", f"first write was {order[0]}"
    assert set(order[1:]) == {"delete"}
    assert monkey_deletes == ["p-1", "p-2"]


def test_a_failed_append_deletes_nothing(manual):
    """Notion has no transactions, so ordering is the only protection: the
    section still holds its previous content."""
    manual["append_error"] = "Notion 502: bad gateway"

    update = implement_it()

    assert manual["deletes"] == [], "deleted despite a failed append"
    assert update.message.replied_with("unchanged")


def test_the_stale_ids_come_from_the_pre_write_index(manual):
    """Deleting from a re-read after appending would list the new blocks too and
    delete the replacement along with the original."""
    def growing_get_children(page_id):
        if page_id == "source-1":
            return [leaf("src-1", "SOURCE BODY")], None
        snapshot = list(manual["blocks"])
        manual["blocks"].append(leaf("SHOULD-NOT-BE-DELETED", "freshly appended"))
        return snapshot, None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(implement, "get_children", growing_get_children)
        implement_it()

    assert "SHOULD-NOT-BE-DELETED" not in manual["deletes"]


# ─── 4. NEW SECTIONS ───────────────────────────────────────────────────────────

def test_a_genuinely_new_step_is_appended_not_forced_into_an_existing_one(manual):
    """A Manual that can never gain a step goes stale; one that rewrites a
    neighbouring step to fit is worse."""
    manual["affected"] = []
    manual["new_steps"] = [{"name": "Spaced Repetition"}]
    manual["updates"] = [{"path": "Step-by-Step Breakdown > Spaced Repetition",
                          "lines": ["Review at expanding intervals"]}]

    implement_it()

    assert manual["deletes"] == [], "a new section deleted something"
    assert len(manual["appends"]) == 1
    after, blocks = manual["appends"][0]
    assert after == "l-1", "not anchored at the end of the Step-by-Step region"
    assert blocks[0]["type"] == "heading_3"


def test_an_unknown_path_is_skipped_rather_than_guessed_at(manual):
    """Writing a section Claude invented into the nearest match would put content
    somewhere the user never looks."""
    manual["updates"] = [{"path": "Nonexistent Section", "lines": ["x"]}]

    update = implement_it()

    assert manual["appends"] == []
    assert manual["deletes"] == []
    assert update.message.replied_with("Skipped")


# ─── 5. NO TRUNCATION OF THE MANUAL ON THE WAY IN ──────────────────────────────

def test_a_huge_manual_does_not_get_its_tail_dropped(manual):
    """The old code sliced the whole Manual at `[:40000]` before prompting, so a
    long Manual was merged against a copy missing its end — and the rebuilt page
    came back missing it too. Sections are sent whole, and only the affected
    ones, so length is bounded by the section rather than by a slice.
    """
    manual["blocks"] = [
        heading("h-process", "⚙️ Perfect Process"),
        leaf("p-1", "x" * 60000, "numbered_list_item"),
        heading("h-tail", "📎 References"),
        leaf("s-1", "THE TAIL SECTION"),
    ]
    manual["affected"] = [{"path": "References"}]
    manual["updates"] = [{"path": "References", "lines": ["Make It Stick"]}]

    implement_it()

    sent = manual["merge_targets"]
    assert [t["path"] for t in sent] == ["References"]
    assert "THE TAIL SECTION" in sent[0]["text"], "the tail section was lost"
    assert manual["deletes"] == ["s-1"]


# ─── 6. THE SOURCES LEDGER ─────────────────────────────────────────────────────
# 📚 Sources is the one section David owns. It is a record of what has been
# merged into the page and when, which is the only place an unverified source's
# provenance survives — the merged CONTENT cannot carry it, because a marker
# inside a section goes back through the model on the next run and is reworded or
# dropped at its discretion.
#
# So it has to be unwritable by a merge, and that is guarded TWICE on purpose.
# Either guard alone can be undone by a change that looks reasonable in isolation:
# an inline `[s.path for s in sections]` at the routing call restores the section
# to the model's view, and a model can name a section it was never shown.

def test_sources_is_never_offered_to_the_routing_call(manual):
    """THE ONE THAT KEEPS THE EXCLUSION FROM BEING UNDONE.

    Asserted against the paths the REAL _sectioned_run passed to route_sections,
    not against routable_sections in isolation — the failure mode being guarded
    is someone rebuilding that list at the call site, which a unit test of the
    helper would not notice.
    """
    implement_it()

    assert "Sources" not in manual["route_prompt"]["paths"]
    assert not any("Sources" in path for path in manual["route_prompt"]["paths"])


def test_sources_is_still_indexed_even_though_it_is_not_routed(manual):
    """Excluded from ROUTING, not from the index.

    `record_source` has to find the section to append to it, and `Get Sources -
    Brain` has to be able to read it. Dropping it from read_manual_sections would
    break both while making this file's other assertions pass.
    """
    sections, _ = implement.read_manual_sections("manual-1")

    assert "Sources" in [s.path for s in sections]


def test_an_h3_under_sources_is_excluded_too(manual):
    """Matched on the first path segment, so a nested heading cannot slip past a
    check that only compared whole paths."""
    manual["blocks"] = MANUAL_BLOCKS + [
        heading("h-sub", "→ Primary", level=3),
        leaf("sub-1", "Make It Stick, chapter 2"),
    ]

    implement_it()

    assert not any(path.startswith("Sources") for path in manual["route_prompt"]["paths"])


def test_a_merge_naming_sources_is_refused_at_the_write(manual):
    """The second guard, and the reason there are two.

    The routing list controls what Claude SEES. This controls what it can DO, so
    a model naming a section it was never offered — or a future change to how
    that list is built — still cannot rewrite the ledger.
    """
    manual["affected"] = [{"path": "Sources"}]
    manual["updates"]  = [{"path": "Sources", "lines": ["Rewritten by the model"]}]

    update = implement_it()

    assert manual["deletes"] == [], "the ledger's existing entries were deleted"
    assert manual["appends"] == []
    assert update.message.replied_with("never rewritten by a merge")


# ─── 7. WHAT THE LEDGER WRITES ─────────────────────────────────────────────────
# These drive the REAL record_source — the fixture's stub is replaced — because
# the point is what reaches Notion, not that a stub was called.

@pytest.fixture
def ledger(manual, monkeypatch):
    """The real record_source, with its writes captured."""
    monkeypatch.setattr(implement, "record_source", REAL_RECORD_SOURCE)
    writes = []

    def append_children(block_id, blocks, after=None):
        writes.append({"after": after,
                       "texts": [b[b["type"]]["rich_text"][0]["text"]["content"]
                                 for b in blocks],
                       "types": [b["type"] for b in blocks]})
        return written_ok(len(blocks)), None

    monkeypatch.setattr(implement, "append_children", append_children)
    manual["writes"] = writes
    return manual


def test_every_run_appends_a_line_to_the_ledger(ledger):
    implement_it()

    entry = ledger["writes"][-1]
    assert entry["types"] == ["bulleted_list_item"]
    assert "Memory Techniques" in entry["texts"][0]


def test_the_ledger_line_says_when_a_source_was_unverified(ledger):
    ledger["blocks"] = [
        heading("h-process", "⚙️ Perfect Process"),
        leaf("p-1", "Prime the material", "numbered_list_item"),
        heading("h-sources", "📚 Sources"),
        leaf("s-1", "Make It Stick"),
    ]
    ledger["source_text"] = f"⚠️ {UNVERIFIED_MARKER}. No source text was read."

    implement_it()

    assert "unverified" in ledger["writes"][-1]["texts"][0]


def test_a_verified_source_is_not_called_unverified(ledger):
    """The mirror. A ledger that marks everything marks nothing."""
    implement_it()

    assert "unverified" not in ledger["writes"][-1]["texts"][0]


def test_the_ledger_entry_lands_inside_the_sources_section(ledger):
    implement_it()

    assert ledger["writes"][-1]["after"] == "s-1", (
        "the ledger line was appended somewhere other than under 📚 Sources")


def test_the_ledger_creates_its_own_section_when_there_is_none(ledger):
    """A Manual whose first run produced no sources has nowhere to append.

    Writing the bullet anyway would drop it at the end of the page under whatever
    section happens to be last — where the next merge into THAT section would
    delete it as stale content.
    """
    ledger["blocks"] = [
        heading("h-process", "⚙️ Perfect Process"),
        leaf("p-1", "Prime the material", "numbered_list_item"),
    ]

    implement_it()

    entry = ledger["writes"][-1]
    assert entry["types"] == ["heading_2", "bulleted_list_item"]
    assert "Sources" in entry["texts"][0]


# ─── 8. ONE INPUT BUDGET, APPLIED ONCE, OUT LOUD ───────────────────────────────
# The source half of both prompts used to be sliced at the call site — three
# `source_text[:60000]` in this module, two `summary_text[:50000]` in the Diet
# one, none of them related to any named constant and none of them mentioned in
# the reply. So a long Learn page merged into a Manual from its first 60k
# characters and the run reported a clean success. Same defect as the
# extractor/summariser pair that SUMMARY_INPUT_CHARS exists for, one layer along:
# what you get is a thin Manual entry months later with no way to tell which
# entries were affected.
#
# Two properties, and they need separate tests because either alone is passable
# with the bug still present: the cut happens ONCE (here), and the builders do
# not cut again (below, against the real functions).


def source_text_of(state) -> str:
    """The text run_implement will actually see for the fixture's source page.

    Not the same string as state["source_text"]: blocks_to_text decorates each
    block ("- " for a bullet), and the boundary tests below are only meaningful
    if they are exact about the value the cap is compared against.
    """
    return implement.blocks_to_text([leaf("src-1", state["source_text"])])


def set_source_length(state, length: int) -> str:
    """Make the source page produce exactly `length` characters of text."""
    state["source_text"] = "x" * length
    state["source_text"] = "x" * (2 * length - len(source_text_of(state)))
    assert len(source_text_of(state)) == length
    return source_text_of(state)


def test_an_oversized_source_reaches_both_calls_already_at_the_cap(manual):
    """THE ONE THAT SAYS THE CUT HAPPENED, AND WHERE."""
    set_source_length(manual, MANUAL_SOURCE_CHARS + 5_000)

    implement_it()

    assert len(manual["route_prompt"]["source"]) == MANUAL_SOURCE_CHARS
    assert len(manual["merge_source"]) == MANUAL_SOURCE_CHARS


def test_the_prompt_builders_do_not_cut_again(monkeypatch):
    """THE ONE THAT SAYS THE SECOND CAP IS GONE.

    Not lowered, not moved — gone. Driven against the real builders with only
    Anthropic stubbed, because a cap at both ends makes a source of exactly the
    budget and one cut down to it the same string, and the truncation warning
    becomes unreliable at exactly its own boundary. That is the property the
    `run_learn` copy of this rule is written for, and it applies here unchanged.
    """
    prompts = []
    monkeypatch.setattr(implement, "complete_json",
                        lambda system, user, schema, **kw: (prompts.append(user), ({}, None))[1])

    oversized = "x" * (MANUAL_SOURCE_CHARS + 5_000)
    implement.route_sections(["Perfect Process"], oversized, "T")
    implement.merge_sections([{"path": "Perfect Process", "style": "bullet", "text": "now"}],
                             oversized, "T")
    implement.build_manual(oversized, "Brain — T", "T")

    for name, prompt in zip(["route_sections", "merge_sections", "build_manual"], prompts):
        assert prompt.count("x") == len(oversized), (
            f"{name} sliced its source again: it carried {prompt.count('x')} of "
            f"{len(oversized)} characters")


def test_the_reply_says_the_source_was_truncated(manual):
    """A partial merge you are told about is a different object from one you find
    months later. The reply is the only place you can be told at the time."""
    set_source_length(manual, MANUAL_SOURCE_CHARS + 5_000)

    update = implement_it()

    assert update.message.replied_with("merging from the first"), (
        f"nothing said the source was cut: {update.message.reply_texts}")
    assert update.message.replied_with(f"{MANUAL_SOURCE_CHARS + 5_000:,}"), (
        "the warning did not say how long the source actually was")


def test_a_source_within_budget_is_never_reported_as_truncated(manual):
    """The mirror. A warning that always fires is not a warning."""
    update = implement_it()

    assert not update.message.replied_with("merging from the first"), (
        f"a short source was reported as truncated: {update.message.reply_texts}")


def test_a_source_of_exactly_the_budget_is_not_truncated(manual):
    """THE BOUNDARY — of the WARNING, and only of the warning.

    Be precise about what this does not do: it does NOT prove the second cap is
    gone. A builder slicing at the same number the caller cut to is a no-op, so
    this stays green with `[:60000]` put back — verified by doing exactly that.
    `test_the_prompt_builders_do_not_cut_again` is the one that sees it, because
    it drives the builders with text the caller never cut.

    What it guards is the thing that made the old bug undetectable from the
    outside: at the boundary, "a source of exactly the budget" and "a source cut
    down to it" are the same string, so a warning that fires here would be
    indistinguishable from one that fires correctly.
    """
    set_source_length(manual, MANUAL_SOURCE_CHARS)

    update = implement_it()

    assert len(manual["merge_source"]) == MANUAL_SOURCE_CHARS
    assert not update.message.replied_with("merging from the first"), (
        f"a source of exactly the budget was called partial: "
        f"{update.message.reply_texts}")


def test_the_cut_is_logged_with_the_length_before_it(manual, caplog):
    """The reply scrolls away; the log is what is left afterwards, and the
    pre-truncation length is the only number that says how much was lost."""
    set_source_length(manual, MANUAL_SOURCE_CHARS + 5_000)

    with caplog.at_level(logging.WARNING, logger="services.implement"):
        implement_it()

    cuts = [r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING and "cut to" in r.getMessage()]
    assert cuts, "the truncation was not logged at WARNING"
    assert str(MANUAL_SOURCE_CHARS + 5_000) in cuts[0], (
        f"the log line does not carry the length before the cut: {cuts[0]}")


def test_the_unverified_marker_survives_the_cut(manual):
    """If truncation could drop the marker it would silently disarm the
    additions-only rule on exactly the long recollections that most need it.

    It cannot, because Learn writes the callout as the FIRST block of the page —
    above the TL;DR — so a head-slice always keeps it. That is a property of the
    two modules agreeing, which is why it is asserted here rather than assumed
    from reading one of them.
    """
    manual["source_text"] = UNVERIFIED_NOTE + "\n" + "x" * (MANUAL_SOURCE_CHARS + 5_000)

    update = implement_it()

    assert len(manual["merge_source"]) == MANUAL_SOURCE_CHARS
    assert UNVERIFIED_MARKER in manual["merge_source"], "the cut dropped the marker"
    assert manual["merge_unverified"] is True, "the merge was not told the source is unverified"
    # And the rule it arms still engages: the default updates drop
    # "Prime the material", which an unverified source may not do.
    assert update.message.replied_with("Held back")
    assert manual["deletes"] == []


def test_the_ledger_only_ever_appends(ledger):
    """It is a pure append, which is why it can run after the section writes
    without any of Hard Rule 2's ordering care — there is no window in which the
    page holds neither the old content nor the new."""
    implement_it()

    assert ledger["deletes"] == ["p-1", "p-2"], (
        "the ledger deleted something, or the section write stopped deleting")
