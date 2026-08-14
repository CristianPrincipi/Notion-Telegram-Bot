"""The handlers whose feature module still takes `update` itself.

`B` and `Month` are one-liners because budget.py and month.py have not been
split into a service and an adapter — they were out of scope for this refactor
and are named as follow-up work in PLAN.md. Until they are, the adapter for each
is the single line that forwards the update, and this module is where those
lines live rather than in david.py.

`Remind`, `Get`, `Diag`, `Find` and `DBs` used to be here too; reminder.py,
pkm.py and notion_ids.py are split, so their adapters are bot/reminder.py,
bot/pkm.py and bot/notion_ids.py — real ones, binding the notify pair.

`B` is the exception that already reads right: budget.py is telegram-free today,
so this handler does the offloading and the formatting decision itself, which is
exactly what a bot-layer handler is supposed to do.
"""

import asyncio

from budget import budget
from month import handle_month
from telegram_text import reply


async def cmd_budget(update, context, args):
    result_text, err = await asyncio.to_thread(budget)
    if result_text:
        # format_budget escapes the Notion category names it interpolates.
        await reply(update, result_text)
    else:
        # reply_text, not reply: `err` is Notion's own string and can carry `_`
        # or `*`, and it is the one message that must survive to be read. Same
        # reasoning as the three error reporters CLAUDE.md exempts.
        await update.message.reply_text(f"❌ Could not calculate budget: {err}")


async def cmd_month(update, context, args):
    await handle_month(update)
