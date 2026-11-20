"""The Android assistant: a sentence from the phone, read as a command — a read
answered at once, a write only after the phone's yes.

    POST /assistant/ask      {"text": "...", "id": "..."?}
    POST /assistant/confirm  {"id": "..."}
    Authorization: Bearer <ASSISTANT_TOKEN>

Every answer is JSON with `say` — what the phone reads aloud — and at most one of:
  - `confirm` + `id`: a write, waiting. The phone asks yes/no and, on yes, posts
    the id to /assistant/confirm. Nothing is written before that.
  - `ask` + `id`: the interpreter needs a missing part ("How much?"). The phone
    asks it and posts the answer to /assistant/ask WITH the id, so the two are
    read together.

The phone (HTTP Shortcuts, docs/assistant-android.md) does the listening — the
keyboard's dictation — so the server receives text, and this is the plain-
sentence path (bot/sentences.py) again, with its rules: the interpreter writes a
command, it must fullmatch EXACTLY ONE command, a read runs at once, a write
waits for yes, and it runs through david.run_composed like a typed message. Two
more rules, because there is no chat on the other end:

  - SOME COMMANDS ARE REFUSED — named by david.py in `Router.refused`, each with
    its reason: a destructive command picks from a numbered list in the chat; the
    menu is buttons; `undo` undoes what was done in the chat; Learn and Implement
    report minutes later, in the chat.
  - ONE PENDING SLOT, two minutes, in memory. A new ask replaces it. Losing it to
    a redeploy loses a question, never a write (Hard Rule 1).

What runs also appears in the chat (bot/chat_target.py): "🗣️ From the
assistant", then the command's own replies. It runs under david's dispatch lock,
so it never interleaves with a chat message (Hard Rule 5).
"""

import asyncio
import hmac
import logging
import secrets
from dataclasses import dataclass, field
from typing import Callable

from aiohttp import web

import pending_choice
from bot import chat_target
from bot.menu import Composed
from config import INTERPRET_MAX_CHARS, INTERPRET_TIMEOUT
from services import interpret

logger = logging.getLogger(__name__)

DIDNT_GET_IT = "I didn't get that. Try saying it more directly, or use Telegram."
LAPSED = "That proposal has lapsed, so nothing was done. Say it again."


@dataclass
class Router:
    """What the assistant needs from david.py, which owns the registry. Passed in:
    a bot module never imports david."""

    catalogue: str
    matching: Callable          # text -> [Command, ...] that fullmatch it
    run_composed: Callable      # async (Composed, update, context) -> None
    lock: Callable              # () -> async context manager: the dispatch lock
    refused: dict               # command name -> why it is Telegram-only


@dataclass
class _Pending:
    id: str
    expires_at: float
    composed: Composed | None = None          # a write waiting for yes
    earlier: tuple | None = None               # (sentence, question) waiting for a reply


@dataclass
class Assistant:
    router: Router
    bot: object
    chat_id: object
    application: object = None
    user_data: dict = field(default_factory=dict)
    pending: _Pending | None = None

    def _take(self, pending_id, want_write):
        """The pending entry with this id and kind, still in time — removed."""
        pending, self.pending = self.pending, None
        if (pending is None or not pending_id or pending.id != pending_id
                or pending_choice.is_expired(pending.expires_at)
                or (pending.composed is not None) != want_write):
            return None
        return pending

    def _park(self, **kwargs):
        self.pending = _Pending(secrets.token_urlsafe(9), pending_choice.deadline(), **kwargs)
        return self.pending.id

    async def ask(self, text, pending_id=None):
        """A sentence → the JSON answer (a dict)."""
        text = text.strip()
        earlier = None
        if pending_id:
            asked = self._take(pending_id, want_write=False)
            if asked is None:
                return {"say": LAPSED}
            earlier = asked.earlier
        else:
            self.pending = None

        if not text or len(text) > INTERPRET_MAX_CHARS:
            return {"say": DIDNT_GET_IT}

        try:
            answer, err = await asyncio.wait_for(
                asyncio.to_thread(interpret.interpret, text, self.router.catalogue, earlier),
                timeout=INTERPRET_TIMEOUT)
        except asyncio.TimeoutError:
            answer, err = None, f"no answer within {INTERPRET_TIMEOUT} seconds"
        if err:
            return {"say": f"I could not read that: {err}"}

        if not answer["command"]:
            if answer["question"]:
                sentence = text if earlier is None else f"{earlier[0]} — {text}"
                ask_id = self._park(earlier=(sentence, answer["question"]))
                return {"say": answer["question"], "ask": answer["question"], "id": ask_id}
            return {"say": DIDNT_GET_IT}

        commands = self.router.matching(answer["command"])
        if len(commands) != 1:
            logger.warning("Assistant: %r matches %d commands — dropped",
                           answer["command"], len(commands))
            return {"say": DIDNT_GET_IT}
        command = commands[0]
        if command.name in self.router.refused:
            return {"say": f"{answer['command']}: {self.router.refused[command.name]}"}

        composed = Composed(answer["command"], command.name, {})
        if command.read_only:
            return {"say": await self._run(composed)}
        write_id = self._park(composed=composed)
        return {"say": f"Shall I do this? {composed.text}", "confirm": composed.text,
                "id": write_id}

    async def confirm(self, pending_id):
        proposal = self._take(pending_id, want_write=True)
        if proposal is None:
            return {"say": LAPSED}
        return {"say": await self._run(proposal.composed)}

    async def _run(self, composed):
        """Run through the router, in the chat's name; the replies, as speech."""
        message = chat_target.ChatMessage(self.bot, self.chat_id, composed.text)
        update = chat_target.ChatUpdate(message)
        context = chat_target.ChatContext(self.bot, self.application, self.user_data)
        try:
            await self.bot.send_message(chat_id=self.chat_id, text="🗣️ From the assistant:")
        except Exception as e:
            logger.warning("Assistant header not sent to the chat: %s", type(e).__name__)
        logger.info("Assistant runs: %s", composed.text)
        async with self.router.lock():
            await self.router.run_composed(composed, update, context)
        # The first reply is run_composed's "▶️ <command>": shown in the chat,
        # not worth reading aloud.
        spoken = [line for line in message.spoken if not line.startswith("▶️")]
        return "\n".join(spoken) or "Done."


def _authorized(request, token):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].encode(), token.encode())


async def _body(request, *fields):
    """(dict, None) or (None, error response)."""
    try:
        payload = await request.json()
    except Exception:
        return None, web.json_response({"say": "The request was not valid JSON."}, status=400)
    if not isinstance(payload, dict):
        return None, web.json_response({"say": "The body must be a JSON object."}, status=400)
    for name in fields:
        value = payload.get(name)
        if value is not None and not isinstance(value, str):
            return None, web.json_response({"say": f"'{name}' must be a string."}, status=400)
    return payload, None


def add_routes(app, *, token, assistant):
    async def ask(request):
        if not _authorized(request, token):
            return web.json_response({"say": "Wrong or missing token."}, status=401)
        payload, refusal = await _body(request, "text", "id")
        if refusal:
            return refusal
        try:
            return web.json_response(await assistant.ask(payload.get("text") or "",
                                                         payload.get("id")))
        except Exception:
            logger.exception("Assistant ask failed")
            return web.json_response(
                {"say": "Something went wrong. Check Telegram before trying again."}, status=500)

    async def confirm(request):
        if not _authorized(request, token):
            return web.json_response({"say": "Wrong or missing token."}, status=401)
        payload, refusal = await _body(request, "id")
        if refusal:
            return refusal
        try:
            return web.json_response(await assistant.confirm(payload.get("id")))
        except Exception:
            logger.exception("Assistant confirm failed")
            # A write that raised may still have written: say where to look.
            return web.json_response(
                {"say": "Something went wrong. Check Telegram before trying again."}, status=500)

    app.router.add_post("/assistant/ask", ask)
    app.router.add_post("/assistant/confirm", confirm)
