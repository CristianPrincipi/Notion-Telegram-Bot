"""The handlers whose feature module still takes `update` itself.

`B`, `Month`, `Diag`, `DBs`, `Find`, `Get` and `Remind` are one-liners because
budget.py, month.py, notion_ids.py, pkm.py and reminder.py have not been split
into a service and an adapter — they were out of scope for this refactor and are
named as follow-up work in PLAN.md. Until they are, the adapter for each is the
single line that forwards the update, and this module is where those lines live
rather than in david.py.

`B` is the exception that already reads right: budget.py is telegram-free today,
so this handler does the offloading and the formatting decision itself, which is
exactly what a bot-layer handler is supposed to do.
"""

import asyncio

from budget import budget
from month import handle_month
from notion_ids import handle_dbs, handle_diag, handle_find
from pkm import handle_get
from reminder import handle_remind
from telegram_text import reply


async def cmd_budget(update, context, args):
    result_text = await asyncio.to_thread(budget)
    if result_text:
        # format_budget escapes the Notion category names it interpolates.
        await reply(update, result_text)
    else:
        await update.message.reply_text("❌ Error: Could not calculate budget.")


async def cmd_diag(update, context, args):
    await handle_diag(update)


async def cmd_dbs(update, context, args):
    await handle_dbs(update)


async def cmd_month(update, context, args):
    await handle_month(update)


async def cmd_find(update, context, args):
    await handle_find(update, args["query"])


async def cmd_get(update, context, args):
    await handle_get(update, args["text"])


async def cmd_remind(update, context, args):
    await handle_remind(update, args["text"])
