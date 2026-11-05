"""The Diet merge edits lines; it never rewrites a section.

THE BUG THIS LOCKS DOWN
-----------------------
The Diet merge returned each routed section's FULL bullets, and `apply_updates`
appended them and deleted every leaf block the section had. So a bullet Claude
kept word for word was still deleted and written back as plain text: its bold
label went, and so did everything nested under it — which `get_children` (one
level) had never read, so Claude had never seen it. The flat Manual had the same
defect; `tests/test_implement_sections.py` §9 is its twin.

The real `run_implement_diet` and `apply_updates` are driven here, with Notion
and Claude faked.
"""

import logging

import pytest

from config import UNVERIFIED_MARKER
from services import implement_diet
from conftest import FakeUpdate, run, with_update, written_nothing, written_ok

PATH = "Goals>Fat Loss>Strategies"


def heading(block_id, level, text, has_children=True):
    btype = f"heading_{level}"
    return {"id": block_id, "type": btype, "has_children": has_children,
            btype: {"rich_text": [{"plain_text": text}], "is_toggleable": True}}


def bullet(block_id, text, has_children=False):
    return {"id": block_id, "type": "bulleted_list_item", "has_children": has_children,
            "bulleted_list_item": {"rich_text": [{"plain_text": text}]}}


LABELLED = {"id": "b-2", "type": "bulleted_list_item", "has_children": False,
            "bulleted_list_item": {"rich_text": [
                {"plain_text": "Protein", "annotations": {"bold": True}},
                {"plain_text": " — first at every meal"}]}}


def page():
    return {
        "page-1":       [heading("h1-goals", 1, "Goals")],
        "h1-goals":     [heading("h2-fat", 2, "Fat Loss")],
        "h2-fat":       [heading("h3-strat", 3, "Strategies"),
                         heading("h3-foods", 3, "Foods")],
        "h3-strat":     [bullet("b-1", "Eat at a deficit", has_children=True), LABELLED],
        "h3-foods":     [bullet("f-1", "Lean protein first")],
        "summary-1":    [bullet("s-1", "A summary about fat loss.")],
    }


@pytest.fixture
def diet(monkeypatch):
    """The real Diet run, against a fake Notion and a stubbed Claude."""
    state = {"page": page(), "appends": [], "deletes": [], "reads": [], "merge_prompt": None,
             "merge_answer": None, "summary": "A summary about fat loss."}

    def get_children(block_id):
        state["reads"].append(block_id)
        if block_id == "summary-1":
            return [bullet("s-1", state["summary"])], None
        return list(state["page"].get(block_id, [])), None

    def complete_json(system, user, schema=None, **kwargs):
        if "affected" in (schema or {}).get("properties", {}):
            return {"affected": [{"path": PATH, "why": "informs it"}]}, None
        state["merge_prompt"] = user
        return state["merge_answer"], None

    def append_children(block_id, blocks, after=None):
        state["appends"].append((block_id, after, blocks))
        return written_ok(len(blocks)), None

    monkeypatch.setattr(implement_diet, "DIET_ID", "diet-db-1")
    monkeypatch.setattr(implement_diet, "get_children", get_children)
    monkeypatch.setattr(implement_diet, "search_page_in_db",
                        lambda db, name, exact=False: ({"id": "summary-1",
                                                        "properties": {}}, None))
    monkeypatch.setattr(implement_diet, "find_or_create_diet_page",
                        lambda: ("page-1", False, None))
    monkeypatch.setattr(implement_diet, "update_page", lambda pid, props: (True, None))
    monkeypatch.setattr(implement_diet, "complete_json", complete_json)
    monkeypatch.setattr(implement_diet, "append_children", append_children)
    monkeypatch.setattr(implement_diet, "delete_block", state["deletes"].append)
    return state


def op(kind, line, text="", why=""):
    """One edit operation, shaped as the Diet _MERGE_SCHEMA asks Claude for it."""
    return {"op": kind, "line": line, "text": text, "why": why}


def edits(*ops, path=PATH):
    return {"updates": [{"path": path, "ops": list(ops)}]}


def done(update):
    return next(t for t in update.message.reply_texts if "Diet page updated" in t)


def implement_it():
    update = FakeUpdate(text="Implement Fat Loss - Diet")
    run(implement_diet.run_implement_diet("Fat Loss", **with_update(update)))
    return update


# ─── 1. EDITS, NOT REWRITES ────────────────────────────────────────────────────

def test_a_bullet_with_nested_lines_survives_a_merge_of_its_section(diet):
    """THE ONE THAT LOST CONTENT: the lines nested under b-1 were never read."""
    diet["merge_answer"] = edits(op("add", 0, "Walk after meals"))

    implement_it()

    assert "b-1" not in diet["deletes"], "a bullet with nested lines was deleted"
    assert diet["deletes"] == []


def test_a_bullet_the_merge_kept_keeps_its_formatting(diet):
    """A bold label is rich text; the old write-back rebuilt every bullet plain."""
    diet["merge_answer"] = edits(op("add", 1, "Walk after meals"))

    implement_it()

    assert "b-2" not in diet["deletes"], "an unchanged bullet was deleted and rewritten"


def test_a_section_claude_was_not_shown_is_never_written(diet):
    """Hard Rule 3 on the Diet page. Routing chose Strategies, so Foods was never
    read or sent; a merge that names it anyway must not replace its content with
    lines written against nothing."""
    # The add is what bites: a replace would also be refused for naming a line
    # that was never numbered, which would hide this check.
    diet["merge_answer"] = edits(op("replace", 1, "Whatever Claude made up"),
                                 op("add", 0, "More of what Claude made up"),
                                 path="Goals>Fat Loss>Foods")

    update = implement_it()

    assert "f-1" not in diet["deletes"], "a section that was never routed was rewritten"
    assert not any(block_id == "h3-foods" for block_id, _, _ in diet["appends"])
    assert update.message.replied_with("Goals>Fat Loss>Foods")


def test_claude_is_shown_numbered_lines_and_which_are_fixed(diet):
    diet["merge_answer"] = {"updates": []}

    implement_it()

    assert ("--- SECTION: Goals>Fat Loss>Strategies ---\n"
            "[1] (fixed) Eat at a deficit\n[2] Protein — first at every meal") in diet["merge_prompt"]


@pytest.mark.parametrize("edit", [op("replace", 1, "Eat less"), op("remove", 1)],
                         ids=["replace", "remove"])
def test_a_bullet_with_nested_lines_is_never_replaced_or_removed(diet, edit):
    diet["merge_answer"] = edits(edit)

    update = implement_it()

    assert diet["deletes"] == [] and diet["appends"] == []
    assert update.message.replied_with("unchanged")


def test_an_add_at_line_0_goes_to_the_end_of_the_section(diet):
    """A Diet section's lines are its toggle heading's CHILDREN, and Notion
    cannot insert above a block's first child: 0 means the end, said so in the
    prompt, and the write is an append with no anchor."""
    diet["merge_answer"] = edits(op("add", 0, "Walk after meals"), op("add", 2, "Sleep 8h"))

    implement_it()

    assert [(block_id, after) for block_id, after, _ in diet["appends"]] == [
        ("h3-strat", None), ("h3-strat", "b-2")]
    assert "0 = the end of the section" in str(implement_diet._MERGE_SCHEMA)
    assert "END of the section" in implement_diet._MERGE_SYSTEM


def test_a_replacement_keeps_the_bold_label(diet):
    diet["merge_answer"] = edits(op("replace", 2, "Protein — first at every meal, 30 g"))

    implement_it()

    (_, after, [block]), = diet["appends"]
    assert after == "b-2"
    assert block["bulleted_list_item"]["rich_text"][0] == {
        "type": "text", "text": {"content": "Protein"}, "annotations": {"bold": True}}
    assert diet["deletes"] == ["b-2"]


def test_each_section_is_read_once(diet):
    """The read the merge is numbered from is the read the writer uses."""
    diet["merge_answer"] = edits(op("replace", 2, "Protein first"))

    implement_it()

    assert diet["reads"].count("h3-strat") == 1


def test_nothing_is_deleted_until_every_insertion_has_landed(diet, monkeypatch):
    """Diet had no half-written bucket: one append per section could not fail
    half-way. With one append per anchor line, it can."""
    calls = []

    def append_children(block_id, blocks, after=None):
        calls.append(after)
        if len(calls) == 2:
            return written_nothing(), "Notion 502: bad gateway"
        return written_ok(len(blocks)), None

    monkeypatch.setattr(implement_diet, "append_children", append_children)
    diet["merge_answer"] = edits(op("add", 0, "Walk after meals"),
                                 op("replace", 2, "Protein first"))

    update = implement_it()

    assert diet["deletes"] == []
    assert update.message.replied_with("Half-written")
    assert update.message.replied_with("1 of 2 insertions")


def test_the_reply_quotes_what_was_replaced_or_removed_and_why(diet):
    diet["merge_answer"] = edits(op("replace", 2, "Protein first", "the summary says 30 g"),
                                 op("add", 0, "Walk after meals", "new"))

    update = implement_it()

    reply = done(update)
    assert "*1* of 1 routed section(s) edited" in reply
    assert "+1 added · ~1 sharpened" in reply
    assert "“Protein — first at every meal” — _the summary says 30 g_" in reply
    assert "Walk after meals" not in reply


def test_the_merge_answers_with_operations_and_no_conflicts():
    """Decision B (2026-11-05): each replace or remove carries its own why,
    quoted next to the line, so the separate `conflicts` list is gone."""
    schema = implement_diet._MERGE_SCHEMA
    item = schema["properties"]["updates"]["items"]

    assert set(schema["properties"]) == {"updates"}
    assert set(item["properties"]) == {"path", "ops"}
    assert item["properties"]["ops"]["items"]["properties"]["op"]["enum"] == [
        "add", "replace", "remove"]


# ─── 2. AN UNVERIFIED SUMMARY MAY ADD, NOT REWRITE ─────────────────────────────
# Decision A (2026-11-05): the Diet page gets the flat Manual's additions-only
# rule. It only WARNED before, and then let a recollection rewrite anything.

UNVERIFIED = f"⚠️ {UNVERIFIED_MARKER}. No source text was read."


@pytest.mark.parametrize("kind", ["replace", "remove"])
def test_an_unverified_edit_to_an_existing_line_is_held_back(diet, kind):
    diet["summary"] = UNVERIFIED
    diet["merge_answer"] = edits(op(kind, 2, "Protein last" if kind == "replace" else ""),
                                 op("add", 0, "Walk after meals"))

    update = implement_it()

    assert diet["deletes"] == [], "a recollection changed an existing line"
    assert [after for _, after, _ in diet["appends"]] == [None], "the addition was dropped"
    assert update.message.replied_with("Held back")
    assert update.message.replied_with("“Protein — first at every meal”")


def test_an_unverified_summary_is_told_to_add_only(diet):
    diet["summary"] = UNVERIFIED
    diet["merge_answer"] = {"updates": []}

    implement_it()

    assert "Use add only" in diet["merge_prompt"]


def test_a_verified_summary_may_rewrite(diet):
    """The mirror: the rule is about recollections only."""
    diet["merge_answer"] = edits(op("replace", 2, "Protein first"))

    update = implement_it()

    assert diet["deletes"] == ["b-2"]
    assert not update.message.replied_with("Held back")
    assert "Use add only" not in diet["merge_prompt"]


def test_a_held_back_edit_is_counted_in_the_log(diet, caplog):
    diet["summary"] = UNVERIFIED
    diet["merge_answer"] = edits(op("remove", 2))

    with caplog.at_level(logging.INFO, logger="services.implement_diet"):
        implement_it()

    assert any(f"{PATH}: held back 1 edit(s) from an unverified source" in r.getMessage()
               for r in caplog.records)
