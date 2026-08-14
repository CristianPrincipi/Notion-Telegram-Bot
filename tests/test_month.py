"""The monthly rollover — Notion mocked with `responses`, so these run offline.

WHAT THIS LOCKS DOWN
--------------------
`MONTH_ID` used to be a hand-edited Railway variable, and forgetting to update it
on the 1st did not fail: expenses kept being written into LAST month's page and
`B` kept answering with last month's total. Nothing in the logs said so.

services/month.py replaces it with a resolution rule, and a rule is only worth having if
it is idempotent — the job runs every night, the `Month` command can be sent at
any time, and an expense write can resolve the month too. So most of the file
asserts the same thing from different angles: running it again changes nothing
and never produces a second page for one month.

The tests that matter most are the ones where it must REFUSE to act: two pages
titled "August" with no year is a mistake only the owner can settle, and a
Notion outage must leave the current pointer alone rather than blank it.
"""

import inspect
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
import responses

from conftest import EXPENSES_ID, NOTION_BASE, FakeUpdate, run, with_update
from services import month as month_module
from services.month import (
    ADOPTED, CREATED, RENAMED, UNCHANGED,
    canonical_title, current_month_id, ensure_current_month_page,
    format_rollover, period_key,
)
from proactive.month_rollover import build_rollover_message

MONTHS_DB   = "months-db-id"
TITLE_PROP  = "Mese"          # deliberately not "Name": it is read from the schema
AUGUST      = datetime(2026, 8, 1, 0, 5)
JULY_PAGE   = "page-july-2026"
AUGUST_PAGE = "page-august-2026"

EXPENSES_SCHEMA_URL = f"{NOTION_BASE}/databases/{EXPENSES_ID}"
MONTHS_SCHEMA_URL   = f"{NOTION_BASE}/databases/{MONTHS_DB}"
MONTHS_QUERY_URL    = f"{NOTION_BASE}/databases/{MONTHS_DB}/query"
PAGES_URL           = f"{NOTION_BASE}/pages"


# ─── FIXTURES ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def fresh_month(monkeypatch):
    """Reset the module's cache and pin the clock to 1 August 2026.

    services/month.py deliberately keeps state across calls — that is what makes the
    common path a memory read rather than a Notion query — so every test starts
    from a known one. The cache is memory-only: there is no state file to
    redirect, which is why this fixture no longer needs tmp_path.
    """
    monkeypatch.setattr(month_module, "MONTHS_DB_ID", None)
    monkeypatch.setattr(month_module, "_schema", {})
    monkeypatch.setattr(month_module, "_state",
                        {"period": "2026-07", "page_id": JULY_PAGE, "title": "July 2026"})
    monkeypatch.setattr(month_module, "now_local", lambda: AUGUST)


def month_page(title, page_id=AUGUST_PAGE, created="2026-08-01T00:00:00.000Z"):
    """One row of a Notion query response for the month database."""
    return {
        "id": page_id,
        "created_time": created,
        "properties": {TITLE_PROP: {"type": "title", "title": [{"plain_text": title}]}},
    }


def notion_month_database(pages, title_type="title"):
    """Register the three reads a rollover makes: both schemas, then the rows."""
    responses.add(responses.GET, EXPENSES_SCHEMA_URL, status=200, json={
        "properties": {"Account": {"type": "relation",
                                   "relation": {"database_id": MONTHS_DB}}}})
    responses.add(responses.GET, MONTHS_SCHEMA_URL, status=200, json={
        "properties": {TITLE_PROP: {"type": title_type}}})
    responses.add(responses.POST, MONTHS_QUERY_URL, status=200,
                  json={"results": pages, "has_more": False, "next_cursor": None})


def sent_bodies(method, url):
    """The decoded JSON bodies of every request made to url."""
    out = []
    for call in responses.calls:
        if call.request.method == method and call.request.url.startswith(url):
            body = call.request.body
            out.append(json.loads(body.decode() if isinstance(body, bytes) else body))
    return out


def written_title(body):
    return body["properties"][TITLE_PROP]["title"][0]["text"]["content"]


# ─── NAMING ────────────────────────────────────────────────────────────────────

def test_the_title_format_is_month_then_year():
    assert canonical_title(datetime(2026, 8, 1)) == "August 2026"
    assert canonical_title(datetime(2026, 9, 30)) == "September 2026"
    assert canonical_title(datetime(2027, 1, 1)) == "January 2027"


def test_month_names_do_not_come_from_the_host_locale():
    """strftime('%B') renders 'agosto' on an Italian host and 'August' on an
    English one, so the page title — which is DATA in Notion, and the key the
    next run matches on — would depend on which machine David happens to run on.

    A convention test, like the weekday one in test_data_integrity.py: no runtime
    assertion can catch this on a CI runner that is already in the C locale.
    """
    code = [line for line in inspect.getsource(month_module).splitlines()
            if not line.strip().startswith("#")]

    assert len(month_module.MONTH_NAMES) == 12
    assert [line for line in code if "%B" in line] == []


def test_periods_compare_as_plain_strings():
    """'2026-08' > '2026-07' is what tells a stale pointer from a current one, so
    the padding matters: '2026-9' would sort BEFORE '2026-10'."""
    assert period_key(datetime(2026, 8, 1)) == "2026-08"
    assert period_key(datetime(2026, 9, 1)) < period_key(datetime(2026, 10, 1))
    assert period_key(datetime(2026, 12, 1)) < period_key(datetime(2027, 1, 1))


# ─── CREATE ────────────────────────────────────────────────────────────────────

@responses.activate
def test_a_missing_month_page_is_created():
    notion_month_database([month_page("July 2026", page_id=JULY_PAGE)])
    responses.add(responses.POST, PAGES_URL, status=200, json={"id": AUGUST_PAGE})

    result = ensure_current_month_page()

    assert result.action == CREATED
    assert result.page_id == AUGUST_PAGE
    assert result.title == "August 2026"
    assert result.error is None
    assert written_title(sent_bodies("POST", PAGES_URL)[0]) == "August 2026"


@responses.activate
def test_the_new_page_is_created_in_the_database_the_relation_points_at():
    """MONTHS_DB_ID is optional because the Expenses `Account` relation already
    names the target — and unlike a second Railway variable, it cannot drift from
    the database the expense writes actually relate to."""
    notion_month_database([])
    responses.add(responses.POST, PAGES_URL, status=200, json={"id": AUGUST_PAGE})

    ensure_current_month_page()

    assert sent_bodies("POST", PAGES_URL)[0]["parent"] == {"database_id": MONTHS_DB}


@responses.activate
def test_the_created_page_becomes_the_month_expenses_relate_to():
    notion_month_database([])
    responses.add(responses.POST, PAGES_URL, status=200, json={"id": AUGUST_PAGE})

    ensure_current_month_page()

    assert current_month_id() == AUGUST_PAGE


# ─── IDEMPOTENCE ───────────────────────────────────────────────────────────────

@responses.activate
def test_a_second_run_finds_the_page_and_writes_nothing():
    """THE property the whole design rests on. The job runs nightly and `Month`
    can be sent at any time; if a repeat run could create a second page, every
    one of those would split the month's expenses across two pages."""
    notion_month_database([])
    responses.add(responses.POST, PAGES_URL, status=200, json={"id": AUGUST_PAGE})

    first = ensure_current_month_page()

    responses.reset()
    notion_month_database([month_page("August 2026")])
    second = ensure_current_month_page()

    assert first.action == CREATED
    assert second.action == UNCHANGED
    assert second.page_id == first.page_id
    assert sent_bodies("POST", PAGES_URL) == [], "a second page was created"


@responses.activate
def test_an_already_current_page_reports_no_change():
    month_module._state.update(period="2026-08", page_id=AUGUST_PAGE, title="August 2026")
    notion_month_database([month_page("August 2026")])

    result = ensure_current_month_page()

    assert result.action == UNCHANGED
    assert result.changed is False


@responses.activate
def test_an_existing_correct_page_is_adopted_when_david_was_pointing_elsewhere():
    """The page was made in Notion by hand before the job ran — the common case
    on the very first rollover after this feature ships."""
    notion_month_database([month_page("August 2026")])

    result = ensure_current_month_page()

    assert result.action == ADOPTED
    assert result.changed is True
    assert result.page_id == AUGUST_PAGE
    assert sent_bodies("POST", PAGES_URL) == []


# ─── RENAME ────────────────────────────────────────────────────────────────────

@responses.activate
def test_a_page_titled_with_the_bare_month_is_renamed_not_duplicated():
    """The old naming. Creating "August 2026" alongside an existing "August"
    would leave the month's expenses on the page David stopped pointing at."""
    notion_month_database([month_page("August", page_id="page-legacy")])
    responses.add(responses.PATCH, f"{PAGES_URL}/page-legacy", status=200, json={})

    result = ensure_current_month_page()

    assert result.action == RENAMED
    assert result.page_id == "page-legacy"
    assert written_title(sent_bodies("PATCH", PAGES_URL)[0]) == "August 2026"
    assert sent_bodies("POST", PAGES_URL) == []


@pytest.mark.parametrize("title", ["august 2026", "AUGUST 2026", "August  2026", " August 2026 "])
@responses.activate
def test_a_page_that_only_differs_in_case_or_spacing_is_retitled(title):
    """Matched loosely so it is never duplicated, then rewritten strictly so the
    database ends up in one consistent format."""
    notion_month_database([month_page(title)])
    responses.add(responses.PATCH, f"{PAGES_URL}/{AUGUST_PAGE}", status=200, json={})

    result = ensure_current_month_page()

    assert result.action == RENAMED
    assert result.page_id == AUGUST_PAGE
    assert written_title(sent_bodies("PATCH", PAGES_URL)[0]) == "August 2026"


@responses.activate
def test_last_months_page_is_left_alone():
    """A rollover renames the page for THIS month. Retitling July's page would
    move a month of history onto the wrong name."""
    notion_month_database([month_page("July 2026", page_id=JULY_PAGE)])
    responses.add(responses.POST, PAGES_URL, status=200, json={"id": AUGUST_PAGE})

    ensure_current_month_page()

    assert sent_bodies("PATCH", PAGES_URL) == []


# ─── AMBIGUITY IS REPORTED, NEVER GUESSED ──────────────────────────────────────

@responses.activate
def test_two_bare_month_pages_are_reported_instead_of_picked_from():
    """"August" carries no year, so two of them could be two different years.
    Guessing would silently attach a month of expenses to the wrong one, and
    creating a third page would be no better."""
    notion_month_database([month_page("August", page_id="page-a", created="2025-08-01T00:00:00Z"),
                           month_page("August", page_id="page-b", created="2026-08-01T00:00:00Z")])

    result = ensure_current_month_page()

    assert result.error is not None
    assert "August" in result.error
    assert result.changed is False
    assert sent_bodies("POST", PAGES_URL) == []
    assert sent_bodies("PATCH", PAGES_URL) == []


@responses.activate
def test_duplicate_correctly_titled_pages_resolve_to_the_oldest():
    """Not an error: both are already named for this month, so whichever expenses
    exist are on one of them — the older one. Reported in the log, not by
    refusing to work."""
    notion_month_database([
        month_page("August 2026", page_id="page-new", created="2026-08-09T00:00:00Z"),
        month_page("August 2026", page_id="page-old", created="2026-08-01T00:00:00Z"),
    ])

    result = ensure_current_month_page()

    assert result.page_id == "page-old"
    assert result.error is None


# ─── FAILURE HANDLING ──────────────────────────────────────────────────────────

@responses.activate
def test_a_notion_failure_leaves_the_current_month_pointer_alone():
    """Reported, not raised, and above all not blanked: David keeps writing
    expenses to the page it already knows while you fix the access problem."""
    responses.add(responses.GET, EXPENSES_SCHEMA_URL, status=404, json={})

    result = ensure_current_month_page()

    assert result.error is not None
    assert result.changed is False
    assert current_month_id() == JULY_PAGE


@responses.activate
def test_a_missing_relation_column_explains_the_fix():
    """The 404-style error a renamed column produces is unactionable on its own —
    'Notion 400' says nothing about which column, or that MONTHS_DB_ID exists."""
    responses.add(responses.GET, EXPENSES_SCHEMA_URL, status=200,
                  json={"properties": {"Conto": {"type": "relation"}}})

    result = ensure_current_month_page()

    assert "Account" in result.error
    assert "MONTHS_DB_ID" in result.error


@responses.activate
def test_a_month_database_without_a_title_column_is_reported():
    notion_month_database([], title_type="rich_text")

    result = ensure_current_month_page()

    assert "title" in result.error
    assert sent_bodies("POST", PAGES_URL) == []


@responses.activate
def test_a_failed_create_does_not_move_the_pointer():
    notion_month_database([])
    responses.add(responses.POST, PAGES_URL, status=400, json={"message": "nope"})

    result = ensure_current_month_page()

    assert result.error is not None
    assert current_month_id() == JULY_PAGE


@responses.activate
def test_a_failed_rename_does_not_move_the_pointer():
    notion_month_database([month_page("August", page_id="page-legacy")])
    responses.add(responses.PATCH, f"{PAGES_URL}/page-legacy", status=400, json={})

    result = ensure_current_month_page()

    assert result.error is not None
    assert current_month_id() == JULY_PAGE


# ─── THE OVERRIDE ──────────────────────────────────────────────────────────────

@responses.activate
def test_months_db_id_overrides_the_relation_lookup(monkeypatch):
    """Set it and the Expenses schema is never read — the escape hatch for a
    setup where the month pages are not the relation's target."""
    monkeypatch.setattr(month_module, "MONTHS_DB_ID", MONTHS_DB)
    responses.add(responses.GET, MONTHS_SCHEMA_URL, status=200,
                  json={"properties": {TITLE_PROP: {"type": "title"}}})
    responses.add(responses.POST, MONTHS_QUERY_URL, status=200,
                  json={"results": [month_page("August 2026")], "has_more": False})

    result = ensure_current_month_page()

    assert result.page_id == AUGUST_PAGE
    assert [c.request.url for c in responses.calls
            if c.request.url.startswith(EXPENSES_SCHEMA_URL)] == []


# ─── current_month_id: WHEN IT TALKS TO NOTION ─────────────────────────────────

def test_the_month_id_is_a_memory_read_while_it_is_current():
    """It is called on every expense write and every budget query. A Notion round
    trip there would put a network call on the hot path for a value that only
    changes twelve times a year — and `responses` is not even active here, so a
    request would raise rather than pass quietly."""
    month_module._state.update(period="2026-08", page_id=AUGUST_PAGE, title="August 2026")

    assert current_month_id() == AUGUST_PAGE


@responses.activate
def test_a_stale_month_id_is_resolved_on_first_use():
    """The safety net for a missed rollover: David was redeployed over the 1st,
    so the nightly job never fired and the pointer still says July. Without this,
    every expense until the next midnight lands in the wrong month."""
    notion_month_database([month_page("August 2026")])

    assert month_module._state["period"] == "2026-07"
    assert current_month_id() == AUGUST_PAGE


@responses.activate
def test_a_clock_that_appears_to_go_backwards_resolves_nothing(monkeypatch):
    """Months only ever roll forward. A cached period NEWER than the clock is a
    clock problem, and re-resolving on it would cache the wrong month — so the
    check is one-directional, not a plain inequality."""
    month_module._state.update(period="2026-08", page_id=AUGUST_PAGE, title="August 2026")
    monkeypatch.setattr(month_module, "now_local", lambda: datetime(2025, 6, 10))

    assert current_month_id() == AUGUST_PAGE
    assert len(responses.calls) == 0, "a backwards clock sent David to Notion"


@responses.activate
def test_an_unresolvable_stale_pointer_falls_back_to_the_last_known_page():
    """Returning None would refuse the expense outright. The same Notion outage
    fails the write a moment later anyway, so the fallback costs nothing — and
    the nightly job reports the failure to Telegram."""
    responses.add(responses.GET, EXPENSES_SCHEMA_URL, status=500, json={})

    assert current_month_id() == JULY_PAGE


# ─── WHAT A FRESH PROCESS BELIEVES ─────────────────────────────────────────────
# There is no state file. The cache is memory-only, so every container starts
# knowing nothing — which is the point: it has to ASK rather than believe what it
# was handed.

@responses.activate
def test_a_stale_month_id_is_never_trusted_without_asking_notion(monkeypatch):
    """THE BUG THE STATE FILE WAS ACCIDENTALLY HIDING.

    _initial_state() used to stamp the seed with TODAY'S period, so
    current_month_id() saw a fresh-looking cache and returned MONTH_ID without a
    single API call. MONTH_ID is a value pasted into Railway once and documented
    as safe to let go stale — so on any container that started with the file
    missing (which is every deploy: the filesystem is ephemeral) every expense
    until the next 00:05 job was filed against LAST month's page, and `B`
    answered for the wrong month. Both look completely normal.
    """
    monkeypatch.setattr(month_module, "BOOTSTRAP_MONTH_ID", JULY_PAGE)
    monkeypatch.setattr(month_module, "_state", month_module._initial_state())
    notion_month_database([month_page("August 2026")])

    assert current_month_id() == AUGUST_PAGE, (
        "current_month_id() handed back the stale seed instead of resolving")
    assert responses.calls, "it answered from the seed without asking Notion"


def test_a_fresh_process_starts_knowing_nothing(monkeypatch):
    """period=None reads as older than any real month, so the first caller
    resolves from Notion instead of believing what it was handed."""
    monkeypatch.setattr(month_module, "BOOTSTRAP_MONTH_ID", "seed-page-id")

    assert month_module._initial_state() == {
        "period": None, "page_id": "seed-page-id", "title": ""}


def test_with_no_seed_at_all_the_page_is_unknown_too(monkeypatch):
    monkeypatch.setattr(month_module, "BOOTSTRAP_MONTH_ID", None)

    assert month_module._initial_state() == {
        "period": None, "page_id": None, "title": ""}


@responses.activate
def test_the_seed_is_the_fallback_when_notion_cannot_answer(monkeypatch):
    """The one job MONTH_ID still has: a stale page beats no page in an outage.

    Only reached AFTER Notion has been asked and could not answer, which is the
    whole difference from the behaviour above.
    """
    monkeypatch.setattr(month_module, "BOOTSTRAP_MONTH_ID", JULY_PAGE)
    monkeypatch.setattr(month_module, "_state", month_module._initial_state())
    responses.add(responses.GET, EXPENSES_SCHEMA_URL, status=500, json={})

    assert current_month_id() == JULY_PAGE
    assert responses.calls, "it fell back without asking Notion first"


@responses.activate
def test_the_resolved_page_is_reused_for_the_rest_of_the_process(monkeypatch):
    """The cache still earns its keep WITHIN a process — one resolve, not one per
    expense. Losing it across a restart costs two API calls, which is why it does
    not need to survive one."""
    monkeypatch.setattr(month_module, "_state", month_module._initial_state())
    notion_month_database([month_page("August 2026")])

    assert current_month_id() == AUGUST_PAGE
    calls_after_first = len(responses.calls)

    assert current_month_id() == AUGUST_PAGE
    assert len(responses.calls) == calls_after_first, (
        "the second call went back to Notion — the in-memory cache is not working")


@responses.activate
def test_repairing_a_past_month_does_not_repoint_this_month():
    """`when` exists so a month missed while David was off can be fixed by hand.
    Doing that must not send today's expenses to a page from last year."""
    month_module._state.update(period="2026-08", page_id=AUGUST_PAGE, title="August 2026")
    notion_month_database([month_page("June 2026", page_id="page-june")])
    responses.add(responses.PATCH, f"{PAGES_URL}/page-june", status=200, json={})

    result = ensure_current_month_page(when=datetime(2026, 6, 15))

    assert result.page_id == "page-june"
    assert current_month_id() == AUGUST_PAGE


# ─── CONCURRENCY ───────────────────────────────────────────────────────────────

def test_two_threads_at_a_month_boundary_create_one_page(monkeypatch):
    """Find-then-create across two round trips, on WORKER THREADS.

    Every Notion call in David runs inside asyncio.to_thread, so the nightly job
    and an expense write really can be inside this cycle at the same instant.
    page_lock.py cannot serialise them — it is an asyncio lock, and two worker
    threads are not two coroutines — so month.py uses a threading lock instead.
    Without one, both threads query an empty database and both create a page.
    """
    pages, created = [], []
    monkeypatch.setattr(month_module, "_months_database",
                        lambda: (MONTHS_DB, TITLE_PROP, None))
    monkeypatch.setattr(month_module, "query_database",
                        lambda db_id, **kw: (list(pages), None))

    def slow_create(db_id, properties, **kwargs):
        title = properties[TITLE_PROP]["title"][0]["text"]["content"]
        page_id = f"page-{len(created)}"
        time.sleep(0.05)          # the window a second thread can slip into
        created.append(page_id)
        pages.append(month_page(title, page_id=page_id))
        return page_id, None

    monkeypatch.setattr(month_module, "create_page", slow_create)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result() for f in
                   [pool.submit(ensure_current_month_page) for _ in range(2)]]

    assert created == ["page-0"], f"created {created} — two pages for one month"
    assert {r.page_id for r in results} == {"page-0"}
    assert sorted(r.action for r in results) == [CREATED, UNCHANGED]


def test_the_lock_is_a_threading_lock_not_an_asyncio_one():
    """Asserted directly because the failure mode is invisible: an asyncio.Lock
    here would acquire without ever blocking, and the test above would still pass
    on a fast machine."""
    assert isinstance(month_module._lock, type(threading.RLock()))


# ─── THE MESSAGE ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("action", [CREATED, RENAMED, ADOPTED])
def test_the_message_carries_the_month_and_the_page_id(action):
    """The page ID is in the message so it can be pasted into Railway — the seed
    variable is not required to stay current, but keeping it right costs one tap."""
    text = format_rollover(month_module.Rollover(AUGUST_PAGE, "August 2026", action))

    assert "August 2026" in text
    assert AUGUST_PAGE in text
    assert text.startswith("✅ Monthly expenses page updated")


def test_an_unchanged_run_does_not_claim_to_have_updated_anything():
    text = format_rollover(month_module.Rollover(AUGUST_PAGE, "August 2026", UNCHANGED))

    assert "updated" not in text
    assert AUGUST_PAGE in text


def test_a_failure_message_says_what_went_wrong():
    text = format_rollover(
        month_module.Rollover(JULY_PAGE, "July 2026", UNCHANGED, "Notion 404: not found"))

    assert "⚠️" in text
    assert "Notion 404" in text


# ─── THE JOB'S POLICY, END TO END ──────────────────────────────────────────────
# These two drive the REAL ensure_current_month_page over the REAL builder, so
# they fail if from_period stops being threaded through — which the monkeypatched
# tests below, constructing Rollovers by hand, could not notice.

@responses.activate
def test_a_restart_mid_month_does_not_ping(monkeypatch):
    """WHY THE MESSAGE ARRIVED EVERY DAY.

    David is holding a page ID that is not the one Notion resolves to — which is
    what a stale MONTH_ID fallback looks like once it has been adopted. The next
    nightly run adopts the resolved page, `changed` goes true, and a
    "✅ Monthly expenses page updated" went out on a day in the middle of August.
    """
    monkeypatch.setattr(month_module, "_state",
                        {"period": "2026-08", "page_id": "stale-seed-from-railway",
                         "title": "August 2026"})
    notion_month_database([month_page("August 2026")])

    # The builder, not a hand-built Rollover: this is the path the job takes, and
    # it must be the FIRST resolve — a second one would find the state already
    # repaired and be silent for the wrong reason.
    assert build_rollover_message() == (None, None)


@responses.activate
def test_that_restart_is_still_a_write_worth_logging(monkeypatch):
    """The counterpart to the silence above: nothing here says the adoption was
    wrong, only that it is not news. `changed` must stay true so the log and the
    `Month` command still report it."""
    monkeypatch.setattr(month_module, "_state",
                        {"period": "2026-08", "page_id": "stale-seed-from-railway",
                         "title": "August 2026"})
    notion_month_database([month_page("August 2026")])

    result = ensure_current_month_page()

    assert result.action == ADOPTED, "the pointer did move — that part was right"
    assert result.changed, "and it is still a write worth logging"
    assert not result.rolled_over, "but the month did not turn, so it is not news"


@responses.activate
def test_the_first_run_of_a_new_month_does_ping(monkeypatch):
    """The counterpart, on the night it matters: July in the cache, August on the
    clock. Anything that silences the restart case must leave this one speaking."""
    monkeypatch.setattr(month_module, "_state",
                        {"period": "2026-07", "page_id": JULY_PAGE, "title": "July 2026"})
    notion_month_database([month_page("August 2026")])

    text, err = build_rollover_message()

    assert err is None
    assert text is not None, "the 1st went unannounced"
    assert "August 2026" in text
    assert AUGUST_PAGE in text


# ─── THE JOB'S POLICY ──────────────────────────────────────────────────────────

def rollover(action, error=None, period="2026-08", from_period="2026-08"):
    """A Rollover as the job sees one. Defaults to "the month did not move"."""
    page = AUGUST_PAGE if period == "2026-08" else JULY_PAGE
    title = "August 2026" if period == "2026-08" else "July 2026"
    return month_module.Rollover(page, title, action, error, period, from_period)


def test_the_job_stays_silent_when_there_was_nothing_to_do(monkeypatch):
    """It runs every night and only has work on the 1st. A nightly "still August"
    would train you to ignore the one message a year that matters."""
    monkeypatch.setattr("proactive.month_rollover.ensure_current_month_page",
                        lambda: rollover(UNCHANGED))

    assert build_rollover_message() == (None, None)


@pytest.mark.parametrize("action", [CREATED, RENAMED, ADOPTED])
def test_the_job_speaks_up_when_the_month_moved(monkeypatch, action):
    monkeypatch.setattr("proactive.month_rollover.ensure_current_month_page",
                        lambda: rollover(action, from_period="2026-07"))

    text, err = build_rollover_message()

    assert AUGUST_PAGE in text
    assert err is None


@pytest.mark.parametrize("action", [CREATED, RENAMED, ADOPTED])
def test_a_write_within_the_same_month_is_not_announced(monkeypatch, action):
    """THE DAILY-MESSAGE BUG. The job tested `changed`, which is true of any run
    that wrote to Notion — including the ADOPTED a re-resolve of the SAME month
    produces.

    So "✅ Monthly expenses page updated" arrived on ordinary days, claiming a
    change of month that had not happened. Same period in and out = silence,
    whatever was written — including CREATED, which is only news to a run that
    did not already know which month it was on.
    """
    monkeypatch.setattr("proactive.month_rollover.ensure_current_month_page",
                        lambda: rollover(action, from_period="2026-08"))

    assert build_rollover_message() == (None, None)


def test_a_missed_rollover_is_announced_on_the_day_it_catches_up(monkeypatch):
    """Deliberately a period comparison and not `now_local().day == 1`.

    If David is down over the 1st, the roll happens on the 2nd — the one run you
    most need to hear about, and the one a strict day-of-month check would drop on
    the floor in silence.
    """
    monkeypatch.setattr("proactive.month_rollover.ensure_current_month_page",
                        lambda: rollover(ADOPTED, from_period="2026-07"))

    text, err = build_rollover_message()

    assert "August 2026" in text
    assert err is None


def test_a_first_run_that_created_the_page_is_announced(monkeypatch):
    """from_period is None, so David did not know which month it was on — but a
    page that did not exist and now does is news whoever asked for it, and it can
    only happen once a month."""
    monkeypatch.setattr("proactive.month_rollover.ensure_current_month_page",
                        lambda: rollover(CREATED, from_period=None))

    text, _ = build_rollover_message()

    assert AUGUST_PAGE in text


@pytest.mark.parametrize("action", [RENAMED, ADOPTED])
def test_a_first_run_that_only_found_the_page_stays_quiet(monkeypatch, action):
    """THE DAILY-MESSAGE BUG, in the shape dropping the state file gives it.

    Every process now starts with from_period=None — the resolved page is cached
    in memory only, so there is nothing to carry across a restart. If that counted
    as "the month moved", a "✅ Monthly expenses page updated" would go out on
    every single deploy, claiming a change of month that had not happened. A run
    that landed on a page which already existed is a boot, and boots are not news.
    """
    monkeypatch.setattr("proactive.month_rollover.ensure_current_month_page",
                        lambda: rollover(action, from_period=None))

    assert build_rollover_message() == (None, None)


def test_the_job_always_reports_a_failure(monkeypatch):
    """A silent failure here is the original bug in a new costume: expenses keep
    going to last month's page and nothing says so."""
    monkeypatch.setattr("proactive.month_rollover.ensure_current_month_page",
                        lambda: month_module.Rollover(JULY_PAGE, "July 2026", UNCHANGED,
                                                      "Notion 401: unauthorized"))

    text, err = build_rollover_message()

    # The failure is both SAID (its wording names the month and page, which reads
    # better than a bare exception) and RETURNED (so the scheduler logs it too).
    assert "Notion 401" in text
    assert err == "Notion 401: unauthorized"


def test_a_failure_on_the_1st_is_not_also_read_as_a_rollover(monkeypatch):
    """A failed run moved nothing. If the clock turning the month were enough to
    make it `rolled_over`, the failure notice would arrive with a success
    predicate attached — and the retry that actually succeeds tomorrow would then
    look like a repeat rather than the recovery.
    """
    monkeypatch.setattr("proactive.month_rollover.ensure_current_month_page",
                        lambda: month_module.Rollover(JULY_PAGE, "July 2026", UNCHANGED,
                                                      "Notion 502: bad gateway",
                                                      "2026-07", "2026-07"))

    text, err = build_rollover_message()

    assert "Notion 502" in text
    assert err == "Notion 502: bad gateway"


# ─── THE COMMAND ───────────────────────────────────────────────────────────────

@responses.activate
def test_the_month_command_reports_the_page_it_resolved():
    notion_month_database([month_page("August 2026")])
    update = FakeUpdate(text="Month")

    run(month_module.run_month(**with_update(update)))

    assert update.message.replied_with(AUGUST_PAGE)
    assert update.message.replied_with("August 2026")


def test_the_month_command_answers_even_when_nothing_changed(monkeypatch):
    """Unlike the job. A manual command that replies with silence looks broken."""
    monkeypatch.setattr(month_module, "ensure_current_month_page",
                        lambda: month_module.Rollover(AUGUST_PAGE, "August 2026", UNCHANGED))
    update = FakeUpdate(text="Month")

    run(month_module.run_month(**with_update(update)))

    assert update.message.replied_with("August 2026")


def test_the_month_command_runs_with_no_update_at_all(monkeypatch):
    """THE PROOF THE SPLIT WORKED.

    The rollover was already callable from the nightly job — proactive/ has read
    ensure_current_month_page all along — but the COMMAND was not: it took an
    update and replied through telegram_text, so the thing that reports the
    result to a human could only ever be a bot handler. A list's `append` is the
    whole interface now.
    """
    monkeypatch.setattr(month_module, "ensure_current_month_page",
                        lambda: month_module.Rollover(AUGUST_PAGE, "August 2026", CREATED))
    said = []

    async def notify(text):
        said.append(text)

    run(month_module.run_month(notify=notify))

    assert any("August 2026" in message for message in said)
    assert any(AUGUST_PAGE in message for message in said), (
        "the page ID is the whole reason you send Month by hand")
