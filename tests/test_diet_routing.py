"""The Diet routing/merge split — and the sections it must never lose.

WHAT THIS LOCKS DOWN
--------------------
`decide_updates` used to send the whole page in one call. It now splits:

  ROUTE — the section paths, names only, plus the summary
  MERGE — the current content of ONLY the routed sections, plus the summary

That split introduces a failure mode the single call did not have: a section can
now fall out BETWEEN the two calls, and every way it can do so is silent. Routing
names a path that does not resolve; the content read drops one; the merge returns
fewer sections than it was given. In each case the page is written, the run
reports success, and a section you expected to be updated simply is not — which
looks exactly like the summary having nothing to say about it.

So these tests are about accounting, not about the model's judgement. Anthropic
is stubbed throughout; Notion is a fake block tree. What is asserted is that
every path routing names reaches the merge and the write, and that anything that
cannot is REPORTED.

The token saving is the point of the change, but the safety property is the one
worth a test: a cheaper run that quietly drops a third of the update is not
cheaper, it is broken.
"""

import pytest

from services import implement_diet
from conftest import FakeUpdate, run, with_update

# ─── A POPULATED DIET PAGE ─────────────────────────────────────────────────────
# Content carries a distinctive marker per section so a test can assert exactly
# which sections' text reached which prompt.


def heading(block_id, level, text, has_children=False):
    btype = f"heading_{level}"
    return {"id": block_id, "type": btype, "has_children": has_children,
            btype: {"rich_text": [{"plain_text": text}], "is_toggleable": True}}


def bullet(block_id, text):
    return {"id": block_id, "type": "bulleted_list_item", "has_children": False,
            "bulleted_list_item": {"rich_text": [{"plain_text": text}]}}


PAGE = {
    "page-1": [heading("h1-goals", 1, "Goals", True),
               heading("h1-season", 1, "Seasonality", True)],
    "h1-goals": [heading("h2-fat", 2, "Fat Loss", True),
                 heading("h2-muscle", 2, "Muscle Mass", True)],
    "h1-season": [heading("h2-fruit", 2, "Fruit", True)],
    "h2-fat": [heading("h3-fat-strat", 3, "Strategies", True),
               heading("h3-fat-eviq", 3, "Evidence", True),
               heading("h3-fat-foods", 3, "Foods", True)],
    "h2-muscle": [heading("h3-mus-strat", 3, "Strategies", True)],
    "h2-fruit": [bullet("b-fruit", "CONTENT-FRUIT berries in summer")],
    "h3-fat-strat": [bullet("b-1", "CONTENT-STRATEGIES eat at a deficit")],
    "h3-fat-eviq": [bullet("b-2", "CONTENT-EVIDENCE twelve trials, pooled")],
    "h3-fat-foods": [bullet("b-3", "CONTENT-FOODS lean protein first")],
    "h3-mus-strat": [bullet("b-4", "CONTENT-MUSCLE progressive overload")],
    # The Learn summary page itself.
    "summary-1": [bullet("s-1", "Creatine raises phosphocreatine stores.")],
}

ALL_CONTENT_MARKERS = ["CONTENT-FRUIT", "CONTENT-STRATEGIES", "CONTENT-EVIDENCE",
                       "CONTENT-FOODS", "CONTENT-MUSCLE"]

# The summary legitimately informs three sections across two different rows.
MULTI = ["Goals>Fat Loss>Strategies",
         "Goals>Fat Loss>Evidence",
         "Goals>Muscle Mass>Strategies"]


class Claude:
    """Stands in for anthropic_client.complete_json, recording both prompts.

    Dispatches on the schema rather than on call order, so a test that only makes
    one of the two calls still gets a sensible answer.
    """

    def __init__(self, affected=None, updates=None, conflicts=None):
        self.route_prompt = None
        self.merge_prompt = None
        self._affected = affected
        self._updates = updates
        self._conflicts = conflicts or []

    def __call__(self, system, user, schema=None, **kwargs):
        properties = (schema or {}).get("properties", {})

        if "affected" in properties:
            self.route_prompt = user
            paths = MULTI if self._affected is None else self._affected
            return {"affected": [{"path": p, "why": "informs it"} for p in paths]}, None

        self.merge_prompt = user
        if self._updates is not None:
            updates = self._updates
        else:
            # Default: the merge returns every section it was given.
            updates = [{"path": p, "mode": "merge", "bullets": [f"merged {p}"]}
                       for p in _sections_in(user)]
        return {"updates": updates, "conflicts": self._conflicts}, None


def _sections_in(merge_prompt: str) -> list:
    """The paths a merge prompt actually carried, read back out of it."""
    return [line.removeprefix("--- SECTION: ").removesuffix(" ---")
            for line in merge_prompt.splitlines()
            if line.startswith("--- SECTION: ")]


@pytest.fixture
def diet(monkeypatch):
    """The real split, against a fake Notion and a stubbed Claude."""
    monkeypatch.setattr(implement_diet, "DIET_ID", "diet-db-1")
    monkeypatch.setattr(implement_diet, "get_children",
                        lambda block_id: (list(PAGE.get(block_id, [])), None))
    monkeypatch.setattr(implement_diet, "search_page_in_db",
                        lambda db, name, exact=False: ({"id": "summary-1",
                                                        "properties": {}}, None))
    monkeypatch.setattr(implement_diet, "find_or_create_diet_page",
                        lambda: ("page-1", False, None))
    monkeypatch.setattr(implement_diet, "update_page", lambda pid, props: (True, None))
    return monkeypatch


@pytest.fixture
def writes(monkeypatch):
    """Captures what reached apply_updates — the actual write scope."""
    captured = []

    def apply_updates(updates, block_map):
        captured.extend(updates)
        return len(updates), []

    monkeypatch.setattr(implement_diet, "apply_updates", apply_updates)
    return captured


def drive(claude, monkeypatch):
    monkeypatch.setattr(implement_diet, "complete_json", claude)
    update = FakeUpdate(text="Implement Creatine - Diet")
    run(implement_diet.run_implement_diet("Creatine", **with_update(update)))
    return update


# ─── THE ROUTING CALL CARRIES NO CONTENT ───────────────────────────────────────

def test_the_routing_prompt_contains_paths_and_no_section_content(diet, writes):
    """THE WHOLE POINT OF THE SPLIT.

    If a single line of section content leaks into the routing payload, the call
    is no longer cheap and the change bought nothing — the cost scales with the
    knowledge base again. Asserted against the page's real content markers, so
    the test fails if the routing prompt starts carrying any of it.
    """
    claude = Claude()
    drive(claude, diet)

    assert claude.route_prompt is not None, "routing never ran"

    for marker in ALL_CONTENT_MARKERS:
        assert marker not in claude.route_prompt, (
            f"section content ({marker}) leaked into the routing prompt")

    # It must still carry the taxonomy, or routing has nothing to choose from.
    for path in ["Goals>Fat Loss>Strategies", "Goals>Fat Loss>Evidence",
                 "Goals>Fat Loss>Foods", "Goals>Muscle Mass>Strategies",
                 "Seasonality>Fruit"]:
        assert path in claude.route_prompt, f"{path} missing from the routing prompt"


def test_the_routing_prompt_offers_no_container_paths(diet, writes):
    """A category or a row that holds attributes cannot take content, so naming
    one is a route that can only ever be skipped."""
    claude = Claude()
    drive(claude, diet)

    lines = claude.route_prompt.splitlines()
    assert "- Goals" not in lines
    assert "- Goals>Fat Loss" not in lines
    assert "- Goals>Fat Loss>Strategies" in lines


# ─── NOTHING IS DROPPED BETWEEN THE TWO CALLS ──────────────────────────────────

def test_every_routed_section_survives_into_the_merge_and_the_write(diet, writes):
    """THE LOAD-BEARING TEST.

    A summary that legitimately touches three sections across two different rows.
    All three must reach the merge prompt WITH their current content, and all
    three must reach the write. A split that quietly delivers two of them reports
    success either way, which is the failure this asserts against.
    """
    claude = Claude(affected=MULTI)
    update = drive(claude, diet)

    assert _sections_in(claude.merge_prompt) == MULTI, (
        f"routing named {MULTI}, the merge was given {_sections_in(claude.merge_prompt)}")

    # Each one arrives with its OWN current content — a merge computed against
    # the wrong section's text is as bad as one computed against nothing.
    assert "CONTENT-STRATEGIES" in claude.merge_prompt
    assert "CONTENT-EVIDENCE" in claude.merge_prompt
    assert "CONTENT-MUSCLE" in claude.merge_prompt

    assert [u["path"] for u in writes] == MULTI, (
        f"routed {MULTI} but wrote {[u['path'] for u in writes]}")
    # "*3*" — the count is bold in the Markdown David actually sends.
    assert update.message.replied_with("*3* of 3 routed section(s) modified")


def test_sections_that_were_not_routed_are_never_fetched_or_sent(diet, writes):
    """The other half: untouched sections stay untouched BECAUSE they never go.

    Same guarantee as implement.py's — the only thing that keeps a section
    byte-identical is that the model never saw it.
    """
    claude = Claude(affected=["Goals>Fat Loss>Strategies"])
    drive(claude, diet)

    assert "CONTENT-FOODS" not in claude.merge_prompt
    assert "CONTENT-FRUIT" not in claude.merge_prompt
    assert [u["path"] for u in writes] == ["Goals>Fat Loss>Strategies"]


def test_a_routed_path_that_does_not_exist_is_reported_not_dropped(diet, writes):
    """Claude copies paths back from a prompt and can invent one.

    An invented path resolves to no block, so it can never be written. Saying so
    is the difference between "I could not do this" and a success message that
    quietly covers three sections when you asked about four.
    """
    claude = Claude(affected=["Goals>Fat Loss>Strategies", "Goals>Imaginary>Section"])
    update = drive(claude, diet)

    assert update.message.replied_with("Goals>Imaginary>Section")
    assert update.message.replied_with("Named but not found on the page")
    # The real one still goes through — one bad path does not sink the run.
    assert [u["path"] for u in writes] == ["Goals>Fat Loss>Strategies"]


def test_a_routed_section_the_merge_declines_is_reported_not_dropped(diet, writes):
    """Routing said three sections; the merge returned two.

    That can be legitimate — with the content in front of it the model may decide
    a section needs nothing. But it is invisible unless it is stated, and
    "invisible" is how a genuinely missed section looks too.
    """
    claude = Claude(
        affected=MULTI,
        updates=[{"path": MULTI[0], "mode": "merge", "bullets": ["merged"]},
                 {"path": MULTI[1], "mode": "merge", "bullets": ["merged"]}],
    )
    update = drive(claude, diet)

    assert update.message.replied_with("Routed but left unchanged by the merge (1)")
    assert update.message.replied_with(MULTI[2])


def test_a_failed_content_read_writes_nothing_and_says_so(diet, writes, monkeypatch):
    """A section that failed to read looks empty, and an empty section is what
    makes Claude populate it — so the merge would be computed against nothing and
    the write would replace real content with it. Refuse the whole run instead."""
    monkeypatch.setattr(implement_diet, "read_section_contents",
                        lambda sections: ({}, "Notion 502: bad gateway"))
    claude = Claude(affected=MULTI)

    update = drive(claude, diet)

    assert update.message.replied_with("502")
    assert update.message.replied_with("Nothing was written")
    assert writes == [], "wrote despite failing to read the sections first"


# ─── THE SKIPPED REPORT ────────────────────────────────────────────────────────

def test_a_long_skipped_list_reports_its_total(diet, monkeypatch):
    """It used to be `skipped[:8]` with no count, so past the eighth entry the
    rest were invisible — content dropped from the write with nothing saying so.
    """
    many = [f"Goals>Fat Loss>Section {i}" for i in range(12)]
    monkeypatch.setattr(implement_diet, "apply_updates",
                        lambda updates, block_map: (0, many))
    claude = Claude(affected=["Goals>Fat Loss>Strategies"])

    update = drive(claude, diet)

    assert update.message.replied_with("(12)"), "the total is missing"
    assert update.message.replied_with("and 4 more"), "the remainder is unaccounted for"


def test_nothing_routed_reports_it_and_still_ticks_implemented(diet, writes):
    """A summary that maps to nothing is a real outcome, not a failure — but the
    source page is still marked processed so the Learn nudge stops surfacing it.
    """
    claude = Claude(affected=[])
    update = drive(claude, diet)

    assert update.message.replied_with("doesn't map to anything")
    assert claude.merge_prompt is None, "ran the expensive merge with nothing to merge"
    assert writes == []
