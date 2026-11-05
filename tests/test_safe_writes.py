"""Write ordering and concurrency for the Implement commands.

Notion has no transactions, so ORDERING is the only safety mechanism available.
The rule under test everywhere here: never delete existing content before the
replacement is committed.

The failure these prevent is the worst kind — a merge that ran, reported
success or a plain network error, and left the knowledge base empty or doubled,
with no way to roll back.
"""

import asyncio
import time

import pytest

from services import implement, implement_diet
import page_lock
from conftest import FakeUpdate, run, with_update, written_nothing


@pytest.fixture(autouse=True)
def fresh_locks():
    """Locks are module-level state; a leaked one would poison later tests."""
    page_lock._locks.clear()
    yield
    page_lock._locks.clear()


class NotionSpy:
    """Records the order of every Notion write."""

    def __init__(self, append_error=None, existing=None):
        self.append_error = append_error
        self.existing = existing if existing is not None else [
            {"id": "old-1", "type": "paragraph"},
            {"id": "old-2", "type": "bulleted_list_item"},
        ]
        self.calls = []          # ordered log of ("append"|"delete", detail)

    @property
    def deletes(self):
        return [d for kind, d in self.calls if kind == "delete"]

    @property
    def appends(self):
        return [d for kind, d in self.calls if kind == "append"]

    def get_children(self, block_id):
        return list(self.existing), None

    def append_children(self, block_id, blocks, after=None):
        self.calls.append(("append", block_id))
        if self.append_error:
            return written_nothing(), self.append_error
        return blocks, None

    def delete_block(self, block_id):
        self.calls.append(("delete", block_id))


# ─── 1. MANUAL: APPEND BEFORE DELETE ───────────────────────────────────────────

def heading(block_id, text, level=2):
    btype = f"heading_{level}"
    return {"id": block_id, "type": btype, btype: {"rich_text": [{"plain_text": text}]}}


@pytest.fixture
def manual_spy(monkeypatch):
    """A Manual with one section: a heading, then the two content blocks it owns.

    read_manual_sections indexes by heading, so a spy that returns bare leaf
    blocks would produce no sections and the handler would never reach a write.
    """
    spy = NotionSpy(existing=[
        heading("h-1", "⚙️ Perfect Process"),
        {"id": "old-1", "type": "numbered_list_item",
         "numbered_list_item": {"rich_text": [{"plain_text": "old step one"}]}},
        {"id": "old-2", "type": "numbered_list_item",
         "numbered_list_item": {"rich_text": [{"plain_text": "old step two"}]}},
    ])
    monkeypatch.setattr(implement, "get_children", spy.get_children)
    monkeypatch.setattr(implement, "delete_block", spy.delete_block)
    monkeypatch.setattr(implement, "append_children", spy.append_children)
    return spy


@pytest.fixture
def wired_manual(manual_spy, monkeypatch):
    """Drive the REAL handle_implement with only its I/O stubbed.

    Deliberately not a test that performs append-then-delete itself and asserts
    its own ordering — that proves the test agrees with itself, not that
    handle_implement is correct. Restoring the old clear-then-append order has to
    fail these.
    """
    monkeypatch.setattr(implement, "get_area_db_id", lambda area: "area-db-1")
    monkeypatch.setattr(
        implement, "search_page_in_db",
        lambda db, name, exact=False: (
            {"id": "manual-1" if db == "area-db-1" else "source-1", "properties": {}},
            None))
    monkeypatch.setattr(implement, "blocks_to_text", lambda blocks: "content")
    monkeypatch.setattr(implement, "route_sections",
                        lambda paths, text, title: (
                            {"affected": [{"path": "Perfect Process"}], "new_steps": []}, None))
    monkeypatch.setattr(implement, "merge_sections",
                        lambda targets, text, title, unverified=False: (
                            {"updates": [{"path": "Perfect Process", "ops": [
                                {"op": "replace", "line": 1, "text": "merged step", "why": ""},
                                {"op": "remove", "line": 2, "text": "", "why": ""}]}]},
                            None))
    # The Sources ledger is a pure append to a section these tests do not touch,
    # and it runs AFTER the section writes — so leaving it live would put a second
    # "append" at the end of every ordering assertion here without saying anything
    # about the ordering those assertions exist to protect.
    # tests/test_implement_sections.py covers the ledger, including that it never
    # deletes.
    monkeypatch.setattr(implement, "record_source",
                        lambda page_id, sections, title, unverified: (True, None))
    monkeypatch.setattr(implement, "update_page", lambda *a, **k: (True, None))
    return manual_spy


def run_implement():
    update_obj = FakeUpdate(text="Implement Memory Techniques - Brain")
    run(implement.run_implement(update_obj.message.text, **with_update(update_obj)))
    return update_obj


def test_manual_deletes_nothing_when_the_append_fails(wired_manual):
    """THE test. A failed append must leave the old content completely intact.

    Before this fix the order was clear-then-append, so a 502 or a Railway
    restart between the two left the Manual permanently empty, with no
    transaction to roll back and no second copy of the content anywhere.

    Now that writes are per-section, the report is per-section too: the failed
    section is listed as skipped AND explicitly called unchanged, which is the
    truthful claim — its append failed, so its delete never ran.
    """
    wired_manual.append_error = "Notion 502: bad gateway"

    update_obj = run_implement()

    assert wired_manual.appends, "never even attempted the append"
    assert wired_manual.deletes == [], "old Manual deleted despite a failed append"
    assert update_obj.message.replied_with("unchanged")
    assert update_obj.message.replied_with("502")


def test_manual_deletes_the_old_content_once_the_append_succeeds(wired_manual):
    run_implement()

    assert wired_manual.deletes == ["old-1", "old-2"]


def test_manual_appends_strictly_before_it_deletes(wired_manual):
    """Ordering, not just counts: the replacement must be committed first."""
    run_implement()

    kinds = [kind for kind, _ in wired_manual.calls]
    assert kinds[0] == "append", f"first write was {kinds[0]}, expected append"
    assert set(kinds[1:]) == {"delete"}


def test_clear_by_id_ignores_missing_ids(manual_spy):
    """A block without an id must not blow up the delete pass."""
    implement.clear_page_blocks_by_id(["a", None, "", "b"])

    assert manual_spy.deletes == ["a", "b"]


# ─── 2. DIET: EDITS, APPLIED ONCE ──────────────────────────────────────────────
# The Diet merge returns edit operations on the numbered lines read_section_contents
# produced; apply_updates writes them, append first. It used to append each
# section's FULL merged bullets and delete the old ones — and before that, in
# "merge" mode, to append them on top of the old ones, which doubled the section on
# every run. These drive the real read and the real writer against a spy.

def diet_leaf(block_id, text, btype="bulleted_list_item"):
    return {"id": block_id, "type": btype, btype: {"rich_text": [{"plain_text": text}]}}


@pytest.fixture
def diet_spy(monkeypatch):
    spy = NotionSpy(existing=[diet_leaf("old-1", "old line one", "paragraph"),
                              diet_leaf("old-2", "old line two")])
    monkeypatch.setattr(implement_diet, "get_children", spy.get_children)
    monkeypatch.setattr(implement_diet, "append_children", spy.append_children)
    monkeypatch.setattr(implement_diet, "delete_block", spy.delete_block)
    return spy


PATH = "Goals>Fat Loss>Strategies"
TARGETS = {PATH: "section-1"}


def diet_op(kind, line, text=""):
    return {"op": kind, "line": line, "text": text, "why": ""}


def apply_diet(*ops, path="Goals > Fat Loss > Strategies"):
    """read_section_contents, then apply_updates, as run_implement_diet does."""
    contents, err = implement_diet.read_section_contents(TARGETS)
    assert err is None
    return implement_diet.apply_updates([{"path": path, "ops": list(ops)}], TARGETS, contents)


REWRITE_BOTH = (diet_op("replace", 1, "new line one"), diet_op("replace", 2, "new line two"))


def test_diet_deletes_exactly_the_lines_it_replaced(diet_spy):
    edited, skipped, partial = apply_diet(*REWRITE_BOTH)

    assert [e.path for e in edited] == [PATH]
    assert (skipped, partial) == ([], [])
    assert diet_spy.deletes == ["old-1", "old-2"]


def test_diet_deletes_nothing_when_the_append_fails(diet_spy):
    diet_spy.append_error = "Notion 502: bad gateway"

    edited, skipped, partial = apply_diet(*REWRITE_BOTH)

    assert edited == []
    assert diet_spy.deletes == [], "old lines deleted despite a failed append"
    assert "502" in skipped[0]


def test_diet_appends_strictly_before_it_deletes(diet_spy):
    apply_diet(*REWRITE_BOTH)

    kinds = [kind for kind, _ in diet_spy.calls]
    assert kinds[:2] == ["append", "append"], f"the writes began {kinds}"
    assert set(kinds[2:]) == {"delete"}


def test_diet_writes_without_reading_the_section_again(diet_spy, monkeypatch):
    """The lines Claude's numbers refer to are the ones read before the merge. A
    second read at write time would be a second numbering, and a wasted request."""
    contents, _ = implement_diet.read_section_contents(TARGETS)
    reads = []
    monkeypatch.setattr(implement_diet, "get_children",
                        lambda block_id: reads.append(block_id) or ([], None))

    implement_diet.apply_updates([{"path": PATH, "ops": list(REWRITE_BOTH)}], TARGETS, contents)

    assert reads == []
    assert diet_spy.deletes == ["old-1", "old-2"]


def test_diet_never_deletes_nested_toggle_headings(diet_spy):
    """Headings are structure, not content — deleting them destroys the skeleton.
    They are never numbered, so no operation can name one."""
    diet_spy.existing = [diet_leaf("leaf-1", "one"), {"id": "toggle-1", "type": "heading_3"},
                         diet_leaf("leaf-2", "two", "paragraph")]

    apply_diet(diet_op("remove", 1), diet_op("remove", 2), diet_op("remove", 3))

    assert "toggle-1" not in diet_spy.deletes
    assert diet_spy.deletes == ["leaf-1", "leaf-2"]


def test_diet_running_twice_does_not_double_the_section(diet_spy):
    """The compounding regression, end to end, now for edits: each run reads what
    the last one left, and the section holds one copy of its lines, not two."""
    live = list(diet_spy.existing)
    counter = iter(range(1000))

    def get_children(block_id):
        return list(live), None

    def append_children(block_id, blocks, after=None):
        # Unique IDs per append: reusing them across runs would make the second
        # run's delete pass remove the blocks it had just written.
        made = [dict(b, id=f"new-{next(counter)}") for b in blocks]
        at = next(i for i, b in enumerate(live) if b["id"] == after) + 1 if after else len(live)
        live[at:at] = made
        return made, None

    def delete_block(block_id):
        live[:] = [b for b in live if b["id"] != block_id]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(implement_diet, "get_children", get_children)
        mp.setattr(implement_diet, "append_children", append_children)
        mp.setattr(implement_diet, "delete_block", delete_block)

        apply_diet(*REWRITE_BOTH)
        after_first = [b["id"] for b in live]
        apply_diet(*REWRITE_BOTH)

    assert len(after_first) == 2, f"first run left {after_first}, expected 2 lines"
    assert len(live) == 2, f"second run left {len(live)} blocks — content compounded"


def test_diet_leaves_unlisted_sections_alone(diet_spy):
    """Sections Claude was not shown must never be touched."""
    edited, skipped, partial = apply_diet(diet_op("add", 0, "x"),
                                          path="Goals > Nonexistent > Section")

    assert edited == []
    assert [kind for kind, _ in diet_spy.calls] == []
    assert skipped == ["Goals > Nonexistent > Section "
                       "(not one of the sections Claude was shown)"]


# ─── 3. PER-PAGE LOCKS ─────────────────────────────────────────────────────────

def test_lock_serialises_two_runs_against_the_same_page():
    order = []

    async def worker(tag, hold):
        async with page_lock.page_lock("page-a"):
            order.append(f"{tag}-start")
            await asyncio.sleep(hold)
            order.append(f"{tag}-end")

    async def main():
        await asyncio.gather(worker("first", 0.05), worker("second", 0))

    run(main())

    assert order == ["first-start", "first-end", "second-start", "second-end"]


def test_a_busy_page_is_refused_not_queued():
    """A merge takes tens of seconds — silently queueing would look like a hang.

    The second attempt is wrapped in wait_for so that a regression to queueing
    FAILS here rather than deadlocking: without it, a lock that waits forever
    hangs this test, and a hung CI run is far worse to diagnose than a red one.
    """
    async def second_attempt():
        async with page_lock.page_lock("page-a", timeout=0.01):
            pass

    async def main():
        async with page_lock.page_lock("page-a"):
            with pytest.raises(page_lock.PageBusy):
                await asyncio.wait_for(second_attempt(), timeout=2)

    run(main())


def test_different_pages_do_not_block_each_other():
    async def main():
        async with page_lock.page_lock("page-a"):
            async with page_lock.page_lock("page-b", timeout=0.01):
                return True

    assert run(main()) is True


def test_the_lock_is_released_after_an_error():
    """An exception mid-merge must not wedge the Manual until the next restart."""
    async def main():
        with pytest.raises(ValueError):
            async with page_lock.page_lock("page-a"):
                raise ValueError("merge exploded")
        # must be re-acquirable immediately
        async with page_lock.page_lock("page-a", timeout=0.01):
            return True

    assert run(main()) is True


def test_the_lock_is_released_after_a_successful_run():
    async def main():
        async with page_lock.page_lock("page-a"):
            pass
        async with page_lock.page_lock("page-a", timeout=0.01):
            return True

    assert run(main()) is True


# ─── 4. THE LOCK IS ACTUALLY WIRED IN ──────────────────────────────────────────
# The tests above prove the lock works. These prove it is used — a correct lock
# nobody acquires protects nothing.

def test_implement_refuses_a_concurrent_manual_update(monkeypatch):
    monkeypatch.setattr(implement, "get_area_db_id", lambda area: "area-db-1")
    # Step A reads the Learn SOURCE page, which sits outside the lock because it
    # is a different page. It has to be stubbed or the handler bails there —
    # and would make a real network call doing so.
    monkeypatch.setattr(implement, "search_page_in_db",
                        lambda db, name, exact=False: ({"id": "source-1",
                                                        "properties": {}}, None))
    monkeypatch.setattr(implement, "get_children", lambda pid: ([{"id": "b1"}], None))
    monkeypatch.setattr(implement, "blocks_to_text", lambda blocks: "source content")

    async def main():
        async with page_lock.page_lock("area-db-1"):
            update_obj = FakeUpdate(text="Implement Memory Techniques - Brain")
            # wait_for so a regression to queueing FAILS here rather
            # than deadlocking the suite.
            await asyncio.wait_for(implement.run_implement(update_obj.message.text, **with_update(update_obj)), timeout=5)
            return update_obj

    result = run(main())

    assert result.message.replied_with("already in progress")


def test_implement_diet_refuses_a_concurrent_update(monkeypatch):
    # Keyed on the Diet DATABASE, not the Diet page: the page id is not known
    # until find_or_create_diet_page returns, and that call is itself the
    # check-then-act the lock has to cover. See test_locks_are_keyed_on_databases.
    monkeypatch.setattr(implement_diet, "DIET_ID", "diet-db-1")
    monkeypatch.setattr(implement_diet, "search_page_in_db",
                        lambda db, name, exact=False: ({"id": "summary-1",
                                                        "properties": {}}, None))
    monkeypatch.setattr(implement_diet, "get_children",
                        lambda bid: ([{"type": "paragraph",
                                       "paragraph": {"rich_text": [
                                           {"plain_text": "some summary text"}]}}], None))
    monkeypatch.setattr(implement_diet, "find_or_create_diet_page",
                        lambda: ("diet-page-1", False, None))

    async def main():
        async with page_lock.page_lock("diet-db-1"):
            update_obj = FakeUpdate(text="Implement Protein Basics - Diet")
            # wait_for so a regression to queueing FAILS here rather
            # than deadlocking the suite.
            await asyncio.wait_for(implement_diet.run_implement_diet("Protein Basics", **with_update(update_obj)), timeout=5)
            return update_obj

    result = run(main())

    assert result.message.replied_with("already in progress")


def test_two_overlapping_diet_runs_create_only_one_diet_page(monkeypatch):
    """THE create race. Two first-runs must not build two Diet pages.

    find_or_create_diet_page is a check-then-act: search for "Diet", create it
    with a full skeleton if missing. It used to run OUTSIDE the lock, because the
    lock was keyed on the page id it returns — so two overlapping runs both
    searched an empty database, both found nothing, and both created one. Every
    later run then picks one of the two arbitrarily and half the knowledge base
    lands in a page nobody reads. Nothing errors.

    Concurrency is what makes this reachable, which is exactly why it had to be
    closed before turning concurrency on.
    """
    monkeypatch.setattr(implement_diet, "DIET_ID", "diet-db-1")
    created = []
    database = {}      # stands in for the Diet DB; populated by whoever creates first

    def find_or_create_diet_page():
        if "page" in database:
            return database["page"], False, None
        # The real call searches Notion, creates a page, then writes the whole
        # skeleton — a wide window between the check and the act. The sleep
        # reproduces it: an instant stub is effectively atomic, so no
        # interleaving is possible and the test would pass against the bug.
        # It runs inside asyncio.to_thread, so the loop stays free and the
        # second run really does get to reach this function.
        time.sleep(0.05)
        page_id = f"diet-page-{len(created)}"
        database["page"] = page_id
        created.append(page_id)
        return page_id, True, None

    monkeypatch.setattr(implement_diet, "find_or_create_diet_page", find_or_create_diet_page)
    monkeypatch.setattr(implement_diet, "search_page_in_db",
                        lambda db, name, exact=False: ({"id": "summary-1",
                                                        "properties": {}}, None))
    monkeypatch.setattr(implement_diet, "get_children",
                        lambda bid: ([{"type": "paragraph",
                                       "paragraph": {"rich_text": [
                                           {"plain_text": "some summary text"}]}}], None))
    monkeypatch.setattr(implement_diet, "read_diet_structure",
                        lambda pid: ({"Goals": {"Fat Loss": {}}},
                                     {"Goals>Fat Loss": "block-1"}, None))
    monkeypatch.setattr(implement_diet, "route_sections",
                        lambda paths, text, title: ({"affected": []}, None))
    monkeypatch.setattr(implement_diet, "update_page", lambda pid, props: (True, None))

    async def one_run():
        update_obj = FakeUpdate(text="Implement Protein Basics - Diet")
        await implement_diet.run_implement_diet("Protein Basics", **with_update(update_obj))
        return update_obj

    async def main():
        return await asyncio.gather(one_run(), one_run())

    run(main())

    assert created == ["diet-page-0"], (
        f"built {len(created)} Diet pages ({created}) — the create race is open")
