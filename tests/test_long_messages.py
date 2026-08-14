"""One splitter for Telegram's message limit, and the channel each adapter binds.

WHY THIS FILE EXISTS
--------------------
`pkm.py` and `notion_ids.py` each carried a private `_send_long`: the same
length-bounded, line-aligned splitter, differing only in which sender it called
and in one character of whitespace handling. Splitting those modules into
services made the duplication unkeepable — a service cannot import `bot/` — so
the splitter became `bot/long_messages.py` and the choice of WHICH channel
splits moved into each adapter.

That choice is not cosmetic, and it is the thing this file pins:

  `Get`         splits the PLAIN channel. A retrieved section is arbitrary
                Notion content, sent unformatted because Markdown cannot be
                trusted to survive it.
  `Diag`/`DBs`  split the MARKDOWN channel. Those reports are David's own
                markup, with every interpolated value escaped at the call site.

Nothing asserted either before the merge. The copies were only ever reached
through a fake Update, and no test built a message long enough to split — so
the merge could have quietly stopped splitting, or started splitting the wrong
channel, with the whole suite green.
"""

from functools import partial

import bot.pkm
from bot.long_messages import LIMIT, send_long, split_for_telegram
from conftest import FakeUpdate, run
from services import pkm


# ─── THE SPLITTER ──────────────────────────────────────────────────────────────

def test_a_short_message_is_left_exactly_as_it_is():
    """Not merely "under the limit" — IDENTICAL. The splitter sits on every reply
    these commands make, so a stray strip() would quietly reformat the short
    ones, which is every message in normal use."""
    text = "line one\nline two\n"

    assert split_for_telegram(text) == [text]


def test_a_long_message_is_split_into_sendable_chunks():
    lines = [f"line {i} " + "x" * 60 for i in range(200)]

    chunks = split_for_telegram("\n".join(lines))

    assert len(chunks) > 1, "a message well over the limit came back whole"
    assert all(len(chunk) <= LIMIT for chunk in chunks), [len(c) for c in chunks]


def test_nothing_is_dropped_and_nothing_is_reordered():
    """The failure that would be invisible in the chat: a splitter that loses the
    line straddling a boundary still produces plausible-looking messages."""
    lines = [f"line {i} " + "x" * 60 for i in range(200)]

    chunks = split_for_telegram("\n".join(lines))

    assert [ln for chunk in chunks for ln in chunk.split("\n")] == lines


def test_the_split_is_on_line_boundaries():
    """Cutting mid-line splits a bullet — or a Notion ID — across two messages,
    and an ID you have to reassemble by hand to paste into Railway is the one
    thing these reports exist to hand you."""
    lines = [f"`{i:032d}`" for i in range(400)]

    chunks = split_for_telegram("\n".join(lines))

    assert len(chunks) > 1
    for chunk in chunks:
        for line in chunk.split("\n"):
            assert line in lines, f"a line was cut in half: {line!r}"


def test_send_long_is_itself_a_notify():
    """`partial(send_long, notify)` has to be droppable into a notify slot — one
    awaitable taking one string — or the adapters cannot bind it."""
    said = []

    async def collect(text):
        said.append(text)

    run(send_long(collect, "just the one"))

    assert said == ["just the one"]


def test_the_splitter_is_bound_through_the_real_notify_pair():
    """`partial(send_long, notify)` must wrap what bot/notify.py hands out, not a
    second binding a test invented. Same reasoning as conftest.with_update."""
    from bot.notify import for_update

    update = FakeUpdate(text="anything")
    notify, _ = for_update(update)

    run(partial(send_long, notify)("through the real pair"))

    assert update.message.replied_with("through the real pair")


# ─── WHICH CHANNEL SPLITS ──────────────────────────────────────────────────────

def test_get_splits_the_plain_channel(monkeypatch):
    """End to end through the real adapter: a long section arrives in pieces, and
    none of them carries parse_mode."""
    section = [{"id": f"b-{i}", "type": "bulleted_list_item", "has_children": False,
                "bulleted_list_item": {
                    "rich_text": [{"plain_text": f"point {i} " + "detail " * 12}]}}
               for i in range(200)]
    monkeypatch.setattr(pkm, "search_page_in_db",
                        lambda db, name, exact=False: ({"id": "manual-1"}, None))
    monkeypatch.setattr(pkm, "get_children", lambda block_id: (
        [{"id": "h-1", "type": "heading_2", "has_children": False,
          "heading_2": {"rich_text": [{"plain_text": "Perfect Process"}]}}] + section, None))
    update = FakeUpdate(text="Get Perfect Process - Brain")

    run(bot.pkm.handle_get(update, update.message.text))

    assert update.message.replied_with("point 199"), "the tail of the section never arrived"
    assert len(update.message.replies) > 3, (
        "a section far over the limit went out in one message — Telegram would "
        "reject it outright")
    assert all(len(text) <= LIMIT for text, _ in update.message.replies)
    body = [kwargs for text, kwargs in update.message.replies if "point 199" in text]
    assert all("parse_mode" not in kwargs for kwargs in body), (
        "the retrieved section was sent as Markdown — it is arbitrary Notion "
        "content and cannot survive it")
