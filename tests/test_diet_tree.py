"""read_diet_structure / read_section_contents — the shape, and which reads run.

Notion has no recursive block read: children come one request per parent. The
Diet page is a three-level toggle tree, so reading ALL of it is ~67 requests on a
populated skeleton (1 page + 4 H1 + 17 H2 + 45 H3).

It no longer reads all of it. Routing needs only the section PATHS, which levels
1-3 produce, so the ~45 level-4 requests that fetch H3 leaf content now run after
routing, for the handful of sections that turned out to be affected.

The tests are split deliberately:

  - SHAPE tests pin the tree and block_map exactly. Every node is a dict of its
    children and a leaf is `{}` — one shape at every level, where the tree used
    to return a `str` for a leaf H2 and a `dict` for an H2 holding H3s.
  - FETCHING tests assert which requests are issued, and that the level-4 reads
    are no longer among them.
  - CONTENT tests cover read_section_contents, which is where those level-4
    reads went.

block_map matters as much as the tree: both the content read and apply_updates
resolve paths through it, so a missing or wrong entry silently reads or writes
the wrong section.
"""

import threading
import time

import pytest

from services import implement_diet


# ─── A FAKE NOTION BLOCK TREE ──────────────────────────────────────────────────

def heading(block_id, level, text, has_children=False):
    btype = f"heading_{level}"
    return {"id": block_id, "type": btype, "has_children": has_children,
            btype: {"rich_text": [{"plain_text": text}], "is_toggleable": True}}


def leaf(block_id, text, btype="bulleted_list_item"):
    return {"id": block_id, "type": btype, "has_children": False,
            btype: {"rich_text": [{"plain_text": text}]}}


# page
# ├── H1 Goals
# │   ├── H2 Fat Loss           (holds H3 toggles)
# │   │   ├── H3 Strategies     → two bullets
# │   │   └── H3 Evidence       → empty
# │   └── H2 Muscle Mass        → empty, no children at all
# ├── H1 Seasonality
# │   └── H2 Fruit              → leaf content directly, no H3
# └── a stray paragraph         → ignored, not a heading
TREE = {
    "page-1": [heading("h1-goals", 1, "Goals", True),
               heading("h1-season", 1, "Seasonality", True),
               leaf("stray", "not a heading", "paragraph")],
    "h1-goals": [heading("h2-fatloss", 2, "Fat Loss", True),
                 heading("h2-muscle", 2, "Muscle Mass", False)],
    "h1-season": [heading("h2-fruit", 2, "Fruit", True)],
    "h2-fatloss": [heading("h3-strategies", 3, "Strategies", True),
                   heading("h3-evidence", 3, "Evidence", False)],
    "h2-fruit": [leaf("p-fruit", "berries in summer", "paragraph")],
    "h3-strategies": [leaf("b-1", "eat at a deficit"), leaf("b-2", "keep protein high")],
}

# One shape at every level: a node is a dict of its children, a leaf is {}.
# "Fat Loss" holds H3 toggles; "Muscle Mass" and "Fruit" are leaf rows. Nothing
# here is a string, and no content appears at all — that is read_section_contents.
EXPECTED_TREE = {
    "Goals": {
        "Fat Loss": {"Strategies": {}, "Evidence": {}},
        "Muscle Mass": {},
    },
    "Seasonality": {"Fruit": {}},
}

EXPECTED_BLOCK_MAP = {
    "Goals":                      "h1-goals",
    "Goals>Fat Loss":             "h2-fatloss",
    "Goals>Fat Loss>Strategies":  "h3-strategies",
    "Goals>Fat Loss>Evidence":    "h3-evidence",
    "Goals>Muscle Mass":          "h2-muscle",
    "Seasonality":                "h1-season",
    "Seasonality>Fruit":          "h2-fruit",
}


class FakeNotion:
    """Serves TREE, recording every read and how many overlapped."""

    def __init__(self, tree=None, errors=None, delay=0.0):
        self.tree = TREE if tree is None else tree
        self.errors = errors or {}        # block_id -> error string
        self.delay = delay
        self.reads = []                   # every block_id asked for
        self._lock = threading.Lock()
        self._inside = 0
        self.peak_concurrent = 0

    def get_children(self, block_id):
        with self._lock:
            self.reads.append(block_id)
            self._inside += 1
            self.peak_concurrent = max(self.peak_concurrent, self._inside)
        try:
            if self.delay:
                time.sleep(self.delay)
            if block_id in self.errors:
                return [], self.errors[block_id]
            return list(self.tree.get(block_id, [])), None
        finally:
            with self._lock:
                self._inside -= 1


@pytest.fixture
def notion(monkeypatch):
    fake = FakeNotion()
    monkeypatch.setattr(implement_diet, "get_children", fake.get_children)
    return fake


# ─── SHAPE ─────────────────────────────────────────────────────────────────────

def test_the_tree_matches_the_page(notion):
    tree, _, err = implement_diet.read_diet_structure("page-1")

    assert err is None
    assert tree == EXPECTED_TREE


def test_the_block_map_locates_every_section(notion):
    """apply_updates resolves Claude's paths through this — a wrong entry sends
    an update to the wrong section, a missing one drops it."""
    _, block_map, err = implement_diet.read_diet_structure("page-1")

    assert err is None
    assert block_map == EXPECTED_BLOCK_MAP


def test_a_section_with_no_children_is_present_not_missing(notion):
    """A section with nothing in it still has to appear, or routing never sees
    that it exists and it can never be populated."""
    tree, block_map, _ = implement_diet.read_diet_structure("page-1")

    assert tree["Goals"]["Muscle Mass"] == {}
    assert tree["Goals"]["Fat Loss"]["Evidence"] == {}
    assert "Goals>Muscle Mass" in block_map


def test_non_heading_blocks_at_the_top_level_are_ignored(notion):
    tree, block_map = implement_diet.read_diet_structure("page-1")[:2]

    assert "not a heading" not in tree
    assert "stray" not in block_map.values()


def test_an_empty_page_reads_as_an_empty_tree(monkeypatch):
    fake = FakeNotion(tree={"page-1": []})
    monkeypatch.setattr(implement_diet, "get_children", fake.get_children)

    tree, block_map, err = implement_diet.read_diet_structure("page-1")

    assert (tree, block_map, err) == ({}, {}, None)


# ─── WHICH READS ARE ISSUED ────────────────────────────────────────────────────

def test_childless_blocks_are_never_read(notion):
    """has_children is already on the parent's payload, so asking for the
    children of a block that has none is a wasted round trip."""
    implement_diet.read_diet_structure("page-1")

    assert "h2-muscle" not in notion.reads
    assert "h3-evidence" not in notion.reads


def test_every_block_is_read_at_most_once(notion):
    implement_diet.read_diet_structure("page-1")

    assert len(notion.reads) == len(set(notion.reads)), (
        f"a block was fetched more than once: {notion.reads}")


def test_exactly_the_necessary_blocks_are_read(notion):
    """Locks the request count. Reading the tree concurrently is only a win if
    it is still the same set of requests, issued in parallel."""
    implement_diet.read_diet_structure("page-1")

    assert set(notion.reads) == {
        "page-1", "h1-goals", "h1-season", "h2-fatloss", "h2-fruit"}


def test_leaf_content_is_not_read_up_front(notion):
    """THE SPLIT. h3-strategies holds this page's only H3 content, and reading it
    is a level-4 request. Routing needs paths, not content, so that request is
    deferred until a section is known to be affected — on the real page that is
    ~45 requests that no longer run on every Implement."""
    implement_diet.read_diet_structure("page-1")

    assert "h3-strategies" not in notion.reads


# ─── CONCURRENCY ───────────────────────────────────────────────────────────────

def wide_tree(n):
    """A page with n H1s, each holding one childless H2 — so level 2 is n reads."""
    tree = {"page-1": [heading(f"h1-{i}", 1, f"Cat {i}", True) for i in range(n)]}
    for i in range(n):
        tree[f"h1-{i}"] = [heading(f"h2-{i}", 2, f"Row {i}", False)]
    return tree


def test_a_level_is_fetched_concurrently_but_stays_within_the_cap(monkeypatch):
    """The fix. Siblings at a level are independent, so they go out together.

    Capped because Notion rate-limits an integration to roughly three requests
    per second: a wider pool mostly earns 429s and the retry backoff, which is
    slower overall than not parallelising at all.
    """
    fake = FakeNotion(tree=wide_tree(12), delay=0.03)
    monkeypatch.setattr(implement_diet, "get_children", fake.get_children)

    implement_diet.read_diet_structure("page-1")

    assert fake.peak_concurrent > 1, "a level is still read one request at a time"
    assert fake.peak_concurrent <= implement_diet._TREE_FETCH_WORKERS, (
        f"{fake.peak_concurrent} requests in flight, cap is "
        f"{implement_diet._TREE_FETCH_WORKERS}")


def test_a_wide_tree_still_reads_correctly(monkeypatch):
    """Concurrency must not scramble which children belong to which parent."""
    fake = FakeNotion(tree=wide_tree(12))
    monkeypatch.setattr(implement_diet, "get_children", fake.get_children)

    tree, block_map, err = implement_diet.read_diet_structure("page-1")

    assert err is None
    assert tree == {f"Cat {i}": {f"Row {i}": {}} for i in range(12)}
    assert block_map["Cat 7>Row 7"] == "h2-7"


# ─── A FAILED READ IS NOT AN EMPTY SECTION ─────────────────────────────────────

@pytest.mark.parametrize("failing_block, level", [
    ("h1-goals", "H1 children"),
    ("h2-fatloss", "H2 children"),
])
def test_a_failed_read_below_the_top_level_fails_the_whole_tree(monkeypatch,
                                                                failing_block, level):
    """BEHAVIOUR CHANGE, and the reason the rewrite is worth more than speed.

    The depth-first version discarded errors below the top level
    (`h2_blocks, _ = get_children(...)`), so a transient 502 reading one section
    made that section look EMPTY rather than unreadable. An empty section is
    exactly what makes Claude decide to populate it, and apply_updates would then
    replace the real content with content merged against nothing. A dropped
    read silently became a content overwrite.

    Failing the read is the safe direction: handle_implement_diet reports it and
    writes nothing at all.
    """
    fake = FakeNotion(errors={failing_block: "Notion 502: bad gateway"})
    monkeypatch.setattr(implement_diet, "get_children", fake.get_children)

    tree, block_map, err = implement_diet.read_diet_structure("page-1")

    assert err is not None and "502" in err, f"a failed read of {level} was swallowed"
    assert (tree, block_map) == ({}, {}), "returned a partial tree alongside an error"


def test_a_failed_page_read_still_fails(notion, monkeypatch):
    """The top-level error path was already correct — keep it that way."""
    fake = FakeNotion(errors={"page-1": "Notion 404: not found"})
    monkeypatch.setattr(implement_diet, "get_children", fake.get_children)

    tree, block_map, err = implement_diet.read_diet_structure("page-1")

    assert (tree, block_map) == ({}, {})
    assert "404" in err


# ─── THE TAXONOMY OFFERED TO ROUTING ───────────────────────────────────────────

def test_content_paths_are_the_ones_that_can_hold_content(notion):
    """Routing is offered leaf H2 rows and H3 attributes — never a container.

    An H1 category and an H2 that holds H3 toggles are structure: the blueprint
    puts content under them, not in them. Offering one to the router only creates
    a way for it to name a section nothing can be written to.
    """
    tree, _, _ = implement_diet.read_diet_structure("page-1")

    assert implement_diet.content_paths(tree) == [
        "Goals>Fat Loss>Strategies",
        "Goals>Fat Loss>Evidence",
        "Goals>Muscle Mass",
        "Seasonality>Fruit",
    ]


def test_content_paths_excludes_categories_and_parent_rows(notion):
    tree, _, _ = implement_diet.read_diet_structure("page-1")
    paths = implement_diet.content_paths(tree)

    assert "Goals" not in paths, "an H1 category is not a content section"
    assert "Goals>Fat Loss" not in paths, "an H2 holding H3 toggles is not one either"


# ─── READING CONTENT FOR THE ROUTED SECTIONS ───────────────────────────────────

def test_content_is_read_for_exactly_the_sections_asked_for(notion):
    contents, err = implement_diet.read_section_contents(
        {"Goals>Fat Loss>Strategies": "h3-strategies"})

    assert err is None
    assert contents == {"Goals>Fat Loss>Strategies": "eat at a deficit\nkeep protein high"}
    assert notion.reads == ["h3-strategies"], "read something it was not asked for"


def test_a_leaf_row_reads_its_content_directly(notion):
    """An H2 that holds content rather than H3 toggles resolves the same way."""
    contents, err = implement_diet.read_section_contents({"Seasonality>Fruit": "h2-fruit"})

    assert (contents, err) == ({"Seasonality>Fruit": "berries in summer"}, None)


def test_an_empty_section_reads_as_empty_not_as_an_error(notion):
    contents, err = implement_diet.read_section_contents({"Goals>Muscle Mass": "h2-muscle"})

    assert (contents, err) == ({"Goals>Muscle Mass": ""}, None)


def test_nested_headings_are_not_read_as_content(notion):
    """apply_updates replaces a section's leaf blocks and leaves nested headings
    alone, so the content shown to the model has to be filtered the same way —
    otherwise the merge is computed over text a rewrite would never replace."""
    contents, err = implement_diet.read_section_contents({"Goals>Fat Loss": "h2-fatloss"})

    assert err is None
    assert contents == {"Goals>Fat Loss": ""}, "H3 toggle headings leaked in as content"


def test_a_failed_content_read_fails_the_whole_fetch(monkeypatch):
    """Same rule as the structure read, and the same reason.

    A section whose read failed is indistinguishable from an empty one once the
    error is dropped — and an empty section is what makes Claude decide to
    populate it, so the merge would be computed against nothing and the write
    would replace real content with it.
    """
    fake = FakeNotion(errors={"h3-strategies": "Notion 502: bad gateway"})
    monkeypatch.setattr(implement_diet, "get_children", fake.get_children)

    contents, err = implement_diet.read_section_contents({
        "Goals>Fat Loss>Strategies": "h3-strategies",
        "Seasonality>Fruit":         "h2-fruit",
    })

    assert err is not None and "502" in err
    assert contents == {}, "returned partial content alongside an error"


def test_reading_no_sections_is_not_an_error(notion):
    assert implement_diet.read_section_contents({}) == ({}, None)
    assert notion.reads == []
