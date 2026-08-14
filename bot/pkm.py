"""`Get [Topic] - [Area]` — the adapter for services/pkm.py.

Runs INLINE, not detached: it is read-only, so it cannot reorder against a write
the way a detached command could. The slow case is a toggle manual (Diet), where
build_index walks the tree one Notion request per heading — that is bounded
inside the service, on a worker thread.

THE PLAIN CHANNEL IS THE ONE THAT SPLITS HERE. A retrieved section is arbitrary
Notion content and goes out unformatted, and it is the only reply long enough to
exceed Telegram's limit — a whole manual's topic tree, or a section with forty
bullets. David's own framing stays on notify_md, unsplit, because it is short by
construction.
"""

from functools import partial

from bot.long_messages import send_long
from bot.notify import for_update
from services.pkm import run_get


async def cmd_get(update, context, args):
    await handle_get(update, args["text"])


async def handle_get(update, user_text: str):
    """Bind this message's reply channels and run the command."""
    notify, notify_md = for_update(update)
    await run_get(user_text, notify=partial(send_long, notify), notify_md=notify_md)
