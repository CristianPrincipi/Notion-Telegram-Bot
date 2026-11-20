"""The Android assistant: a sentence over HTTP, read as a command — through the real
routes, the real registry and router, with Claude scripted and the handlers' side
effects spied.

What is asserted is what the phone is NOT trusted with: a write never runs
without /assistant/confirm and the id it was given, a refused command never runs,
a lapsed or foreign id runs nothing, the token is checked on every route, and an
assistant run never interleaves with a chat message. And that what it does is
said twice: aloud (`say`) and in the chat.
"""
# Fixtures imported by name are injected as parameters, which ruff reads as a
# redefinition of the import.
# ruff: noqa: F811

import asyncio
import dataclasses

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import david
import pending_choice
from bot import assistant, chat_target, http_server
from conftest import FakeApplication, FakeBot, FakeUpdate, FakeContext, run
from test_router import NO_HANDLER, router  # noqa: F401 — a fixture `spies` needs
from test_sentences import claude, spies  # noqa: F401 — fixtures

TOKEN = "assistant-secret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CHAT = "chat-1"


class Phone:
    """The assistant's routes on a test server, and the bot its chat goes to."""

    def __init__(self, router=None):
        self.bot = FakeBot()
        self.assistant = assistant.Assistant(
            router=router or david.ASSISTANT_ROUTER, bot=self.bot, chat_id=CHAT,
            application=FakeApplication())

    def post(self, path, body=None, *, headers=AUTH, raw=None):
        async def go():
            app = web.Application()
            assistant.add_routes(app, token=TOKEN, assistant=self.assistant)
            async with TestClient(TestServer(app)) as client:
                if raw is not None:
                    resp = await client.post(path, headers=headers, data=raw)
                else:
                    resp = await client.post(path, headers=headers, json=body)
                return resp.status, await resp.json()
        return run(go())

    def ask(self, text, pending_id=None):
        body = {"text": text} if pending_id is None else {"text": text, "id": pending_id}
        return self.post("/assistant/ask", body)

    def confirm(self, pending_id):
        return self.post("/assistant/confirm", {"id": pending_id})

    @property
    def chat(self):
        return [text for _, text in self.bot.sent]


# ─── THE GATE ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/assistant/ask", "/assistant/confirm"])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"},
                                     {"Authorization": TOKEN}],
                         ids=["none", "wrong", "no bearer"])
def test_a_wrong_or_missing_token_is_401_and_nothing_is_read(spies, claude, path, headers):
    status, body = Phone().post(path, {"text": "B", "id": "x"}, headers=headers)

    assert status == 401
    assert claude.calls == []
    assert spies.chain == NO_HANDLER


@pytest.mark.parametrize("raw, body", [("not json", None), (None, ["a list"]),
                                       (None, {"text": 12})],
                         ids=["not json", "not an object", "text not a string"])
def test_a_malformed_body_is_400(spies, claude, raw, body):
    status, _ = Phone().post("/assistant/ask", body, raw=raw)

    assert status == 400
    assert claude.calls == []


# ─── A READ RUNS, A WRITE WAITS ───────────────────────────────────────────────

def test_a_read_runs_at_once_and_is_said(spies, claude):
    claude.says("B")
    phone = Phone()

    status, body = phone.ask("quanto mi resta questo mese?")

    assert status == 200
    assert spies.chain == "budget"
    assert set(body) == {"say"}
    assert body["say"] and not body["say"].startswith("▶️")


def test_a_write_is_proposed_and_runs_only_on_confirm(spies, claude):
    claude.says("Add e Carrefour 12 f")
    phone = Phone()

    _, proposed = phone.ask("ho speso 12 euro da Carrefour")

    assert spies.chain == NO_HANDLER
    assert proposed["confirm"] == "Add e Carrefour 12 f"
    assert "Add e Carrefour 12 f" in proposed["say"]
    assert phone.chat == []                     # nothing done, nothing to show

    status, done = phone.confirm(proposed["id"])

    assert status == 200
    assert spies.chain == "add_Expenses"
    assert spies.parsed_args == {"name": "Carrefour", "amount": 12.0, "category": "Food"}
    assert "say" in done


def test_a_confirmed_write_runs_once(spies, claude):
    claude.says("Add e Carrefour 12 f")
    phone = Phone()
    _, proposed = phone.ask("ho speso 12 euro da Carrefour")

    phone.confirm(proposed["id"])
    _, again = phone.confirm(proposed["id"])

    assert again == {"say": assistant.LAPSED}
    assert [name for name, _ in spies.calls].count("add_Expenses") == 1


@pytest.mark.parametrize("which", ["unknown", "missing", "lapsed", "replaced"])
def test_a_confirm_that_is_not_the_waiting_write_runs_nothing(spies, claude, which,
                                                               monkeypatch):
    claude.says("Add e Carrefour 12 f")
    phone = Phone()
    _, proposed = phone.ask("ho speso 12 euro da Carrefour")
    pending_id = {"unknown": "not-the-id", "missing": None}.get(which, proposed["id"])
    if which == "lapsed":
        monkeypatch.setattr(pending_choice, "is_expired", lambda expires_at, now=None: True)
    if which == "replaced":
        claude.says("B")
        phone.ask("quanto mi resta?")
        spies.calls.clear()

    _, body = phone.confirm(pending_id)

    assert body == {"say": assistant.LAPSED}
    assert "add_Expenses" not in [name for name, _ in spies.calls]


def test_a_clarifying_question_is_asked_and_its_answer_read_with_the_sentence(
        spies, claude):
    claude.says(question="Quanto hai speso?")
    claude.says("Add e Carrefour 12 f")
    phone = Phone()

    _, asked = phone.ask("ho fatto la spesa da Carrefour")
    _, proposed = phone.ask("dodici euro", asked["id"])

    assert asked["ask"] == asked["say"] == "Quanto hai speso?"
    _, second_user_message, _ = claude.calls[1]
    assert "ho fatto la spesa da Carrefour" in second_user_message
    assert second_user_message.endswith("dodici euro")
    assert proposed["confirm"] == "Add e Carrefour 12 f"
    assert spies.chain == NO_HANDLER


def test_an_answer_to_a_question_no_longer_open_is_not_read(spies, claude):
    phone = Phone()

    _, body = phone.ask("dodici euro", "an-old-id")

    assert body == {"say": assistant.LAPSED}
    assert claude.calls == []


# ─── WHAT IS NOT RUN ──────────────────────────────────────────────────────────

ALLOWED = {"Add b", "Add q", "Remind", "Agenda", "Add e", "B", "Month", "Diag",
           "Find", "DBs", "Get", "v", "h"}


def test_the_commands_the_assistant_may_run_are_exactly_these():
    """A new command is decided in david.ASSISTANT_REFUSED, not by default."""
    assert {c.name for c in david.COMMANDS} - set(david.ASSISTANT_REFUSED) == ALLOWED


@pytest.mark.parametrize("proposal", ["D e Carrefour", "U e Carrefour 3", "Cancel Dentista",
                                      "undo", "Menu", "Learn video https://youtu.be/x",
                                      "Implement Atomic Habits - Brain"])
def test_a_refused_command_says_why_and_runs_nothing(spies, claude, proposal):
    claude.says(proposal)

    _, body = Phone().ask("fai questa cosa")

    assert spies.chain == NO_HANDLER
    assert set(body) == {"say"}
    assert "Telegram" in body["say"]


@pytest.mark.parametrize("proposal", ["", "Add e Carrefour", "hello there"],
                         ids=["nothing", "no amount", "not a command"])
def test_a_proposal_that_is_not_exactly_one_command_runs_nothing(spies, claude, proposal):
    claude.says(proposal)

    _, body = Phone().ask("boh")

    assert body == {"say": assistant.DIDNT_GET_IT}
    assert spies.chain == NO_HANDLER


def test_a_proposal_matching_two_commands_runs_neither(spies, claude):
    """The registry never lets one text match two commands (test_router); this is
    the assistant's own check, should that ever slip."""
    claude.says("B")
    b = next(c for c in david.COMMANDS if c.name == "B")
    phone = Phone(assistant.Router(**{**david.ASSISTANT_ROUTER.__dict__,
                                      "matching": lambda text: [b, b]}))

    _, body = phone.ask("quanto mi resta?")

    assert body == {"say": assistant.DIDNT_GET_IT}
    assert spies.chain == NO_HANDLER


def test_a_model_failure_is_said_and_never_a_write(spies, claude):
    claude.fails("Daily Anthropic budget reached")

    _, body = Phone().ask("ho speso 12 euro da Carrefour")

    assert "Daily Anthropic budget reached" in body["say"]
    assert spies.chain == NO_HANDLER


@pytest.mark.parametrize("text", ["", "   ", "x" * 501], ids=["empty", "blank", "too long"])
def test_an_empty_or_long_text_never_reaches_claude(spies, claude, text):
    _, body = Phone().ask(text)

    assert body == {"say": assistant.DIDNT_GET_IT}
    assert claude.calls == []


# ─── SAID IN THE CHAT TOO ─────────────────────────────────────────────────────

def test_what_runs_is_shown_in_the_chat(spies, claude):
    claude.says("Add e Carrefour 12 f")
    phone = Phone()
    _, proposed = phone.ask("ho speso 12 euro da Carrefour")

    phone.confirm(proposed["id"])

    assert phone.chat[0] == "🗣️ From the assistant:"
    assert phone.chat[1] == "▶️ Add e Carrefour 12 f"
    assert {chat_id for chat_id, _ in phone.bot.sent} == {CHAT}


def test_a_chat_that_cannot_be_reached_does_not_turn_a_write_into_an_error(spies, claude):
    class DownBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            raise RuntimeError("Telegram is down")

    claude.says("Add e Carrefour 12 f")
    phone = Phone()
    phone.bot = phone.assistant.bot = DownBot()
    _, proposed = phone.ask("ho speso 12 euro da Carrefour")

    status, body = phone.confirm(proposed["id"])

    assert status == 200 and body["say"]
    assert spies.chain == "add_Expenses"


def test_a_markdown_reply_the_chat_rejects_is_sent_plain():
    """Review of #79: the stand-in swallowed Telegram's BadRequest, so a reply
    with an unescaped `_` was spoken but never reached the chat — telegram_text's
    plain-text retry could not run inside it."""
    from telegram.error import BadRequest

    class StrictBot(FakeBot):
        async def send_message(self, chat_id, text, **kwargs):
            if kwargs.get("parse_mode"):
                raise BadRequest("Can't parse entities")
            await super().send_message(chat_id, text, **kwargs)

    bot = StrictBot()
    message = chat_target.ChatMessage(bot, CHAT, "B")

    run(message.reply_text("*Budget* for my_book", parse_mode="Markdown"))

    assert bot.sent == [(CHAT, "*Budget* for my_book")]
    assert message.spoken == ["Budget for mybook"]


def test_a_markdown_reply_is_said_without_its_markers():
    assert chat_target.plain(r"*Budget*: 120\_50 `left`", "Markdown") == "Budget: 120_50 left"
    assert chat_target.plain("*not markdown*", None) == "*not markdown*"


def test_the_assistant_has_its_own_user_data(spies, claude):
    """Nothing it runs can answer or close a prompt open in the chat."""
    claude.says("B")
    phone = Phone()
    seen = []
    original = david.run_composed

    async def recording(composed, update, context):
        seen.append(context.user_data)
        await original(composed, update, context)

    router = assistant.Router(**{**david.ASSISTANT_ROUTER.__dict__, "run_composed": recording})
    phone.assistant.router = router
    phone.ask("quanto mi resta?")

    assert seen == [phone.assistant.user_data]
    assert seen[0] is not phone.assistant.application


# ─── HARD RULE 5: ONE AT A TIME WITH THE CHAT ─────────────────────────────────

def test_an_assistant_run_never_interleaves_with_a_chat_message(monkeypatch, claude):
    """PTB runs one chat update at a time; the assistant arrives outside that
    queue. Without the dispatch lock a chat `B` and an assistant `Add e`
    interleave at every await, and `B` can report a total half-written."""
    order = []

    def slow(name):
        async def handler(update, context, args):
            order.append(f"{name} start")
            await asyncio.sleep(0.05)
            order.append(f"{name} end")
        return handler

    who = {"B": "chat B", "Add e": "assistant Add e"}
    monkeypatch.setattr(david, "COMMANDS", [
        dataclasses.replace(c, handler=slow(who[c.name])) if c.name in who else c
        for c in david.COMMANDS])
    claude.says("Add e Carrefour 12 f")
    phone = Phone()
    proposed = run(phone.assistant.ask("ho speso 12 euro da Carrefour"))

    async def both():
        await asyncio.gather(
            phone.assistant.confirm(proposed["id"]),
            david.handle_message(FakeUpdate(text="B"), FakeContext()))

    run(both())

    assert order in (
        ["assistant Add e start", "assistant Add e end", "chat B start", "chat B end"],
        ["chat B start", "chat B end", "assistant Add e start", "assistant Add e end"])


def test_the_chat_header_waits_for_the_lock_too(spies):
    """Review of #79: the header was sent before the lock, so it could land in
    the middle of a chat command's replies."""
    phone = Phone()
    composed = assistant.Composed("B", "B", {})

    async def go():
        lock = david.dispatch_lock()
        await lock.acquire()
        task = asyncio.create_task(phone.assistant._run(composed))
        await asyncio.sleep(0.02)
        sent_while_held = list(phone.bot.sent)
        lock.release()
        await task
        return sent_while_held

    assert run(go()) == []
    assert phone.chat[0] == "🗣️ From the assistant:"


def test_the_assistant_reads_a_sentence_through_the_chats_own_rules(spies, claude):
    """Review of #79: the assistant re-implemented the reading. Through
    sentences' one reading, a bare number is not a sentence and never costs a
    Claude call — on either channel."""
    _, body = Phone().ask("2")

    assert body == {"say": assistant.DIDNT_GET_IT}
    assert claude.calls == []


# ─── THE SERVER ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kindle, assistant_token, expected", [
    ("", "", []),
    ("k", "", ["Kindle quotes"]),
    ("", "a", ["Android assistant"]),
    ("k", "a", ["Kindle quotes", "Android assistant"]),
])
def test_each_route_set_is_served_only_with_its_own_token(monkeypatch, kindle,
                                                         assistant_token, expected):
    monkeypatch.setattr(http_server, "KINDLE_TOKEN", kindle)
    monkeypatch.setattr(http_server, "ASSISTANT_TOKEN", assistant_token)

    app, names = http_server.build_app(FakeBot(), CHAT, david.ASSISTANT_ROUTER)

    assert names == expected
    paths = {route.resource.canonical for route in app.router.routes()}
    assert ("/assistant/ask" in paths) == bool(assistant_token)
    assert ("/kindle/quote" in paths) == bool(kindle)


def test_the_kindle_token_does_not_open_the_assistant(monkeypatch, spies, claude):
    monkeypatch.setattr(http_server, "KINDLE_TOKEN", "kindle-secret")
    monkeypatch.setattr(http_server, "ASSISTANT_TOKEN", TOKEN)
    app, _ = http_server.build_app(FakeBot(), CHAT, david.ASSISTANT_ROUTER)

    async def go():
        async with TestClient(TestServer(app)) as client:
            resp = await client.post("/assistant/ask", json={"text": "B"},
                                     headers={"Authorization": "Bearer kindle-secret"})
            return resp.status

    assert run(go()) == 401
    assert claude.calls == []
