"""Binding one Telegram message to the callbacks a service reports through.

A service says what happened; this decides where it goes. Two channels, because
David already had two and the difference is load-bearing:

  notify(text)     plain. `update.message.reply_text`, no parse_mode, nothing
                   that can be rejected for bad markup. What a raw Notion error,
                   a PyPDF2 exception or a slice of an uploaded PDF goes out as.

  notify_md(text)  Markdown, through telegram_text.reply — so David's own
                   *bold* renders, every interpolated value is escaped at the
                   call site by the service that built the string, and a missed
                   escape degrades to plain text instead of vanishing.

WHY THE SERVICE PICKS, NOT THIS MODULE. Only the service knows whether the
string it just built contains its own markup or someone else's raw text. That is
the same reason escape_md is applied at the interpolation site rather than
inside the senders: a sender cannot tell David's `*bold*` from a stray asterisk
in an expense name. See telegram_text.py.

`notify_md` is optional everywhere it is consumed and defaults to `notify`, so a
caller with nothing to render Markdown — a test collecting into a list, a job
writing to the log — implements the whole interface with one function.
"""

from functools import partial

from telegram_text import reply


def for_update(update):
    """(notify, notify_md) for the message that triggered this handler.

    `notify` is the bound `reply_text` itself rather than a wrapper around it:
    there is nothing to add, and a wrapper would only be one more place for the
    kwargs to drift from what the handlers used to pass.
    """
    return update.message.reply_text, partial(reply, update)
