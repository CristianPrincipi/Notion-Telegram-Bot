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

import pytest

import implement
from conftest import FakeUpdate, run

AREA_DB = "test-brain-id"          # BRAIN_ID in the fake environment


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
    }

    def get_children(page_id):
        # The Learn source page and the Manual are different pages; returning the
        # Manual for both would let a section's content leak in as "the source".
        if page_id == "source-1":
            return [leaf("src-1", "SOURCE BODY")], None
        return list(state["blocks"]), None

    def route_sections(section_paths, source_text, source_title):
        state["route_prompt"] = {"paths": list(section_paths), "source": source_text}
        return {"affected": state["affected"], "new_steps": state["new_steps"]}, None

    def merge_sections(targets, source_text, source_title):
        state["merge_targets"] = [dict(t) for t in targets]
        return {"updates": state["updates"]}, None

    def append_children(block_id, blocks, after=None):
        if state["append_error"]:
            return [], state["append_error"]
        state["appends"].append((after, blocks))
        return [{"id": f"new-{len(state['appends'])}"}], None

    monkeypatch.setattr(implement, "get_area_db_id", lambda area: AREA_DB)
    monkeypatch.setattr(implement, "search_page_in_db",
                        lambda db, name, exact=False: (
                            {"id": "manual-1" if db == AREA_DB else "source-1",
                             "properties": {}}, None))
    monkeypatch.setattr(implement, "get_children", get_children)
    monkeypatch.setattr(implement, "route_sections", route_sections)
    monkeypatch.setattr(implement, "merge_sections", merge_sections)
    monkeypatch.setattr(implement, "append_children", append_children)
    monkeypatch.setattr(implement, "delete_block", state["deletes"].append)
    monkeypatch.setattr(implement, "update_page", lambda page_id, props: (True, None))
    return state


def implement_it():
    update = FakeUpdate(text="Implement Memory Techniques - Brain")
    run(implement.handle_implement(update, update.message.text))
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
        "Step-by-Step Breakdown > Interleaving", "Sources",
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
    run(implement.handle_implement(update, update.message.text))

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
        heading("h-tail", "📚 Sources"),
        leaf("s-1", "THE TAIL SECTION"),
    ]
    manual["affected"] = [{"path": "Sources"}]
    manual["updates"] = [{"path": "Sources", "lines": ["Make It Stick"]}]

    implement_it()

    sent = manual["merge_targets"]
    assert [t["path"] for t in sent] == ["Sources"]
    assert "THE TAIL SECTION" in sent[0]["text"], "the tail section was lost"
    assert manual["deletes"] == ["s-1"]
