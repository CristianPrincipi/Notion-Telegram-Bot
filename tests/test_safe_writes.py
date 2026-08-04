"""Write ordering and concurrency for the Implement commands.

Notion has no transactions, so ORDERING is the only safety mechanism available.
The rule under test everywhere here: never delete existing content before the
replacement is committed.

The failure these prevent is the worst kind — a merge that ran, reported
success or a plain network error, and left the knowledge base empty or doubled,
with no way to roll back.
"""

import asyncio

import pytest

import implement
import implement_diet
import page_lock
from conftest import FakeUpdate, run


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

    def append_children(self, block_id, blocks):
        self.calls.append(("append", block_id))
        if self.append_error:
            return [], self.append_error
        return blocks, None

    def delete_block(self, block_id):
        self.calls.append(("delete", block_id))


# ─── 1. MANUAL: APPEND BEFORE DELETE ───────────────────────────────────────────

@pytest.fixture
def manual_spy(monkeypatch):
    spy = NotionSpy()
    monkeypatch.setattr(implement, "get_all_blocks", spy.get_children)
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
    monkeypatch.setattr(implement, "merge_with_claude",
                        lambda **kw: ({"title": "Manual", "routine": [],
                                       "improvements": []}, None))
    monkeypatch.setattr(implement, "build_manual_blocks",
                        lambda merged, title: [{"new": "block"}])
    monkeypatch.setattr(implement, "update_page", lambda *a, **k: (True, None))
    return manual_spy


def run_implement():
    update_obj = FakeUpdate(text="Implement Memory Techniques - Brain")
    run(implement.handle_implement(update_obj, update_obj.message.text))
    return update_obj


def test_manual_deletes_nothing_when_the_append_fails(wired_manual):
    """THE test. A failed append must leave the old Manual completely intact.

    Before this fix the order was clear-then-append, so a 502 or a Railway
    restart between the two left the Manual permanently empty, with no
    transaction to roll back and no second copy of the content anywhere.
    """
    wired_manual.append_error = "Notion 502: bad gateway"

    update_obj = run_implement()

    assert wired_manual.appends, "never even attempted the append"
    assert wired_manual.deletes == [], "old Manual deleted despite a failed append"
    assert update_obj.message.replied_with("unchanged")


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


# ─── 2. DIET: MERGE REPLACES, IT DOES NOT COMPOUND ─────────────────────────────

@pytest.fixture
def diet_spy(monkeypatch):
    spy = NotionSpy()
    monkeypatch.setattr(implement_diet, "get_children", spy.get_children)
    monkeypatch.setattr(implement_diet, "append_children", spy.append_children)
    monkeypatch.setattr(implement_diet, "delete_block", spy.delete_block)
    return spy


BLOCK_MAP = {"Goals>Fat Loss>Strategies": "section-1"}


def update(mode="merge", bullets=("new bullet",)):
    return [{"path": "Goals > Fat Loss > Strategies", "mode": mode,
             "bullets": list(bullets)}]


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_both_modes_clear_the_old_bullets(diet_spy, mode):
    """_DIET_SYSTEM makes Claude return the FULL merged content in BOTH modes.

    "merge" meant Claude merged against the existing bullets itself — not that
    Notion should append to them. Treating it as append-only wrote the merged
    superset on top of what was already there, so every run duplicated the
    section and the bloat was fed back into the next prompt.
    """
    applied, skipped = implement_diet.apply_updates(update(mode=mode), BLOCK_MAP)

    assert applied == 1
    assert skipped == []
    assert diet_spy.deletes == ["old-1", "old-2"], (
        f"mode {mode!r} left the old bullets in place — content will compound")


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_diet_deletes_nothing_when_the_append_fails(diet_spy, mode):
    diet_spy.append_error = "Notion 502: bad gateway"

    applied, skipped = implement_diet.apply_updates(update(mode=mode), BLOCK_MAP)

    assert applied == 0
    assert diet_spy.deletes == [], "old bullets deleted despite a failed append"
    assert "502" in skipped[0]


@pytest.mark.parametrize("mode", ["merge", "replace"])
def test_diet_appends_strictly_before_it_deletes(diet_spy, mode):
    implement_diet.apply_updates(update(mode=mode), BLOCK_MAP)

    kinds = [kind for kind, _ in diet_spy.calls]
    assert kinds[0] == "append", f"first write was {kinds[0]}, expected append"


def test_diet_never_deletes_nested_toggle_headings(diet_spy):
    """Headings are structure, not content — deleting them destroys the skeleton."""
    diet_spy.existing = [
        {"id": "leaf-1", "type": "bulleted_list_item"},
        {"id": "toggle-1", "type": "heading_3"},
        {"id": "leaf-2", "type": "paragraph"},
    ]

    implement_diet.apply_updates(update(), BLOCK_MAP)

    assert "toggle-1" not in diet_spy.deletes
    assert diet_spy.deletes == ["leaf-1", "leaf-2"]


def test_diet_running_twice_does_not_double_the_section(diet_spy):
    """The compounding regression, end to end.

    Second run starts from what the first left behind; the section must hold one
    copy of the bullets, not two.
    """
    live = list(diet_spy.existing)
    counter = iter(range(1000))

    def get_children(block_id):
        return list(live), None

    def append_children(block_id, blocks):
        # Unique IDs per append: reusing them across runs would make the second
        # run's delete pass remove the blocks it had just written.
        live.extend({"id": f"new-{next(counter)}", "type": "bulleted_list_item"}
                    for _ in blocks)
        return blocks, None

    def delete_block(block_id):
        live[:] = [b for b in live if b["id"] != block_id]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(implement_diet, "get_children", get_children)
        mp.setattr(implement_diet, "append_children", append_children)
        mp.setattr(implement_diet, "delete_block", delete_block)

        implement_diet.apply_updates(update(bullets=["a", "b"]), BLOCK_MAP)
        after_first = len(live)
        implement_diet.apply_updates(update(bullets=["a", "b"]), BLOCK_MAP)

    assert after_first == 2, f"first run left {after_first} blocks, expected 2"
    assert len(live) == 2, f"second run left {len(live)} blocks — content compounded"


def test_diet_skips_a_section_whose_read_fails(diet_spy, monkeypatch):
    monkeypatch.setattr(implement_diet, "get_children",
                        lambda block_id: ([], "Notion 404: not found"))

    applied, skipped = implement_diet.apply_updates(update(), BLOCK_MAP)

    assert applied == 0
    assert diet_spy.deletes == []
    assert "404" in skipped[0]


def test_diet_leaves_unlisted_sections_alone(diet_spy):
    """Sections Claude did not mention must never be touched."""
    applied, skipped = implement_diet.apply_updates(
        [{"path": "Goals > Nonexistent > Section", "mode": "merge",
          "bullets": ["x"]}], BLOCK_MAP)

    assert applied == 0
    assert diet_spy.calls == []
    assert skipped == ["Goals > Nonexistent > Section"]


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
    monkeypatch.setattr(implement, "get_all_blocks", lambda pid: ([{"id": "b1"}], None))
    monkeypatch.setattr(implement, "blocks_to_text", lambda blocks: "source content")

    async def main():
        async with page_lock.page_lock("area-db-1"):
            update_obj = FakeUpdate(text="Implement Memory Techniques - Brain")
            # wait_for so a regression to queueing FAILS here rather
            # than deadlocking the suite.
            await asyncio.wait_for(implement.handle_implement(update_obj, update_obj.message.text), timeout=5)
            return update_obj

    result = run(main())

    assert result.message.replied_with("already in progress")


def test_implement_diet_refuses_a_concurrent_update(monkeypatch):
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
        async with page_lock.page_lock("diet-page-1"):
            update_obj = FakeUpdate(text="Implement Protein Basics - Diet")
            # wait_for so a regression to queueing FAILS here rather
            # than deadlocking the suite.
            await asyncio.wait_for(implement_diet.handle_implement_diet(update_obj, "Protein Basics"), timeout=5)
            return update_obj

    result = run(main())

    assert result.message.replied_with("already in progress")
