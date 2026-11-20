"""A stand-in for Telegram's `Update` and context, for commands that arrive
from somewhere other than the chat — the Android assistant (bot/assistant.py).

David's handlers take `(update, context, args)` and answer through
`update.message.reply_text`. Rather than give every handler a second way to
answer, the assistant hands them this: the same three things, offering ONLY what
the handlers it may run actually use —

  - `message.reply_text(text, **kwargs)` sends the reply to the chat (CHAT_ID)
    through telegram_text.send — so Markdown the chat rejects is resent plain —
    and keeps a plain copy for the phone to speak (`spoken`). A reply the chat
    could not take at all is still spoken: the write already happened, and the
    phone is where you are looking;
  - `context.user_data`, a dict of the assistant's OWN, so nothing it runs can
    answer, close or overwrite a prompt open in the chat;
  - `context.bot` and `context.application`, the real ones.

Anything else a handler reached for would raise AttributeError — on purpose: the
commands that need more (a file, a numbered list) are refused by the assistant
before they get here, and a test names the ones it may run.
"""

import logging
import re

import telegram_text

logger = logging.getLogger(__name__)

# Legacy Markdown: a backslash-escaped marker is a literal, a bare one is markup.
_MARKDOWN_TOKEN = re.compile(r"\\([_*`\[])|[_*`]")


def plain(text: str, parse_mode=None) -> str:
    """`text` as it reads aloud: a Markdown reply without its markers."""
    if not parse_mode:
        return text
    return _MARKDOWN_TOKEN.sub(lambda m: m.group(1) or "", text)


class ChatMessage:
    def __init__(self, bot, chat_id, text):
        self._bot = bot
        self._chat_id = chat_id
        self.text = text
        self.caption = None
        self.document = None
        self.voice = None
        self.spoken = []

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.spoken.append(plain(text, parse_mode))
        try:
            # telegram_text.send, not bot.send_message: a reply whose Markdown the
            # chat rejects is resent plain, as on every other path (review of #79).
            await telegram_text.send(self._bot, self._chat_id, text,
                                     parse_mode=parse_mode, **kwargs)
        except Exception as e:
            # Anything else — Telegram down — must not turn a write that happened
            # into a failed request: the phone still hears it.
            logger.warning("Assistant reply not mirrored to the chat: %s", type(e).__name__)


class ChatUpdate:
    def __init__(self, message):
        self.message = message
        self.update_id = None


class ChatContext:
    def __init__(self, bot, application, user_data):
        self.bot = bot
        self.application = application
        self.user_data = user_data
