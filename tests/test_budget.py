"""Budget tests — Notion mocked with `responses`, so these run offline.

Covers both implementations, because there are two:

  david.budget()          the one the `B` command and the weekly recap call
  budget.compute_budget() the shared one proactive/ calls, which also does pacing

They aggregate the same data and are expected to agree on the totals; a test
below asserts exactly that, so if one is changed without the other the suite
says so.
"""

from datetime import datetime

import pytest
import responses

import budget as budget_module
import david
from budget import compute_budget, format_budget
from conftest import EXPENSES_ID, NOTION_BASE

QUERY_URL = f"{NOTION_BASE}/databases/{EXPENSES_ID}/query"


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

    text = david.budget()

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

    lines = david.budget().splitlines()
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
    assert "test-month-id" in body


@responses.activate
def test_budget_survives_rows_with_no_amount_or_category():
    notion_query([
        expense(None, "Food"),          # Amount cleared in Notion
        expense(10.00, None),           # no Category selected
        expense(5.00, "Food"),
    ])

    text = david.budget()

    assert "**Other: €10.00**" in text      # uncategorised falls back to Other
    assert "**Food: €5.00**" in text        # the null amount contributed 0
    assert "**Spent: €15.00**" in text


@responses.activate
def test_budget_with_no_expenses_yet():
    notion_query([])

    text = david.budget()

    assert "**Spent: €0.00**" in text
    assert "**Remaining: €300.00**" in text


@responses.activate
def test_budget_returns_none_when_notion_rejects_the_query():
    notion_query([], status=400)

    assert david.budget() is None
    assert compute_budget() is None


@responses.activate
def test_budget_returns_none_on_notion_auth_failure():
    notion_query([], status=401)

    assert david.budget() is None


# ─── PACING (compute_budget only) ──────────────────────────────────────────────

@responses.activate
def test_compute_budget_paces_against_the_month(frozen_june_10):
    notion_query([expense(100.00, "Food"), expense(50.00, "Shopping")])

    b = compute_budget()

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

    b = compute_budget()

    assert b["on_pace"] is True                 # 60 spent vs 100 expected
    assert b["projected_total"] == 180.00
    assert b["projected_over"]  == 0.0


@responses.activate
def test_compute_budget_with_no_expenses_has_no_top_category(frozen_june_10):
    notion_query([])

    b = compute_budget()

    assert b["total"] == 0.0
    assert b["top_category"] is None
    assert b["projected_over"] == 0.0


@responses.activate
def test_format_budget_matches_the_bot_reply(frozen_june_10):
    """The shared formatter must still produce the recap the `B` command sends."""
    rows = [expense(10.00, "Food"), expense(30.00, "Shopping")]
    notion_query(rows)
    notion_query(rows)

    assert format_budget(compute_budget()) == david.budget()
