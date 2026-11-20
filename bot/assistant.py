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
sentence path again — `sentences.read`, the one reading every channel uses: the
interpreter writes a command, it must fullmatch EXACTLY ONE command — then a read
runs at once, a write waits for yes, and it runs through david.run_composed like
a typed message. Two
more rules, because there is no chat on the other end:

  - SOME COMMANDS ARE REFUSED — named by david.py in `Router.refused`, each with
    its reason: a destructive command picks from a numbered list in the chat; the
    menu is buttons; `undo` undoes what was done in the chat; Learn and Implement
    report minutes later, in the chat.
  - ONE PENDING SLOT: pending_choice's, in the assistant's own user_data, two
    minutes, with an id the phone sends back. A new ask replaces it. Losing it to
    a redeploy loses a question, never a write (Hard Rule 1).

What runs also appears in the chat (bot/chat_target.py): "🗣️ From the
assistant", then the command's own replies. It runs under david's dispatch lock,
so it never interleaves with a chat message (Hard Rule 5).
"""

import hmac
import logging
import secrets
from dataclasses import dataclass, field
from typing import Callable

from aiohttp import web

import pending_choice
from bot import chat_target, sentences
from bot.menu import Composed

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


@dataclass(frozen=True)
class _Waiting:
    """What the assistant parks in pending_choice's slot — its OWN user_data's,
    under sentences' kinds: CONFIRM for a write waiting for yes, CLARIFY for a
    question waiting for its answer. The id is what the phone sends back."""

    id: str
    expires_at: float
    composed: Composed | None = None           # CONFIRM
    earlier: tuple | None = None               # CLARIFY: (sentence, question)


@dataclass
class Assistant:
    router: Router
    bot: object
    chat_id: object
    application: object = None
    user_data: dict = field(default_factory=dict)

    def _take(self, kind, pending_id):
        """The waiting record of this kind and id, still in time — and the slot
        emptied whatever it held: an answer that is not this one closes it."""
        waiting = pending_choice.peek(self.user_data, kind)
        pending_choice.clear(self.user_data)
        if waiting is None or not pending_id or waiting.id != pending_id:
            return None
        return waiting

    def _park(self, kind, **kwargs):
        waiting = _Waiting(secrets.token_urlsafe(9), pending_choice.deadline(), **kwargs)
        pending_choice.remember(self.user_data, kind, waiting, None)
        return waiting.id

    async def ask(self, text, pending_id=None):
        """A sentence → the JSON answer (a dict)."""
        text = text.strip()
        earlier = None
        if pending_id:
            asked = self._take(sentences.CLARIFY, pending_id)
            if asked is None:
                return {"say": LAPSED}
            earlier = asked.earlier
        else:
            pending_choice.clear(self.user_data)

        # The chat's own reading: the same gate, interpreter and exactly-one check.
        reading = await sentences.read(text, catalogue=self.router.catalogue,
                                       matching=self.router.matching, earlier=earlier)
        if reading.error:
            return {"say": f"I could not read that: {reading.error}"}
        if reading.question:
            sentence = text if earlier is None else f"{earlier[0]} — {text}"
            ask_id = self._park(sentences.CLARIFY, earlier=(sentence, reading.question))
            return {"say": reading.question, "ask": reading.question, "id": ask_id}
        if reading.composed is None:
            return {"say": DIDNT_GET_IT}

        composed = reading.composed
        if reading.command.name in self.router.refused:
            return {"say": f"{composed.text}: {self.router.refused[reading.command.name]}"}
        if reading.command.read_only:
            return {"say": await self._run(composed)}
        write_id = self._park(sentences.CONFIRM, composed=composed)
        return {"say": f"Shall I do this? {composed.text}", "confirm": composed.text,
                "id": write_id}

    async def confirm(self, pending_id):
        proposal = self._take(sentences.CONFIRM, pending_id)
        if proposal is None:
            return {"say": LAPSED}
        return {"say": await self._run(proposal.composed)}

    async def _run(self, composed):
        """Run through the router, in the chat's name; the replies, as speech."""
        message = chat_target.ChatMessage(self.bot, self.chat_id, composed.text)
        update = chat_target.ChatUpdate(message)
        context = chat_target.ChatContext(self.bot, self.application, self.user_data)
        logger.info("Assistant runs: %s", composed.text)
        async with self.router.lock():
            # The header too, inside the lock: before it, it could land between a
            # chat command's replies (review of #79).
            try:
                await self.bot.send_message(chat_id=self.chat_id, text="🗣️ From the assistant:")
            except Exception as e:
                logger.warning("Assistant header not sent to the chat: %s", type(e).__name__)
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
