"""Splitting a reply that is longer than Telegram will accept.

WHY IT IS HERE AND NOT IN A SERVICE. The 4096-character limit is a property of
Telegram, not of any answer David computes — a service builds one message and
says so once. But a service cannot import bot/, so the splitting is bound where
the notify pair itself is bound, in the adapter:

    notify, notify_md = for_update(update)
    await run_get(text, notify=partial(send_long, notify), notify_md=notify_md)

The service still calls its channel once with the whole text. Which channel
splits is the adapter's decision, and the two callers make it differently on
purpose: `Get` renders a Notion section as PLAIN text (arbitrary user content,
which Markdown cannot be trusted to survive), while the ID diagnostics build
David's own Markdown and split that.

THERE USED TO BE TWO OF THIS, one private to pkm.py and one to notion_ids.py,
differing in which sender they called and in one character of whitespace
handling. Merged here so a fix to the splitting is a fix to both.

KNOWN LIMITATION, mitigated rather than fixed and inherited from the copy in
notion_ids.py: the split point is chosen by LENGTH, so a long Markdown report
can be cut between an opening `*` and its closing one, leaving an unbalanced
entity in each half. escape_md protects the interpolated values but cannot
protect David's own formatting across a chunk boundary. Going through
telegram_text.reply means such a chunk degrades to plain text rather than being
dropped, which is why this is a limitation and not a bug that loses messages.
"""

# Under Telegram's 4096, with room for the entity overhead a Markdown chunk adds.
LIMIT = 3800


def split_for_telegram(text: str, limit: int = LIMIT) -> list[str]:
    """`text` as chunks under `limit`, broken on line boundaries.

    Line boundaries rather than characters because every caller sends rendered
    lists and reports: cutting mid-line splits a bullet or an ID across two
    messages, and a Notion ID you have to reassemble by hand to paste into
    Railway is the one thing these reports exist to hand you.

    A line longer than the limit on its own is NOT broken up — it goes out as
    its own oversized chunk and Telegram rejects it. That is the behaviour both
    copies had; nothing in David produces one (the longest single line is a
    64-character UUID in backticks), and inventing a hard split here would be a
    fix hidden inside a move.
    """
    if len(text) <= limit:
        return [text]

    chunks, chunk = [], ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > limit and chunk:
            chunks.append(chunk.rstrip("\n"))
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        chunks.append(chunk.rstrip("\n"))
    return chunks


async def send_long(send, text: str) -> None:
    """Send `text` through `send`, in as many messages as it takes.

    Shaped to be partial-applied into a notify slot: `partial(send_long, notify)`
    is itself a notify — one awaitable taking one string — so a service never
    learns that its reply was split.
    """
    for chunk in split_for_telegram(text):
        await send(chunk)
