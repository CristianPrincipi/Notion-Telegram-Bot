"""A failed lookup is not an empty one — the three answers of search_page_in_db.

THE BUG
-------
search_page_in_db answered "no such page" with (None, "No page found matching
'X'") — an error string, the same shape as a failed read. Two callers CREATE the
page when it is missing, the Diet page and an area's Manual, and "missing" had
to fall through to the create, so both wrote `page, _ = search_page_in_db(...)`
and threw the error away. A transient Notion failure therefore read as "there is
no Diet page" or "this area has no Manual yet", and David built a second one —
full skeleton and all — next to the real page. Every later run then picks one of
the two, so half of what gets merged lands on a page nobody reads. Nothing
errored, and the reply said "First run".

The same collapse ran the other way at the three callers that only report: both
Implement paths answered an outage with "Make sure you used Learn to save it and
the title matches", sending you to check a title that was fine, and Get said "No
Manual page found" while sending the real error underneath.

THE CONTRACT NOW
----------------
    (page, None)   found
    (None, None)   the read worked and nothing matches
    (None, error)  the read failed — a create-if-missing caller must REFUSE

Every test below drives the shipping handler with only its Notion I/O faked,
and fails against the old contract.
"""

import pytest

from conftest import FakeUpdate, run, with_update
from services import implement, implement_diet, pkm

OUTAGE = "Notion 502: bad gateway"
AREA_DB = "area-db-1"
SOURCE = {"id": "source-1", "properties": {"Name": {"type": "title", "title": [
    {"plain_text": "Memory Techniques"}]}}}
SOURCE_BLOCKS = [{"id": "b-1", "type": "paragraph", "has_children": False,
                  "paragraph": {"rich_text": [{"plain_text": "Spaced repetition works."}]}}]


def replies(update) -> str:
    return "\n".join(update.message.reply_texts)


# ─── THE DIET PAGE ─────────────────────────────────────────────────────────────

@pytest.fixture
def diet_db(monkeypatch):
    """find_or_create_diet_page against a fake Notion that records every create."""
    state = {"lookup": (None, None), "created": [], "skeletons": []}
    monkeypatch.setattr(implement_diet, "DIET_ID", "diet-db-1")
    monkeypatch.setattr(implement_diet, "search_page_in_db",
                        lambda db, title, exact=False: state["lookup"])

    def create_page(db_id, properties, children=None, icon=None):
        state["created"].append(db_id)
        return "new-diet-page", None

    monkeypatch.setattr(implement_diet, "create_page", create_page)
    monkeypatch.setattr(implement_diet, "_append_skeleton_deep",
                        lambda page_id: state["skeletons"].append(page_id))
    return state


def test_a_failed_diet_lookup_creates_nothing(diet_db):
    """THE bug. A second Diet page, built from a 502."""
    diet_db["lookup"] = (None, OUTAGE)

    page_id, was_created, err = implement_diet.find_or_create_diet_page()

    assert diet_db["created"] == [], "a failed lookup created a Diet page"
    assert diet_db["skeletons"] == []
    assert (page_id, was_created) == (None, False)
    assert err and "502" in err


def test_a_missing_diet_page_is_still_created(diet_db):
    """The path the error was being discarded FOR must keep working."""
    diet_db["lookup"] = (None, None)

    page_id, was_created, err = implement_diet.find_or_create_diet_page()

    assert (page_id, was_created, err) == ("new-diet-page", True, None)
    assert diet_db["created"] == ["diet-db-1"]
    assert diet_db["skeletons"] == ["new-diet-page"]


def test_an_existing_diet_page_is_found_not_created(diet_db):
    diet_db["lookup"] = ({"id": "diet-page-1"}, None)

    assert implement_diet.find_or_create_diet_page() == ("diet-page-1", False, None)
    assert diet_db["created"] == []


# ─── THE MANUAL ────────────────────────────────────────────────────────────────

@pytest.fixture
def area(monkeypatch):
    """The real run_implement up to the fork between a first run and a
    sectioned one. Both branches are stubbed and recorded: what matters here is
    which one a lookup result sends the run down, and whether it goes at all."""
    state = {"source": (SOURCE, None), "manual": ({"id": "manual-1"}, None),
             "first_runs": [], "sectioned_runs": [], "ticked": []}
    monkeypatch.setattr(implement, "get_area_db_id", lambda area_name: AREA_DB)
    monkeypatch.setattr(implement, "search_page_in_db",
                        lambda db, title, exact=False:
                        state["manual"] if db == AREA_DB else state["source"])
    monkeypatch.setattr(implement, "get_children", lambda page_id: (list(SOURCE_BLOCKS), None))

    async def first_run(*args, **kwargs):
        state["first_runs"].append(args)
        return True

    async def sectioned_run(*args, **kwargs):
        state["sectioned_runs"].append(args)
        return True

    monkeypatch.setattr(implement, "_first_run", first_run)
    monkeypatch.setattr(implement, "_sectioned_run", sectioned_run)
    monkeypatch.setattr(implement, "update_page",
                        lambda page_id, props: state["ticked"].append(page_id) or (True, None))
    return state


def implement_it():
    update = FakeUpdate(text="Implement Memory Techniques - Brain")
    run(implement.run_implement(update.message.text, **with_update(update)))
    return update


def test_a_failed_manual_lookup_builds_no_second_manual(area):
    """THE bug, Manual edition: _first_run builds a whole Manual from scratch."""
    area["manual"] = (None, OUTAGE)

    update = implement_it()

    assert area["first_runs"] == [], "a failed lookup started a first run"
    assert area["sectioned_runs"] == []
    assert area["ticked"] == [], "the source was marked Implemented with nothing merged"
    assert "502" in replies(update)
    assert "nothing was written" in replies(update).lower()


def test_a_missing_manual_is_still_a_first_run(area):
    area["manual"] = (None, None)

    implement_it()

    assert len(area["first_runs"]) == 1
    assert area["ticked"] == ["source-1"]


def test_an_existing_manual_is_a_sectioned_run(area):
    implement_it()

    assert len(area["sectioned_runs"]) == 1
    assert area["first_runs"] == []


# ─── THE LEARN SOURCE, IN BOTH IMPLEMENT PATHS ─────────────────────────────────
# Not a duplicate — a misdirection. An outage used to be answered with "check
# that you saved it and the title matches", which is advice about your data for
# a failure that was not.

def test_an_outage_finding_the_source_is_not_blamed_on_the_title(area):
    area["source"] = (None, OUTAGE)

    update = implement_it()

    assert "Make sure you used" not in replies(update)
    assert "502" in replies(update)
    assert area["first_runs"] == area["sectioned_runs"] == []


def test_a_source_that_is_not_there_still_says_so(area):
    area["source"] = (None, None)

    update = implement_it()

    assert update.message.replied_with("Could not find *Memory Techniques*")
    assert "Make sure you used" in replies(update)


@pytest.fixture
def diet_source(monkeypatch):
    """run_implement_diet as far as the source lookup; nothing past it may run."""
    state = {"source": ({"id": "summary-1", "properties": {}}, None), "prepared": []}
    monkeypatch.setattr(implement_diet, "DIET_ID", "diet-db-1")
    monkeypatch.setattr(implement_diet, "search_page_in_db",
                        lambda db, title, exact=False: state["source"])
    monkeypatch.setattr(implement_diet, "find_or_create_diet_page",
                        lambda: state["prepared"].append(True) or (None, False, "stop here"))
    monkeypatch.setattr(implement_diet, "get_children",
                        lambda page_id: (list(SOURCE_BLOCKS), None))
    return state


def implement_diet_it():
    update = FakeUpdate(text="Implement Creatine - Diet")
    run(implement_diet.run_implement_diet("Creatine", **with_update(update)))
    return update


def test_an_outage_finding_the_diet_source_is_not_blamed_on_the_title(diet_source):
    diet_source["source"] = (None, OUTAGE)

    update = implement_diet_it()

    assert "Make sure you used" not in replies(update)
    assert "502" in replies(update)
    assert diet_source["prepared"] == [], "went on to touch the Diet page"


def test_a_diet_source_that_is_not_there_still_says_so(diet_source):
    diet_source["source"] = (None, None)

    update = implement_diet_it()

    assert update.message.replied_with("Could not find *Creatine*")
    assert diet_source["prepared"] == []


# ─── GET ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def get_lookup(monkeypatch):
    state = {"lookup": (None, None)}
    monkeypatch.setattr(pkm, "get_area_db_id", lambda area_name: AREA_DB)
    monkeypatch.setattr(pkm, "search_page_in_db",
                        lambda db, title, exact=False: state["lookup"])
    return state


def get_it():
    update = FakeUpdate(text="Get Perfect Process - Brain")
    run(pkm.run_get(update.message.text, **with_update(update)))
    return update


def test_an_outage_finding_the_manual_is_not_reported_as_no_manual(get_lookup):
    """It used to say "No Manual page found" and send the 502 underneath —
    two contradicting messages, the first of which is the one you read."""
    get_lookup["lookup"] = (None, OUTAGE)

    update = get_it()

    assert "page found" not in replies(update)
    assert "502" in replies(update)


def test_a_manual_that_is_not_there_is_still_no_manual(get_lookup):
    get_lookup["lookup"] = (None, None)

    update = get_it()

    assert update.message.replied_with("No *Manual* page found")
