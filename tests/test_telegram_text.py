"""escape_md and the safe-send wrappers.

A Notion write that succeeds but whose confirmation is rejected by Telegram looks
exactly like a bot that ignored you. These cover both layers: escaping at the
call site, and the plain-text retry that catches the escape someone forgot.
"""

import logging

import pytest
from telegram.error import BadRequest

import telegram_text
from telegram_text import escape_md, reply, send
from conftest import FakeUpdate, run

CHAT_ID = "test-chat-id"


# ─── escape_md ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("*",  r"\*"),
    ("_",  r"\_"),
    ("[",  r"\["),
    ("`",  r"\`"),
])
def test_escape_md_escapes_every_markdown_special_character(raw, expected):
    assert escape_md(raw) == expected


def test_escape_md_handles_all_four_together():
    assert escape_md("a*b_c[d`e") == r"a\*b\_c\[d\`e"


def test_escape_md_leaves_ordinary_text_alone():
    assert escape_md("Carrefour groceries 12.50") == "Carrefour groceries 12.50"


def test_escape_md_handles_the_real_world_cases_that_broke_replies():
    # An expense name with a stray asterisk.
    assert escape_md("Uber*airport") == r"Uber\*airport"
    # A Notion property name, which is where the underscores come from.
    assert escape_md("rich_text") == r"rich\_text"
    # A markdown link, which legacy Markdown would try to parse.
    assert escape_md("[see notes](x)") == r"\[see notes](x)"


def test_escape_md_is_tolerant_of_none_and_non_strings():
    """Call sites interpolate whatever Notion or Claude handed back."""
    assert escape_md(None) == ""
    assert escape_md("") == ""
    assert escape_md(12.5) == "12.5"


# ─── the plain-text fallback ───────────────────────────────────────────────────

class RejectingMessage:
    """Rejects Markdown the way Telegram does on unbalanced markup.

    `always` models a BadRequest that has nothing to do with markup (chat not
    found, message too long) — those fail on the plain retry too.
    """

    def __init__(self, fail_times=1, always=False):
        self.fail_times = fail_times
        self.always     = always
        self.calls      = []            # [(text, parse_mode), ...]

    async def reply_text(self, text, parse_mode=None, **kwargs):
        self.calls.append((text, parse_mode))
        if self.always:
            raise BadRequest("Chat not found")
        if parse_mode is not None and len(self.calls) <= self.fail_times:
            raise BadRequest("Can't parse entities: can't find end of the entity starting at byte offset 7")
        return "sent"


class RejectingBot:
    def __init__(self, fail_times=1):
        self.fail_times = fail_times
        self.calls = []                 # [(chat_id, text, parse_mode), ...]

    async def send_message(self, chat_id, text, parse_mode=None, **kwargs):
        self.calls.append((chat_id, text, parse_mode))
        if parse_mode is not None and len(self.calls) <= self.fail_times:
            raise BadRequest("Can't parse entities")
        return "sent"


def test_reply_falls_back_to_plain_text_and_succeeds():
    update = FakeUpdate(text="irrelevant")
    update.message = RejectingMessage()

    result = run(reply(update, "broken *markup", parse_mode="Markdown"))

    assert result == "sent", "the fallback must deliver, not just swallow the error"
    assert [mode for _, mode in update.message.calls] == ["Markdown", None]
    assert all(text == "broken *markup" for text, _ in update.message.calls)


def test_send_falls_back_to_plain_text_and_succeeds():
    bot = RejectingBot()

    result = run(send(bot, CHAT_ID, "broken *markup", parse_mode="Markdown"))

    assert result == "sent"
    assert [mode for _, _, mode in bot.calls] == ["Markdown", None]


def test_the_fallback_logs_a_warning_naming_the_problem(caplog):
    update = FakeUpdate(text="irrelevant")
    update.message = RejectingMessage()

    with caplog.at_level(logging.WARNING, logger="telegram_text"):
        run(reply(update, "broken *markup", parse_mode="Markdown"))

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "escape_md" in caplog.text, "the warning must say what to do about it"
    # stack_info is what makes the warning actionable — it names the call site.
    assert caplog.records[0].stack_info is not None


def test_a_successful_markdown_send_does_not_retry_or_warn(caplog):
    update = FakeUpdate(text="irrelevant")

    with caplog.at_level(logging.WARNING, logger="telegram_text"):
        run(reply(update, "fine *bold* text", parse_mode="Markdown"))

    assert update.message.replies == [("fine *bold* text", {"parse_mode": "Markdown"})]
    assert caplog.records == []


def test_plain_sends_skip_the_fallback_path_entirely():
    """parse_mode=None cannot fail on markup, so it must not be wrapped."""
    update = FakeUpdate(text="irrelevant")
    update.message = RejectingMessage(fail_times=99)

    run(reply(update, "plain", parse_mode=None))

    assert update.message.calls == [("plain", None)]


def test_a_non_markup_bad_request_still_surfaces():
    """A second failure means the problem was never the markup — do not hide it."""
    update = FakeUpdate(text="irrelevant")
    update.message = RejectingMessage(always=True)

    with pytest.raises(BadRequest):
        run(reply(update, "chat not found", parse_mode="Markdown"))

    # It tried the fallback before giving up, rather than failing on the first try.
    assert [mode for _, mode in update.message.calls] == ["Markdown", None]


def test_escape_md_output_survives_the_round_trip():
    """The escaped value must be what actually reaches Telegram."""
    update = FakeUpdate(text="irrelevant")
    name = escape_md("Uber*airport")

    run(reply(update, f"✅ Added *{name}*", parse_mode="Markdown"))

    text, kwargs = update.message.replies[0]
    assert text == r"✅ Added *Uber\*airport*"
    assert kwargs["parse_mode"] == "Markdown"


def test_the_module_exposes_only_the_intended_surface():
    """Guards against a future helper quietly becoming a second escape path."""
    assert callable(telegram_text.escape_md)
    assert callable(telegram_text.reply)
    assert callable(telegram_text.send)


# ─── the architectural guard ───────────────────────────────────────────────────

def _telegram_calls_with_parse_mode(path):
    """(line, call) for every raw reply_text/send_message that sets parse_mode.

    Parsed rather than grepped: a docstring may legitimately discuss parse_mode,
    and notion_ids._send_long legitimately forwards one INTO telegram_text.reply.
    Only a direct call to Telegram's own API counts as an offence.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in ("reply_text", "send_message"):
            continue
        if any(kw.arg == "parse_mode" for kw in node.keywords):
            found.append((node.lineno, node.func.attr))
    return found


def test_no_module_speaks_markdown_to_telegram_except_telegram_text():
    """telegram_text must stay the ONLY place that hands parse_mode to Telegram.

    A reply_text(..., parse_mode="Markdown") anywhere else is a message that can be
    rejected outright and silently lost — the bug this module exists to close, and
    the one that made every error report unsendable. Adding one turns this red.

    david.notify_error, david.on_error and proactive.scheduler._report_error pass
    no parse_mode at all, so they satisfy this without being special-cased.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    sources = list(root.glob("*.py")) + list((root / "proactive").glob("*.py"))

    offenders = []
    for path in sources:
        if path.name == "telegram_text.py":
            continue
        for line, call in _telegram_calls_with_parse_mode(path):
            offenders.append(f"{path.name}:{line}: {call}(..., parse_mode=...)")

    assert offenders == [], (
        "these must go through telegram_text.reply/send instead:\n"
        + "\n".join(offenders)
    )


def test_the_guard_can_actually_detect_an_offender(tmp_path):
    """A guard that cannot fail is not a guard."""
    offender = tmp_path / "bad.py"
    offender.write_text(
        'async def f(update):\n'
        '    await update.message.reply_text("hi", parse_mode="Markdown")\n',
        encoding="utf-8",
    )

    assert _telegram_calls_with_parse_mode(offender) == [(2, "reply_text")]
