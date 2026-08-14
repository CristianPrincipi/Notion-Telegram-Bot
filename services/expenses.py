"""Writing, changing, removing and restoring one month's expenses.

Moved out of david.py unchanged in every respect that matters: the Notion calls,
the lock, the ordering of the two round trips inside it, the undo snapshot, and
every message. What changed is how a message LEAVES: `update.message.reply_text`
became `notify`, and `telegram_text.reply` became `notify_md`. Nothing here knows
what a Telegram update is.

THE FOUR GUARDS THIS MODULE CARRIES, all from Hard Rule 4, none of them new:

  1. `sorts=CREATED_DESC` on the lookup, so "the first match" is the same row on
     two identical calls rather than whatever Notion felt like returning.
  2. the lookup is SCOPED to the current month, and REFUSED rather than widened
     when the month cannot be resolved.
  3. more than one match writes NOTHING and asks (expense_safety).
  4. every destructive write records its reversal, snapshotted from the page the
     LOOKUP returned, before it reports success.

And the lock covers the LOOKUP as well as the write, because the pair is a
find-then-mutate spanning two round trips. Splitting find from mutate is what
made it possible to lock only the second half — which reads as safe and is not.

WHERE THE PENDING CHOICE LIVES. `user_data` — the plain dict python-telegram-bot
keeps per user, handed in by the bot layer rather than reached through a
`context`. That is the whole of what this module needs from PTB, and a dict is
something a test can pass.
"""

import asyncio
import logging
import os

import expense_safety
from clients.calendar_client import now_local
from clients.notion_client import (
    CREATED_DESC, body_excerpt, notion_request, query_database, set_archived, update_page,
)
from config import EXPENSE_MONTH_RELATION
from services.month import current_month_id
from page_lock import WRITE_LOCK_TIMEOUT_SECONDS, PageBusy, page_lock
from telegram_text import escape_md

logger = logging.getLogger(__name__)

EXPENSES_ID = os.environ.get("EXPENSES_ID")


# --- EXPENSE WRITE SERIALISATION ---
# `U e` and `D e` are both find-then-mutate: query the Expenses DB by name, take
# results[0], then PATCH that page. The query and the PATCH are two round trips,
# and between them another run can read the SAME results[0].
#
# The delete case is the one that bites. Notion excludes archived pages from
# query results, which is what makes `D e Carrefour` twice in a row correctly
# delete two different rows — the second query no longer sees the first one.
# Overlap them and both queries run before either archive, both resolve to the
# same page, and both archive it. You are told "deleted successfully" twice and
# one row is still there.
#
# Locked on EXPENSES_ID rather than on the expense name: page_lock keys must be
# database ids so the lock table stays bounded (see page_lock.py), and expense
# names come from the user. Serialising every expense write costs nothing here —
# they take about a second and David has one user.
#
# `Add e` is deliberately NOT locked. It is a bare create with no preceding read,
# so it cannot double-target a row or lose an update. Locking it would only
# serialise it against the other two, which does not make "add X" and "delete X"
# sent at the same instant any less ambiguous than they already are.
BUSY_EXPENSE_MESSAGE = ("⏳ Another expense write is still running. "
                        "Give it a second and try again.")


# --- NOTION FUNCTIONS --- #
#
# Everything in this section is SYNCHRONOUS and makes blocking HTTP calls. None
# of it may be called directly from an `async def` — python-telegram-bot runs
# updates on one event loop, so a blocking call here stops every other command
# and every scheduled job for its whole duration. Call them with
#   await asyncio.to_thread(fn, ...)
# as the flows below do. They stay sync so they remain directly testable.


# --- NEW EXPENSES FUNCTION ---
def add_Expenses(name, amount, category):

    # --- GENERATE TODAY DATE ---
    # Europe/Rome, not the host clock: Railway runs UTC, so a naive now() files
    # anything logged after local midnight under YESTERDAY — and at a month
    # boundary, into the wrong month's budget entirely.
    today = now_local().strftime("%Y-%m-%d")

    # The month page, resolved now rather than read from MONTH_ID at import. That
    # is what fixes the other half of the same bug: the date was already right at
    # a month boundary, but the relation still pointed at the previous month's
    # page until the environment variable was updated by hand. See services/month.py.
    month_id = current_month_id()
    if not month_id:
        logger.error("add_Expenses: no month page resolved — send `Month`, or check `Diag`.")
        return False

    data = {
        "parent": {"database_id": EXPENSES_ID},
        "properties": {
            "Name": {
                "title": [{"text": {"content": name}}]},
            "Amount": {"number": amount},
            "Date": {"date": {"start": today}},
            "Category":{"multi_select": [{"name": category}]},
            EXPENSE_MONTH_RELATION: {"relation": [{"id": month_id}]}
        }
    }

    response = notion_request("POST", "https://api.notion.com/v1/pages", json=data)

    if response.status_code != 200:
        logger.error("add_Expenses failed: Notion %s: %s",
                     response.status_code, body_excerpt(response))

    return response.status_code == 200


# --- FINDING THE EXPENSE A DESTRUCTIVE COMMAND MEANS ---
#
# SPLIT OUT OF THE WRITES ON PURPOSE. `update_Expense` and `delete_Expense` used
# to do their own lookup and act on results[0], which made "which row did that
# hit?" unanswerable from outside them — there was no point between finding and
# mutating at which anything could be shown to you or counted. Both writes now
# take a page ID that something else chose, which is what lets the caller stop
# and ask when the choice is not obvious.
#
# TWO NARROWINGS, both closing a way to hit the wrong row:
#
#   sorts=CREATED_DESC   — "the first match" now means the most recent one, the
#                          same way on every call. See notion_client.
#   the month filter     — the search covers THIS month only, so `D e Coffee`
#                          cannot reach into last December for a coffee you have
#                          long since forgotten. It also matches how you think
#                          about expenses: the budget is monthly, so the row you
#                          mean is one of this month's.

def find_expense_matches(name):
    """Expenses in the CURRENT month whose Name contains `name`, newest first.

    Returns (pages, error) — full page objects, not IDs, because the caller
    needs their Amount, Date and Category both to tell two matches apart in the
    prompt and to snapshot the old values for `undo`. Those properties come back
    with the query, so carrying them costs no extra request.
    """
    month_id = current_month_id()
    if not month_id:
        # REFUSING BEATS WIDENING. Falling back to an unscoped search here would
        # silently restore the exact reach this filter exists to remove, and it
        # would do it precisely when David is least sure of its own state.
        return [], ("I could not work out which month page to search — "
                    "send `Month` to re-resolve it, or `Diag` to see why.")

    return query_database(
        EXPENSES_ID,
        filter_obj={"and": [
            {"property": "Name", "title": {"contains": name.strip()}},
            {"property": EXPENSE_MONTH_RELATION, "relation": {"contains": month_id}},
        ]},
        sorts=CREATED_DESC,
    )


# --- UPDATE EXPENSES FUNCTION ---
def update_Expense(page_id, amount, category):
    """Overwrite one expense's amount and category. Returns (ok, error)."""
    update_response = notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        json={"properties": {
            "Amount": {"number": amount},
            "Category": {"multi_select": [{"name": category}]},
        }},
    )

    if update_response.status_code != 200:
        logger.error("update_Expense(%s) failed: Notion %s: %s",
                     page_id, update_response.status_code, body_excerpt(update_response))
        return False, f"Notion {update_response.status_code}: {body_excerpt(update_response)}"

    return True, None


# --- DELETE EXPENSES FUNCTION ---
def delete_Expense(page_id):
    """Archive one expense. Returns (ok, error).

    Notion has no hard delete for an integration, which is what makes this
    reversible — `undo` sends the same page back with archived=False.
    """
    update_response = notion_request(
        "PATCH",
        f"https://api.notion.com/v1/pages/{page_id}",
        json={"archived": True},
    )

    if update_response.status_code != 200:
        logger.error("delete_Expense(%s) failed: Notion %s: %s",
                     page_id, update_response.status_code, body_excerpt(update_response))
        return False, f"Notion {update_response.status_code}: {body_excerpt(update_response)}"

    return True, None


# --- ADDING ONE --- #

async def run_add(name, amount, category, *, notify, notify_md=None):
    """`Add e [Name] [Amount] [Category]` — a bare create, no lookup, no lock."""
    await notify(f"⏳ Adding '{name}' (€{amount}) to Notion...")

    # CALL THE NOTION FUNCTION
    success = await asyncio.to_thread(add_Expenses, name, amount, category)

    if success:
        await notify("✅ Success! Expenses added to your database.")
    else:
        await notify("❌ Error: Could not connect to Notion. Check your API keys.")


# --- DESTRUCTIVE EXPENSE COMMANDS (`U e`, `D e`, `undo`) --- #
#
# Both destructive commands run the same three steps — find, choose, write —
# and differ only in which write they end at, so they share the pair below
# rather than each carrying its own copy of the ambiguity and undo handling.
# The state machine and every message live in expense_safety.py; what stays here
# is the Notion I/O and the locking.

async def run_destructive(user_data, action, name, amount=None, category=None,
                          *, notify, notify_md=None):
    """Resolve which expense `name` means, then either write or ask.

    THE ONE RULE: more than one match means NOTHING is written. A destructive
    command whose target is ambiguous is not a command yet, and guessing at it
    is the failure this whole path exists to remove — the write is cheap to
    repeat and the wrong write is expensive to notice.

    THE LOOKUP IS INSIDE THE LOCK, and has to be. This is a find-then-mutate
    spanning two round trips, so a second expense write slipping between them
    would let both resolve to the same row and archive it twice — the exact race
    page_lock.py's docstring names as the reason its keys are database ids.
    Only the single-match path writes here; the ambiguous one releases the lock
    and waits for a number, because holding it across a reply from you would
    stall every other expense command for as long as you took to answer.
    """
    notify_md = notify_md or notify

    if action == expense_safety.UPDATE:
        await notify(f"🔍 Finding '{name}' to update to €{amount} [{category}]...")
    else:
        await notify(f"🔍 Finding '{name}' to delete...")

    try:
        async with page_lock(EXPENSES_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            matches, err = await asyncio.to_thread(find_expense_matches, name)

            if err is None and len(matches) == 1:
                await _apply_destructive(
                    user_data, action, matches[0], amount, category,
                    notify=notify, notify_md=notify_md)
                return
    except PageBusy:
        await notify(BUSY_EXPENSE_MESSAGE)
        return

    if err:
        # An error is NOT an empty result: "Notion is down" and "you have no
        # Coffee this month" need opposite reactions, and reporting the first as
        # the second is how a failed lookup turns into "it wasn't there anyway".
        await notify_md(f"❌ Could not look up '{escape_md(name)}':\n{escape_md(err)}")
        return

    if not matches:
        await notify(f"❌ Error: no expense matching '{name}' this month.")
        return

    pending = expense_safety.remember_pending(
        user_data, action, name, matches, amount=amount, category=category)
    await notify_md(expense_safety.format_choices(pending))


async def _apply_destructive(user_data, action, page, amount, category,
                             *, notify, notify_md):
    """Make the write, and record how to reverse it. CALL UNDER THE EXPENSE LOCK.

    The undo snapshot is taken from `page` — the row as the lookup found it —
    and is therefore the state BEFORE this write, even though it is stored
    after. Re-reading the page afterwards would faithfully record the new amount
    as the old one, which is worse than having no undo at all.
    """
    choice   = expense_safety.choice_from_page(page)
    previous = (expense_safety.previous_properties(page)
                if action == expense_safety.UPDATE else None)

    if action == expense_safety.DELETE:
        success, err = await asyncio.to_thread(delete_Expense, choice.page_id)
    else:
        success, err = await asyncio.to_thread(
            update_Expense, choice.page_id, amount, category)

    if not success:
        verb = "delete" if action == expense_safety.DELETE else "update"
        await notify_md(f"❌ Could not {verb} '{escape_md(choice.name)}':\n{escape_md(err)}")
        return

    # Only now. An undo record for a write that failed would offer to reverse
    # something that never happened.
    expense_safety.remember_undo(user_data, action, choice.page_id, choice.name, previous)

    headline = (f"🗑️ Deleted *{escape_md(choice.name)}*"
                if action == expense_safety.DELETE else
                f"✅ Updated *{escape_md(choice.name)}* to €{amount:.2f} [{escape_md(category)}]")
    await notify_md(f"{headline}\n{expense_safety.format_undo_offer(action, choice.name)}")


async def run_selection(user_data, selection: int, *, notify, notify_md=None):
    """A bare number answering the numbered list of matches.

    No lookup runs here: the page was chosen from a list David printed, so this
    is a write against a known ID rather than a find-then-mutate. The lock is
    still taken, to keep it ordered against the other expense writes.
    """
    notify_md = notify_md or notify

    pending, page, err = expense_safety.take_pending(user_data, selection)
    if err:
        await notify(f"❌ {err}")
        return

    try:
        async with page_lock(EXPENSES_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            await _apply_destructive(user_data, pending.action, page,
                                     pending.amount, pending.category,
                                     notify=notify, notify_md=notify_md)
    except PageBusy:
        await notify(BUSY_EXPENSE_MESSAGE)


async def run_undo(user_data, *, notify, notify_md=None):
    """`undo` — reverse the last destructive expense write.

    Both branches are ordinary writes against a page ID David already holds, so
    neither re-runs a lookup: an undo that had to find its own target could pick
    a different row than the one it is undoing, which would make the recovery
    command a third way to hit the wrong expense.
    """
    notify_md = notify_md or notify

    undo, err = expense_safety.take_undo(user_data)
    if err:
        await notify(f"❌ {err}")
        return

    try:
        async with page_lock(EXPENSES_ID, timeout=WRITE_LOCK_TIMEOUT_SECONDS):
            if undo.action == expense_safety.DELETE:
                success, err = await asyncio.to_thread(set_archived, undo.page_id, False)
            else:
                success, err = await asyncio.to_thread(update_page, undo.page_id, undo.properties)
    except PageBusy:
        # Put it back: the reversal has not happened, so it must stay available.
        expense_safety.remember_undo(user_data, undo.action, undo.page_id,
                                     undo.name, undo.properties)
        await notify(BUSY_EXPENSE_MESSAGE)
        return

    if not success:
        expense_safety.remember_undo(user_data, undo.action, undo.page_id,
                                     undo.name, undo.properties)
        await notify_md(f"❌ Could not undo '{escape_md(undo.name)}':\n{escape_md(err)}")
        return

    await notify_md(expense_safety.format_undone(undo))
