"""The only safe way to send Telegram Markdown in David.

THE PROBLEM. Telegram's legacy Markdown has no notion of a literal asterisk. Any
value interpolated into a formatted reply — an expense name, a Notion category, a
heading Claude wrote, 300 characters sliced out of a PDF — can contain _ * ` or [,
and one unbalanced character makes Telegram reject the WHOLE message with
BadRequest. The write to Notion had already succeeded; only the confirmation was
lost, so the failure looked like "the bot ignored me".

TWO LAYERS, because either one alone leaks:

  1. escape_md() at every interpolation site. This is the real fix, and it is
     applied at the call site rather than inside the senders below — a sender
     cannot tell David's own `*bold*` from a stray asterisk in someone's data.

  2. reply()/send() retry once without parse_mode when Telegram rejects the
     markup. This is the safety net for the escape that someone forgets to add:
     the reply degrades to unformatted text instead of vanishing. The fallback
     logs a WARNING with a stack trace, so a gap announces itself with the call
     site attached rather than waiting to be noticed.

NOT EVERY SENDER BELONGS HERE. david.notify_error, david.on_error and
proactive.scheduler._report_error send plain text unconditionally and are exempt
on purpose — they wrap an exception in a `code span`, where Markdown v1 ignores
backslash escapes, so escaping cannot make them safe. See the comments there.
"""

import logging
import re

from telegram.error import BadRequest

logger = logging.getLogger(__name__)

# The four characters legacy Markdown treats as markup. Identical to the set
# telegram.helpers.escape_markdown uses for version=1, and to the _esc() that
# used to live privately in notion_ids.py.
_MARKDOWN_SPECIAL = re.compile(r"([_*`\[])")


def escape_md(text) -> str:
    """Escape the characters that would break Telegram legacy Markdown.

    Apply to every value interpolated into a Markdown message — anything from the
    user, from Claude, from Notion, from Google, or out of an uploaded file.

    Tolerant of None and non-strings so call sites do not each need a guard.

    NOTE the one place this does not work: inside a `code span`, Markdown v1 does
    not honour backslash escapes, so a backtick in the value still breaks out.
    Send those messages as plain text instead.
    """
    if text is None:
        return ""
    return _MARKDOWN_SPECIAL.sub(r"\\\1", str(text))


async def _send_once_then_plain(attempt, text, parse_mode):
    """Run `attempt(parse_mode)`, and on a markup rejection retry it plain."""
    if parse_mode is None:
        return await attempt(None)

    try:
        return await attempt(parse_mode)
    except BadRequest as err:
        # stack_info so the log names the call site whose value was not escaped —
        # the whole point of the warning is to be actionable.
        logger.warning(
            "Telegram rejected %s markup (%s) — resending as plain text. "
            "A value at this call site is not escaped with escape_md(). "
            "Message began: %r",
            parse_mode, err, text[:160],
            stack_info=True,
        )
        return await attempt(None)


async def reply(update, text, *, parse_mode="Markdown", **kwargs):
    """update.message.reply_text, with the plain-text fallback."""
    async def attempt(mode):
        return await update.message.reply_text(text, parse_mode=mode, **kwargs)

    return await _send_once_then_plain(attempt, text, parse_mode)


async def send(bot, chat_id, text, *, parse_mode="Markdown", **kwargs):
    """bot.send_message, with the plain-text fallback."""
    async def attempt(mode):
        return await bot.send_message(chat_id=chat_id, text=text, parse_mode=mode, **kwargs)

    return await _send_once_then_plain(attempt, text, parse_mode)
