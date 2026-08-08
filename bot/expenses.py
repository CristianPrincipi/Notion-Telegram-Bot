"""The expense commands: `Add e`, `U e`, `D e`, `undo`, and a bare number.

The argument grammar lives here with the handlers that consume it — `AMOUNT` is
imported back into david.py's registry so the pattern and the parser that reads
its group cannot drift apart in two files.

Everything below stops at "what did the user type": which row a destructive
command means, whether it is ambiguous, and how to reverse it are decisions, and
decisions are services/expenses.py's.
"""

import expense_safety
from bot.notify import for_update
from config import CATEGORY_MAP, DEFAULT_CATEGORY, category_help
from services import expenses

# Accepts a dot or a comma decimal separator. Italian keyboards produce commas
# by reflex, and the old \d+\.?\d* matched "2" out of "2,20" and dropped the
# rest, recording EUR 2.00 as a success.
AMOUNT = r"(?P<amount>\d+(?:[.,]\d+)?)"


def parse_amount(raw: str):
    """Parse an amount written with either separator. Returns (amount, error)."""
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return None, f"❌ Error: '{raw}' is not a valid amount."
    if value <= 0:
        return None, f"❌ Error: amount must be greater than zero, got {value:g}."
    return value, None


def resolve_category(raw):
    """Map a category shortcut to its Notion name. Returns (category, error).

    An ABSENT category falls back to the default. A SUPPLIED but unrecognised one
    is an error — otherwise a typo silently files the expense under Food, which
    is indistinguishable from having meant the default. Mirrors how genre already
    behaves.
    """
    if raw is None or not raw.strip():
        return DEFAULT_CATEGORY, None
    category = CATEGORY_MAP.get(raw.strip().lower())
    if category is None:
        return None, (f"❌ Error: unknown category '{raw.strip()}'. "
                      f"Please use: {category_help()}")
    return category, None


async def cmd_add_expense(update, context, args):
    name = args["name"].strip()

    amount, err = parse_amount(args["amount"])
    if err:
        await update.message.reply_text(err)
        return

    # --- IF NAME = C -> CARREFOUR (case-insensitive, like the command itself)
    if name.lower() == "c": name = "Carrefour"

    # --- CATEGORY: absent -> default, supplied but unknown -> error
    category, err = resolve_category(args["category"])
    if err:
        await update.message.reply_text(err)
        return

    notify, notify_md = for_update(update)
    await expenses.run_add(name, amount, category, notify=notify, notify_md=notify_md)


async def cmd_update_expense(update, context, args):
    name = args["name"].strip()

    amount, err = parse_amount(args["amount"])
    if err:
        await update.message.reply_text(err)
        return

    category, err = resolve_category(args["category"])
    if err:
        await update.message.reply_text(err)
        return

    notify, notify_md = for_update(update)
    await expenses.run_destructive(
        context.user_data, expense_safety.UPDATE, name,
        amount=amount, category=category, notify=notify, notify_md=notify_md)


async def cmd_delete_expense(update, context, args):
    notify, notify_md = for_update(update)
    await expenses.run_destructive(
        context.user_data, expense_safety.DELETE, args["name"].strip(),
        notify=notify, notify_md=notify_md)


async def cmd_undo(update, context, args):
    notify, notify_md = for_update(update)
    await expenses.run_undo(context.user_data, notify=notify, notify_md=notify_md)


async def handle_selection(update, context, selection: int):
    """A bare number answering a printed list of matches.

    Not a Command: it depends on STATE rather than on the text, so david's
    dispatch loop checks for a live list before it walks the registry.
    """
    notify, notify_md = for_update(update)
    await expenses.run_selection(context.user_data, selection,
                                 notify=notify, notify_md=notify_md)
