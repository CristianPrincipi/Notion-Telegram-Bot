"""`v` — the adapter for services/version.py.

Runs INLINE, and it is the clearest case in the registry: four environment reads
and a string. Detaching it would add a task for work that cannot block.

PASSES ONLY `notify`. The pair is bound as usual, and `notify_md` is deliberately
dropped on the floor rather than forwarded: the reply carries a commit message,
which Markdown cannot be trusted to survive, and `run_version` has no parameter
to receive it anyway. See the comment on that function for why the plain channel
is the correct one here rather than a shortcut.

No splitting (see bot/long_messages.py): the reply is five short lines and cannot
grow with anything — a commit subject is the only variable-length part of it.
"""

from bot.notify import for_update
from services.version import run_version


async def cmd_version(update, context, args):
    await handle_version(update)


async def handle_version(update):
    """Bind this message's reply channel and run the command."""
    notify, _notify_md = for_update(update)
    await run_version(notify=notify)
