"""`Get [Topic] - [Area]` — retrieval out of the Manuals.

WHY THIS FILE IS LARGE FOR A "WIRING" CHANGE
--------------------------------------------
pkm.py was written in full and then never imported: no route, no test, never
executed. Wiring it into the router is what makes every line of it reachable for
the first time, so the parts that decide WHAT the user gets back — index
building, the resolver, discovery mode — are covered here rather than trusted.

Two manual layouts exist in this codebase and the index builder has to handle
both, which is the subtlety worth pinning:

  • FLAT (implement.py Manuals): plain headings; a section's content is the
    sibling blocks that follow it, up to the next heading of any level.
  • TOGGLE (implement_diet.py Diet page): toggle headings; a section's content
    is its own non-heading children, and sub-headings are recursed into.

A section returns its OWN content, never its whole subtree — asking for
"Goals" must not dump every goal underneath it.
"""

import pytest

import pkm
from conftest import FakeUpdate, run

BRAIN_DB = "test-brain-id"      # BRAIN_ID in the fake environment
DIET_DB  = "test-diet-id"


# ─── A FAKE NOTION PAGE ────────────────────────────────────────────────────────

def heading(block_id, level, text, has_children=False):
    btype = f"heading_{level}"
    return {"id": block_id, "type": btype, "has_children": has_children,
            btype: {"rich_text": [{"plain_text": text}]}}


def leaf(block_id, text, btype="paragraph", has_children=False):
    return {"id": block_id, "type": btype, "has_children": has_children,
            btype: {"rich_text": [{"plain_text": text}]}}


# A Manual as implement.build_manual_blocks actually emits one: decorated plain
# headings, content as following siblings.
FLAT_MANUAL = {
    "manual-1": [
        {"id": "c-1", "type": "callout", "has_children": False,
         "callout": {"rich_text": [{"plain_text": "What this covers"}],
                     "icon": {"emoji": "📋"}}},
        {"id": "d-1", "type": "divider", "has_children": False, "divider": {}},
        heading("h-process", 2, "⚙️ Perfect Process"),
        leaf("n-1", "Prime the material", "numbered_list_item"),
        leaf("n-2", "Recall it cold", "numbered_list_item"),
        heading("h-improve", 2, "🚀 Improvements & Optimizations"),
        leaf("b-1", "Spaced repetition beats massed practice", "bulleted_list_item"),
        heading("h-break", 2, "📖 Step-by-Step Breakdown"),
        heading("h-recall", 3, "→ Active Recall"),
        leaf("p-1", "Purpose: force retrieval instead of review"),
    ],
}

# The Diet page: toggle headings, content nested as children.
TOGGLE_MANUAL = {
    "diet-1": [heading("h-goals", 1, "Goals", has_children=True)],
    "h-goals": [
        leaf("p-intro", "Pick one goal at a time", has_children=False),
        heading("h-fatloss", 2, "Fat Loss", has_children=True),
    ],
    "h-fatloss": [
        leaf("b-fat", "Deficit of 300-500 kcal", "bulleted_list_item"),
    ],
}


@pytest.fixture
def manual(monkeypatch):
    """Serve a fake Notion page. Defaults to the flat Manual in Brain."""
    state = {"tree": FLAT_MANUAL, "page": {"id": "manual-1"}, "err": None,
             "searched": []}

    def search_page_in_db(db_id, query, exact=False):
        state["searched"].append((db_id, query, exact))
        if state["page"] is None:
            return None, f"No page found matching '{query}'"
        return state["page"], None

    def get_children(block_id):
        if state["err"]:
            return [], state["err"]
        return list(state["tree"].get(block_id, [])), None

    monkeypatch.setattr(pkm, "search_page_in_db", search_page_in_db)
    monkeypatch.setattr(pkm, "get_children", get_children)
    return state


def ask(text):
    update = FakeUpdate(text=text)
    run(pkm.handle_get(update, text))
    return update


def replies(update):
    return "\n".join(update.message.reply_texts)


# ─── INDEX BUILDING: THE FLAT LAYOUT ───────────────────────────────────────────

def test_a_flat_section_owns_the_siblings_that_follow_it(manual):
    index, err = pkm.build_index("manual-1")

    assert err is None
    process = next(e for e in index if e["norm"] == "perfect process")
    assert [b["id"] for b in process["content"]] == ["n-1", "n-2"]


def test_a_flat_section_stops_at_the_next_heading(manual):
    """Content is bounded by the next heading of ANY level, so a section never
    swallows the one below it."""
    index, _ = pkm.build_index("manual-1")

    breakdown = next(e for e in index if e["norm"] == "step by step breakdown")
    assert breakdown["content"] == [], "swallowed the H3 that follows it"


def test_the_index_keeps_document_order_and_levels(manual):
    index, _ = pkm.build_index("manual-1")

    assert [(e["level"], e["norm"]) for e in index] == [
        (2, "perfect process"),
        (2, "improvements optimizations"),
        (2, "step by step breakdown"),
        (3, "active recall"),
    ]


def test_decorative_emoji_and_arrows_are_normalised_away(manual):
    """The Manuals bake '⚙️' and '→' into their headings, so a user typing the
    plain name has to match anyway."""
    index, _ = pkm.build_index("manual-1")

    assert {e["norm"] for e in index} >= {"perfect process", "active recall"}


def test_a_read_failure_is_reported_not_treated_as_an_empty_manual(manual):
    manual["err"] = "Notion 502: bad gateway"

    index, err = pkm.build_index("manual-1")

    assert index is None
    assert "502" in err


# ─── INDEX BUILDING: THE TOGGLE LAYOUT ─────────────────────────────────────────

def test_a_toggle_section_owns_only_its_non_heading_children(manual):
    """Asking for 'Goals' must return its own intro line, not every goal under it."""
    manual["tree"] = TOGGLE_MANUAL

    index, _ = pkm.build_index("diet-1")

    goals = next(e for e in index if e["norm"] == "goals")
    assert [b["id"] for b in goals["content"]] == ["p-intro"]


def test_toggle_subheadings_are_indexed_in_document_order(manual):
    manual["tree"] = TOGGLE_MANUAL

    index, _ = pkm.build_index("diet-1")

    assert [(e["level"], e["norm"]) for e in index] == [
        (1, "goals"), (2, "fat loss")]


# ─── RESOLUTION ────────────────────────────────────────────────────────────────

def test_an_exact_topic_returns_its_content(manual):
    update = ask("Get Perfect Process - Brain")

    assert update.message.replied_with("Prime the material")
    assert update.message.replied_with("Recall it cold")


def test_the_lookup_is_case_insensitive(manual):
    assert ask("get perfect process - Brain").message.replied_with("Prime the material")


def test_a_partial_topic_still_resolves(manual):
    """'recall' → 'Active Recall'. Typing the full decorated heading defeats the
    point of the command."""
    assert ask("Get recall - Brain").message.replied_with("force retrieval")


def test_several_matches_ask_which_one_instead_of_guessing(manual):
    """Returning an arbitrary one of two plausible sections is worse than asking:
    the user cannot tell they got the wrong section."""
    manual["tree"] = {"manual-1": [
        heading("h-1", 2, "Evidence Review"),
        leaf("p-1", "first body"),
        heading("h-2", 3, "Evidence Grading"),
        leaf("p-2", "second body"),
    ]}

    update = ask("Get Evidence - Brain")

    assert update.message.replied_with("Multiple matches")
    assert update.message.replied_with("Evidence Review")
    assert update.message.replied_with("Evidence Grading")
    assert not update.message.replied_with("first body")


def test_an_unmatched_topic_lists_what_is_available(manual):
    update = ask("Get Quantum Chromodynamics - Brain")

    assert update.message.replied_with("No topic matching")
    assert update.message.replied_with("Perfect Process"), "no topic tree to recover with"


def test_a_section_with_no_body_offers_its_subsections(manual):
    """'Step-by-Step Breakdown' holds only sub-headings. Answering '(empty)' when
    the content is one level down is a dead end."""
    update = ask("Get Step-by-Step Breakdown - Brain")

    assert update.message.replied_with("Active Recall")


# ─── DISCOVERY MODE ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("word", ["?", "list", "topics", "index", "all"])
def test_discovery_lists_every_topic(manual, word):
    update = ask(f"Get {word} - Brain")

    body = replies(update)
    for topic in ("Perfect Process", "Improvements", "Step-by-Step", "Active Recall"):
        assert topic in body, f"{word!r} did not list {topic}"


def test_discovery_indents_by_heading_depth(manual):
    """The tree is the map of the manual; flattening it loses which section a
    subsection belongs to."""
    update = ask("Get ? - Brain")

    lines = [ln for ln in replies(update).splitlines() if "Active Recall" in ln]
    assert lines and lines[0].startswith("   "), f"not indented: {lines}"


# ─── AREA + PAGE RESOLUTION ────────────────────────────────────────────────────

def test_the_area_resolves_through_the_env_convention(manual):
    """'Brain' → BRAIN_ID, the same mapping Implement uses."""
    ask("Get Perfect Process - Brain")

    assert manual["searched"][0][0] == BRAIN_DB


def test_diet_looks_for_the_diet_page_not_a_manual(manual):
    """The Diet area's page is titled 'Diet'; searching for 'Manual' finds nothing."""
    manual["tree"] = TOGGLE_MANUAL
    manual["page"] = {"id": "diet-1"}

    ask("Get Goals - Diet")

    assert manual["searched"][0] == (DIET_DB, "Diet", True)


def test_an_unconfigured_area_names_the_variable_to_set(manual):
    update = ask("Get Perfect Process - Astrophysics")

    assert update.message.replied_with("ASTROPHYSICS_ID")
    assert manual["searched"] == [], "hit Notion for an area that is not configured"


def test_a_missing_manual_page_is_reported(manual):
    manual["page"] = None

    update = ask("Get Perfect Process - Brain")

    assert update.message.replied_with("No *Manual* page found")


def test_an_empty_manual_says_so_rather_than_failing_a_lookup(manual):
    manual["tree"] = {"manual-1": []}

    update = ask("Get Perfect Process - Brain")

    assert update.message.replied_with("no sections yet")


# ─── NO LLM ON THIS PATH ───────────────────────────────────────────────────────

def test_retrieval_never_calls_claude():
    """Reading your own notes back should not cost Anthropic quota or 30 seconds.
    The resolver is difflib; asserted here because 'just ask Claude to find the
    section' is the obvious way for this to drift."""
    with open(pkm.__file__, encoding="utf-8") as fh:
        source = fh.read()

    assert "api.anthropic.com" not in source
    assert "ANTHROPIC_API_KEY" not in source
    assert not hasattr(pkm, "requests"), (
        "pkm imported requests — retrieval is meant to be a Notion-only path")
