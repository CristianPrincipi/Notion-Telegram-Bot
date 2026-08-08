"""What David actually does, with nothing about Telegram in it.

THE RULE, and it is the whole point of this package:

    Nothing under services/ may import telegram, and no service function may
    take `update` as a parameter.

It is enforced mechanically by tests/test_layering.py, not by discipline. An
`import telegram` here turns that test red the moment it is written.

WHY. A service welded to `update.message.reply_text` can only ever be run by a
bot handler. It cannot be driven from a test without building a fake Update, it
cannot be reused from a scheduled job, and it cannot be called from anything
else at all — so every one of those needs a second copy of the logic, and the
copies drift. `reminder.build_today_message` and its twin were exactly that:
dead duplicates of proactive/briefing.py's logic, carrying a bug fixed in one
copy and not the other.

HOW PROGRESS GETS OUT. A service reports by calling back, not by sending:

    async def run_something(..., *, notify, notify_md=None) -> None

`notify(text)` is plain text and is the only channel a caller must supply — a
bot handler passes `update.message.reply_text`, a test passes a list's
`append`, a scheduled job passes something that logs.

`notify_md(text)` is the Markdown channel and DEFAULTS TO `notify`. It exists
because these services already sent two kinds of message and the difference is
load-bearing: David's own `*bold*` goes out with parse_mode and every
interpolated value escaped (telegram_text.escape_md), while a raw Notion error
or a slice of an uploaded PDF goes out as plain text precisely because escaping
cannot save it. Collapsing the two would have changed which messages Telegram
can reject. A caller that supplies only `notify` gets everything as text, which
is what makes a list's append a complete implementation of this interface.

WHAT A SERVICE STILL OWNS: the (value, error) convention, append-then-delete
write ordering, the page locks, and running every blocking call through
asyncio.to_thread. Those are properties of the work, not of the transport, and
they do not move up into bot/.
"""
