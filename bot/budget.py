"""`B` — the monthly budget recap.

THE ONE HANDLER THAT NEVER NEEDED A SPLIT. budget.py has been telegram-free
since it was written: `budget()` returns `(text, error)` and knows nothing about
who is asking, which is why proactive/ has been reading its `compute_budget()`
for the morning pace tag all along. So this handler does the offloading and the
formatting decision itself — exactly what a bot-layer handler is for — rather
than forwarding an update into a module that would then have to send.

It lived in bot/commands.py, alongside the one-line delegators for the four
modules that DID still take an update. Those are split now and have adapters of
their own, so the file holding their placeholders has nothing left to hold.

budget.py itself stays at the repo root: it is not welded to Telegram, so it was
never part of that work.
"""

import asyncio

from budget import budget
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
