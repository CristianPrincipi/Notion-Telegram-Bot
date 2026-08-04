"""Access control — only the owner's messages reach handler code.

These tests exercise the REAL python-telegram-bot filter against REAL Update
objects, because that is where authorization actually lives. Asserting that
handle_message ignores a stranger would prove nothing: the whole design is that
a stranger's update is dropped by the dispatcher and never gets that far.
"""

import logging
from datetime import datetime, timezone

import pytest
from telegram import Chat, Document, Message, Update
from telegram import User as TelegramUser
from telegram.ext import ApplicationBuilder

import david
from conftest import OWNER_ID, FakeContext, run

STRANGER_ID = 999999999

owner_only = david.build_owner_filter(OWNER_ID)


def make_update(user_id, text="B", caption=None, document=None):
    """A real telegram.Update, as PTB would hand to the dispatcher."""
    message = Message(
        message_id=1,
        date=datetime.now(timezone.utc),
        chat=Chat(id=user_id, type=Chat.PRIVATE),
        from_user=TelegramUser(id=user_id, is_bot=False, first_name="Test"),
        text=text,
        caption=caption,
        document=document,
    )
    return Update(update_id=1, message=message)


# ─── THE FILTER ────────────────────────────────────────────────────────────────

def test_owner_passes_the_filter():
    assert owner_only.check_update(make_update(OWNER_ID))


def test_stranger_fails_the_filter():
    assert not owner_only.check_update(make_update(STRANGER_ID))


@pytest.mark.parametrize("text", [
    "B",                                # budget — reads Notion
    "Add e Carrefour 2.20",             # writes to Notion
    "D e Carrefour",                    # deletes from Notion
    "Diag",                             # leaks database IDs
    "DBs",                              # leaks every database the integration sees
    "Learn video https://youtu.be/x",   # spends Anthropic quota
    "h",                                # even the help text is owner-only
])
def test_no_command_is_reachable_by_a_stranger(text):
    """Every command costs the owner money, data, or privacy. None is public."""
    assert not owner_only.check_update(make_update(STRANGER_ID, text=text))


def test_the_catch_all_filter_is_the_exact_complement():
    """~owner_only must catch everything the owner filter does not, and nothing else.

    If these two ever overlap, the owner's own messages would also hit the
    unauthorized logger; if they leave a gap, a stranger's message is silently
    dropped with no log line at all.
    """
    catch_all = ~owner_only

    assert catch_all.check_update(make_update(STRANGER_ID))
    assert not catch_all.check_update(make_update(OWNER_ID))


# ─── THE REGISTERED HANDLERS ───────────────────────────────────────────────────
# These drive the handlers david.register_handlers() actually attaches, rather
# than filters rebuilt inside the test. Dropping `& owner_only` from a real
# registration has to fail here — a test that recomposes the filter itself would
# happily keep passing.

PDF = Document(file_id="f", file_unique_id="u", mime_type="application/pdf", file_size=10)


@pytest.fixture
def registered():
    """The real Application, wired exactly as __main__ wires it.

    ApplicationBuilder does no network I/O at build time, so this is offline.
    """
    application = ApplicationBuilder().token("123456:test-token").build()
    david.register_handlers(application, OWNER_ID)
    return application


def matching_callbacks(application, update):
    """Names of the handler callbacks that would fire for this update.

    Mirrors how PTB dispatches: it walks each group in order and stops at the
    first handler in that group whose filter matches.
    """
    fired = []
    for group in sorted(application.handlers):
        for handler in application.handlers[group]:
            if handler.check_update(update):
                fired.append(handler.callback.__name__)
                break
    return fired


@pytest.mark.parametrize("update, expected", [
    (make_update(OWNER_ID, text="B"), "handle_message"),
    (make_update(OWNER_ID, text="Add e Carrefour 2.20"), "handle_message"),
    (make_update(OWNER_ID, text=None, caption="Learn pdf", document=PDF), "handle_document"),
], ids=["owner text", "owner expense", "owner upload"])
def test_owner_reaches_the_real_handlers(registered, update, expected):
    assert matching_callbacks(registered, update) == [expected]


@pytest.mark.parametrize("update", [
    make_update(STRANGER_ID, text="B"),
    make_update(STRANGER_ID, text="Add e Sneaky 100"),
    make_update(STRANGER_ID, text="D e Carrefour"),
    make_update(STRANGER_ID, text="DBs"),
    make_update(STRANGER_ID, text=None, caption="Learn pdf", document=PDF),
], ids=["budget", "write expense", "delete expense", "list databases", "upload"])
def test_stranger_reaches_only_the_unauthorized_logger(registered, update):
    """THE security property: no handler but the logger ever sees a stranger."""
    assert matching_callbacks(registered, update) == ["handle_unauthorized"]


def test_every_registered_handler_except_the_logger_is_owner_gated(registered):
    """A newly added handler that forgets `& owner_only` fails here."""
    stranger = make_update(STRANGER_ID, text="anything", caption="anything", document=PDF)

    for group in registered.handlers.values():
        for handler in group:
            if handler.callback is david.handle_unauthorized:
                continue
            assert not handler.check_update(stranger), (
                f"{handler.callback.__name__} accepts messages from a stranger — "
                "it is missing the owner filter"
            )


def test_the_unauthorized_logger_is_registered_last(registered):
    """If it came first it would swallow the owner's messages too."""
    callbacks = [h.callback.__name__ for group in registered.handlers.values() for h in group]

    assert callbacks[-1] == "handle_unauthorized"


# ─── THE UNAUTHORIZED HANDLER ──────────────────────────────────────────────────

def test_unauthorized_attempt_is_logged_at_warning(caplog):
    update = make_update(STRANGER_ID, text="Add e Sneaky 100")

    with caplog.at_level(logging.WARNING):
        run(david.handle_unauthorized(update, FakeContext()))

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert str(STRANGER_ID) in caplog.text


def test_unauthorized_log_records_what_was_attempted(caplog):
    update = make_update(STRANGER_ID, text="Add e Sneaky 100")

    with caplog.at_level(logging.WARNING):
        run(david.handle_unauthorized(update, FakeContext()))

    assert "Add e Sneaky 100" in caplog.text


def test_unauthorized_handler_does_not_reply(caplog):
    """Silence on purpose: a reply confirms the bot is live to whoever probed it."""
    update = make_update(STRANGER_ID)
    context = FakeContext()

    with caplog.at_level(logging.WARNING):
        run(david.handle_unauthorized(update, context))

    assert context.bot.sent == []


def test_unauthorized_handler_truncates_long_content(caplog):
    """A flood of junk must not blow up the logs."""
    update = make_update(STRANGER_ID, text="x" * 5000)

    with caplog.at_level(logging.WARNING):
        run(david.handle_unauthorized(update, FakeContext()))

    assert len(caplog.text) < 500


def test_unauthorized_handler_logs_a_caption_when_there_is_no_text(caplog):
    pdf = Document(file_id="f", file_unique_id="u", mime_type="application/pdf", file_size=10)
    update = make_update(STRANGER_ID, text=None, caption="Learn pdf", document=pdf)

    with caplog.at_level(logging.WARNING):
        run(david.handle_unauthorized(update, FakeContext()))

    assert "Learn pdf" in caplog.text


def test_unauthorized_handler_survives_a_message_with_no_content(caplog):
    """A sticker or a join event has neither text nor caption — must not crash."""
    update = make_update(STRANGER_ID, text=None)

    with caplog.at_level(logging.WARNING):
        run(david.handle_unauthorized(update, FakeContext()))

    assert len(caplog.records) == 1
