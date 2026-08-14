"""Budget tests — Notion mocked with `responses`, so these run offline.

There is ONE implementation, in budget.py:

  compute_budget()  aggregates the month and works out the pacing numbers
  format_budget(b)  renders the recap text
  budget()          compute + format — what the `B` command and the Sunday
                    recap send, reached as david.budget

david.py used to carry its own independent copy. Same maths, same output, but a
second place to fix a pagination or rounding bug and a second place to forget.
The tests below still drive `david.budget()` because that is the shipping path
for the `B` command, and one asserts it IS budget.budget rather than a copy of
it.
"""

from datetime import datetime

import pytest
import responses

import budget as budget_module
import david
from budget import compute_budget, format_budget
from conftest import EXPENSES_ID, NOTION_BASE

QUERY_URL = f"{NOTION_BASE}/databases/{EXPENSES_ID}/query"

MONTH_PAGE = "test-month-id"


@pytest.fixture(autouse=True)
def pinned_month(monkeypatch):
    """Pin the month page so these tests are about budget maths, not resolution.

    The resolved month page is cached in memory only, so the first
    current_month_id() call in a process asks Notion — which would otherwise put
    an unrelated schema request in front of every query these tests inspect.
    """
    monkeypatch.setattr(budget_module, "current_month_id", lambda: MONTH_PAGE)


# ─── FIXTURE BUILDERS ──────────────────────────────────────────────────────────

def expense(amount, category="Food", name="Item"):
    """One row of a Notion database-query response."""
    page = {
        "id": f"page-{name}-{amount}",
        "properties": {
            "Name":     {"title": [{"plain_text": name, "text": {"content": name}}]},
            "Amount":   {"number": amount},
            "Category": {"multi_select": [{"name": category}]},
        },
    }
    if category is None:
        del page["properties"]["Category"]
    if amount is None:
        page["properties"]["Amount"] = {"number": None}
    return page


def notion_query(pages, status=200, has_more=False, next_cursor=None):
    """Register one Notion query response."""
    responses.add(
        responses.POST,
        QUERY_URL,
        json={"results": pages, "has_more": has_more, "next_cursor": next_cursor},
        status=status,
    )


@pytest.fixture
def frozen_june_10(monkeypatch):
    """Pin 'today' to 10 June (day 10 of 30) so pacing maths is deterministic."""

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            moment = cls(2025, 6, 10, 12, 0, 0)
            return tz.localize(moment) if tz is not None else moment

    monkeypatch.setattr(budget_module, "datetime", FrozenDatetime)


# ─── AGGREGATION ───────────────────────────────────────────────────────────────

@responses.activate
def test_budget_totals_and_groups_by_category():
    notion_query([
        expense(10.00, "Food"),
        expense(5.50,  "Food"),
        expense(30.00, "Shopping"),
        expense(2.50,  "Gift"),
    ])

    text, err = david.budget()

    assert err is None
    assert "**Food: €15.50**" in text
    assert "**Shopping: €30.00**" in text
    assert "**Gift: €2.50**" in text
    assert "**Spent: €48.00**" in text
    assert "**Remaining: €252.00** (of €300)" in text


@responses.activate
def test_budget_lists_categories_biggest_first():
    notion_query([
        expense(5.00,  "Gift"),
        expense(50.00, "Shopping"),
        expense(20.00, "Food"),
    ])

    lines = david.budget()[0].splitlines()
    categories = [ln for ln in lines if ln.startswith("**") and "€" in ln and ":" in ln]

    assert categories[0].startswith("**Shopping")
    assert categories[1].startswith("**Food")
    assert categories[2].startswith("**Gift")


@responses.activate
def test_budget_queries_the_current_month_only():
    """The filter must scope to the Month relation, or it totals every month."""
    notion_query([expense(10.00)])

    david.budget()

    sent = responses.calls[0].request.body
    body = sent.decode() if isinstance(sent, bytes) else sent
    assert "Account" in body
    assert MONTH_PAGE in body


@responses.activate
def test_budget_survives_rows_with_no_amount_or_category():
    notion_query([
        expense(None, "Food"),          # Amount cleared in Notion
        expense(10.00, None),           # no Category selected
        expense(5.00, "Food"),
    ])

    text, _ = david.budget()

    assert "**Other: €10.00**" in text      # uncategorised falls back to Other
    assert "**Food: €5.00**" in text        # the null amount contributed 0
    assert "**Spent: €15.00**" in text


@responses.activate
def test_budget_with_no_expenses_yet():
    notion_query([])

    text, err = david.budget()

    assert err is None, "an empty month is not a failure"
    assert "**Spent: €0.00**" in text
    assert "**Remaining: €300.00**" in text


@responses.activate
def test_a_rejected_query_comes_back_as_an_error_not_as_an_empty_month():
    """The collapse this milestone removed.

    `None` meant "Notion failed" AND "nothing to report", so a 400 reached
    `_budget_line` and `_should_warn` as an ordinary quiet month. Both halves
    are asserted: no recap to print, and a non-empty reason to print instead.
    """
    notion_query([], status=400)
    notion_query([], status=400)

    text, err = david.budget()
    assert text is None
    assert err, "a failed query must come back with a reason"

    b, err = compute_budget()
    assert b is None
    assert err


@responses.activate
def test_an_auth_failure_names_itself(caplog):
    """A 401 is the failure most worth naming: it does not clear up on its own,
    and 'Could not fetch budget from Notion' reads identically to a 429."""
    notion_query([], status=401)

    text, err = david.budget()

    assert text is None
    assert "401" in err, f"the reason did not name the status: {err!r}"


@responses.activate
def test_an_empty_month_is_a_budget_not_an_error():
    """THE MIRROR, and the reason there is no third state.

    A month with nothing in it is a perfectly good dict whose total is 0.0. If
    this ever came back as an error, every reader would report an outage on the
    1st of the month.
    """
    notion_query([])

    b, err = compute_budget()

    assert err is None
    assert b["total"] == 0.0
    assert b["per_category"] == {}


# ─── PACING (compute_budget only) ──────────────────────────────────────────────

@responses.activate
def test_compute_budget_paces_against_the_month(frozen_june_10):
    notion_query([expense(100.00, "Food"), expense(50.00, "Shopping")])

    b, _ = compute_budget()

    assert b["total"]            == 150.00
    assert b["ceiling"]          == 300.00
    assert b["remaining"]        == 150.00
    assert b["day"]              == 10
    assert b["days_in_month"]    == 30          # June
    assert b["expected_to_date"] == 100.00      # 300 * 10/30
    assert b["on_pace"] is False                # spent 150 against 100 expected
    assert b["projected_total"]  == 450.00      # 150 / 10 * 30
    assert b["projected_over"]   == 150.00
    assert b["top_category"]     == ("Food", 100.00)


@responses.activate
def test_compute_budget_on_pace_reports_no_overspend(frozen_june_10):
    notion_query([expense(60.00, "Food")])

    b, _ = compute_budget()

    assert b["on_pace"] is True                 # 60 spent vs 100 expected
    assert b["projected_total"] == 180.00
    assert b["projected_over"]  == 0.0


@responses.activate
def test_compute_budget_with_no_expenses_has_no_top_category(frozen_june_10):
    notion_query([])

    b, _ = compute_budget()

    assert b["total"] == 0.0
    assert b["top_category"] is None
    assert b["projected_over"] == 0.0


# ─── ONE IMPLEMENTATION ────────────────────────────────────────────────────────

EQUIVALENCE_CASES = {
    "several categories": [expense(10.00, "Food"), expense(5.50, "Food"),
                           expense(30.00, "Shopping"), expense(2.50, "Gift")],
    "single category":    [expense(7.25, "Food")],
    "no expenses":        [],
    "null amount":        [expense(None, "Food"), expense(5.00, "Food")],
    "no category":        [expense(10.00, None)],
    "ties on amount":     [expense(10.00, "Food"), expense(10.00, "Gift")],
    "over the ceiling":   [expense(500.00, "Shopping")],
}


@pytest.mark.parametrize("rows", EQUIVALENCE_CASES.values(), ids=EQUIVALENCE_CASES)
@responses.activate
def test_the_b_command_and_the_shared_recap_agree_exactly(rows):
    """Byte-for-byte, across every shape of data the aggregation can meet.

    Written while david.py still had its own copy of budget(), to prove the two
    were interchangeable before one was deleted. It keeps earning its place
    afterwards: it is what would catch the recap drifting if the `B` command
    were ever pointed somewhere else again.
    """
    notion_query(rows)
    notion_query(rows)

    assert david.budget()[0] == format_budget(compute_budget()[0])


def test_there_is_only_one_budget_implementation():
    """`B` must call the shared budget(), not a private copy.

    david.py carried its own duplicate for a while — same maths, same output,
    but a second place to fix and a second place to forget. budget.py was
    written to replace it and says so in its docstring; the call site simply
    never switched over.
    """
    assert david.budget is budget_module.budget, (
        "david.py has its own budget() again — the recap can now drift from the "
        "one proactive/ uses")
