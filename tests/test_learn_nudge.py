"""The Learn nudge — pages saved and never merged into a Manual.

WHAT THIS FILE IS ABOUT. Not the list. The list is easy; what is hard is that
this job has four outcomes and three of them look like each other from the
outside:

    a backlog          → name it
    caught up          → say nothing
    Notion unreachable → REPORT, do not go quiet
    no `Implemented`   → REPORT, in different words, because the fix is different

The third is the one M2 was about, one module over: silence that means "you are
done" and silence that means "I could not look" are the same message. The fourth
is why this job refuses rather than degrades — with no checkbox there is nothing
to degrade to.

WHERE THE FILTERING HAPPENS, and therefore what these tests can assert.
"Unimplemented and older than N days" is ONE Notion filter, evaluated by Notion.
A fake Notion returns whatever rows it is handed, so no assertion here can prove
a page was excluded — the exclusion is in the REQUEST. Hence the tests that read
the filter body: that is where the behaviour actually lives.
"""

from datetime import datetime, timedelta, timezone

import pytest

from config import LEARN_NUDGE_MAX_ITEMS, LEARN_NUDGE_STALE_DAYS
import proactive.learn_nudge as nudge

NOW = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)


def page(title, days_old=30, page_id=None):
    """One row of a Notion query response, as query_database returns it."""
    return {
        "id": page_id or f"page-{title}",
        "created_time": (NOW - timedelta(days=days_old)).isoformat(),
        "properties": {"Name": {"type": "title",
                                "title": [{"plain_text": title}]}},
    }


@pytest.fixture
def notion(monkeypatch):
    """A Learn database with three stale, unimplemented pages.

    `queries` records what was actually asked of Notion — the filter is where
    "pending" is decided, so several tests below read it rather than the rows.
    """
    state = {
        "pages": [page("Deep Work", 30), page("Atomic Habits", 12),
                  page("Why We Sleep", 9)],
        "error": None,
        "prop": ("checkbox", None),
        "queries": [],
    }

    def query_database(db_id, filter_obj=None, sorts=None, page_size=100):
        state["queries"].append({"db": db_id, "filter": filter_obj, "sorts": sorts})
        return (state["pages"], None) if state["error"] is None else ([], state["error"])

    monkeypatch.setattr(nudge, "query_database", query_database)
    monkeypatch.setattr(nudge, "database_property_type",
                        lambda db_id, prop: state["prop"])
    monkeypatch.setattr(nudge, "now_local", lambda: NOW)
    return state


# ─── THE BACKLOG ───────────────────────────────────────────────────────────────

def test_it_names_the_pages_you_never_implemented(notion):
    text, err = nudge.build_nudge()

    assert err is None
    assert "Deep Work" in text
    assert "Atomic Habits" in text
    assert "Why We Sleep" in text


def test_it_says_how_old_each_one_is(notion):
    """A backlog with no ages is a list; with ages it is a priority order."""
    text, _ = nudge.build_nudge()

    assert "Deep Work (30 days ago)" in text
    assert "Why We Sleep (9 days ago)" in text


def test_it_asks_notion_for_the_oldest_first(notion):
    """The cap cuts from the END, so the sort decides WHICH pages get named.
    Oldest-first is what makes the cut the newest ones rather than arbitrary."""
    nudge.build_nudge()

    assert notion["queries"][0]["sorts"] == [
        {"timestamp": "created_time", "direction": "ascending"}]


def test_it_tells_you_how_to_act_on_it(notion):
    text, _ = nudge.build_nudge()

    assert "Implement" in text, "the nudge does not say what to do about it"


def test_a_page_with_no_created_time_is_still_listed(notion):
    """The age is decoration. It must never be the thing that costs you the row —
    and an unknown age must not print as '0 days ago', which would put the oldest
    debt in the list looking like the newest."""
    notion["pages"] = [{"id": "p-1", "properties": {
        "Name": {"type": "title", "title": [{"plain_text": "Undated"}]}}}]

    text, err = nudge.build_nudge()

    assert err is None
    assert "Undated" in text
    assert "0 days ago" not in text


# ─── WHAT "PENDING" MEANS — ASSERTED ON THE REQUEST ────────────────────────────
# See the module docstring: Notion evaluates the predicate, so the filter body is
# the behaviour. A test that only checked the returned rows would pass against a
# filter that asked for the wrong thing entirely.


def test_only_unimplemented_pages_are_asked_for(notion):
    """The checkbox is the whole point of the job — a page you have merged must
    never be named again."""
    nudge.build_nudge()

    clauses = notion["queries"][0]["filter"]["and"]

    assert {"property": "Implemented", "checkbox": {"equals": False}} in clauses


def test_the_staleness_cutoff_is_now_minus_the_threshold(notion):
    """THE BOUNDARY, and the only place it can be asserted.

    `before` is strict, so a page created exactly LEARN_NUDGE_STALE_DAYS ago is
    NOT named and becomes eligible a moment later. That is Notion's rule, not
    ours — which is exactly why this test pins the cutoff and the operator
    rather than an outcome no fake can produce.
    """
    nudge.build_nudge()

    clauses = notion["queries"][0]["filter"]["and"]
    timestamp = next(c for c in clauses if c.get("timestamp") == "created_time")

    expected = (NOW - timedelta(days=LEARN_NUDGE_STALE_DAYS)).isoformat()
    assert timestamp["created_time"] == {"before": expected}


def test_the_cutoff_is_measured_from_the_project_clock(notion, monkeypatch):
    """`now_local()`, never `datetime.now()` — the clock is a project decision
    and this job would otherwise compute the cutoff in the server's timezone."""
    moved = NOW - timedelta(days=100)
    monkeypatch.setattr(nudge, "now_local", lambda: moved)

    nudge.build_nudge()

    clauses = notion["queries"][0]["filter"]["and"]
    timestamp = next(c for c in clauses if c.get("timestamp") == "created_time")

    assert timestamp["created_time"]["before"] == (
        moved - timedelta(days=LEARN_NUDGE_STALE_DAYS)).isoformat(), (
        "the cutoff did not move with the clock — it is being read from "
        "somewhere other than now_local()")


# ─── THE CAP ───────────────────────────────────────────────────────────────────

def test_more_pending_than_the_cap_names_the_overflow(notion):
    """A list of forty is the same as no list. The count is what keeps the size
    of the debt visible after the list stops."""
    notion["pages"] = [page(f"Book {i}", days_old=30 - i)
                       for i in range(LEARN_NUDGE_MAX_ITEMS + 4)]

    text, _ = nudge.build_nudge()

    assert text.count("•") == LEARN_NUDGE_MAX_ITEMS
    assert "and 4 more" in text
    assert f"{LEARN_NUDGE_MAX_ITEMS + 4} Learn page(s)" in text


def test_a_backlog_at_exactly_the_cap_does_not_claim_an_overflow(notion):
    """The mirror: "…and 0 more." is the kind of line you stop trusting."""
    notion["pages"] = [page(f"Book {i}") for i in range(LEARN_NUDGE_MAX_ITEMS)]

    text, _ = nudge.build_nudge()

    assert "more." not in text


# ─── THE THREE SILENCES, KEPT APART ────────────────────────────────────────────

def test_nothing_pending_is_genuinely_silent(notion):
    """No weekly "you have nothing to do" ping. This is the ONLY silence."""
    notion["pages"] = []

    assert nudge.build_nudge() == (None, None)


def test_a_notion_failure_reports_instead_of_going_quiet(notion):
    """THE WHOLE POINT OF THE (text, error) CONTRACT.

    Without this the job repeats the bug M2 existed to fix: an outage and an
    empty backlog produce identical output, and the silence reads as "you are
    caught up" for as long as Notion is unhappy.
    """
    notion["error"] = "Notion 503: service unavailable"

    text, err = nudge.build_nudge()

    assert text is None
    assert "503" in err


def test_a_missing_implemented_column_reports_rather_than_listing_nothing(notion):
    """The refusal, and it is deliberately the opposite of Learn's Source URL
    check: there the safeguard is optional so it degrades, here the checkbox IS
    the feature and there is nothing to degrade to."""
    notion["prop"] = (None, None)

    text, err = nudge.build_nudge()

    assert text is None
    assert "Implemented" in err
    assert "checkbox" in err


def test_a_failed_schema_read_is_not_reported_as_a_missing_column(notion):
    """database_property_type has three outcomes and `if not prop_type` folds
    two of them together. "Add a column" and "Notion is down" want opposite
    reactions from the person reading the report."""
    notion["prop"] = (None, "Notion 401: API token is invalid")

    _, err = nudge.build_nudge()

    assert "401" in err
    assert "no 'Implemented' checkbox" not in err


def test_a_column_of_the_wrong_type_says_so(notion):
    """A text column named Implemented would make the checkbox filter a 400 —
    reported as an outage, weekly, forever."""
    notion["prop"] = ("rich_text", None)

    text, err = nudge.build_nudge()

    assert text is None
    assert "rich_text" in err


def test_no_learn_database_is_reported(notion, monkeypatch):
    monkeypatch.setattr(nudge, "LEARN_ID", None)

    text, err = nudge.build_nudge()

    assert text is None
    assert "LEARN_ID" in err


# ─── IT SURVIVES REAL TITLES ───────────────────────────────────────────────────

def test_a_title_full_of_markdown_does_not_break_the_send(notion, monkeypatch):
    """Driven through the real scheduler job, because that is where a parse_mode
    would be added — asserting on the builder's string alone would keep passing
    if someone made this job send Markdown."""
    import proactive.scheduler as scheduler
    from conftest import FakeContext, run

    notion["pages"] = [page("*Deep* _Work_ `and` [brackets]")]

    context = FakeContext()
    context.job = type("Job", (), {"chat_id": "chat-1"})()

    run(scheduler._learn_nudge_job(context))

    chat_id, text, kwargs = context.bot.sent_full[0]
    assert "*Deep* _Work_" in text
    assert kwargs.get("parse_mode") is None, (
        "the nudge interpolates Notion page titles — Markdown would reject the "
        "message on exactly the titles most worth reading")
