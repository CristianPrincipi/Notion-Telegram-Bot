"""Plain sentences: a message that is not a command, read as one — with a
confirmation before anything is written.

    you:    ho speso 12 euro da Carrefour
    David:  Did you mean this?
            Add e Carrefour 12 f          [✅ Yes] [❌ No]
    you:    ✅ Yes
    David:  ▶️ Add e Carrefour 12 f
            ✅ Expense added …

WHERE IT SITS
-------------
Last, after everything else has had its chance: an open prompt, then the
registry, then a menu button tapped late. Only a message nothing else claimed
reaches the interpreter, so a typed command never costs a Claude call and never
waits on one.

WHAT IS CHECKED, AND BY WHOM
----------------------------
services/interpret.py writes a command. Nothing it writes runs until:

  1. it fullmatches EXACTLY ONE command in the registry (`matching`, passed in by
     david.py, which owns the registry) — a proposal matching nothing, or two, is
     "I didn't get that", never a guess;
  2. for a command that writes (`Command.read_only` False), you tap ✅ Yes. A
     read — `B`, `Agenda`, `Get`… — runs at once: it changes nothing, so a wrong
     reading costs one look. A voice note will always ask, because what was HEARD
     may be wrong even when the command is right;
  3. david.run_composed routes it like a typed message: OWNER_ID, the numbered
     list of a destructive command, the locks.

A failure of the model — over the daily budget, unreachable, declined — is
reported as such next to "I didn't get that", and is never a write.

THE TWO PROMPTS
---------------
Both live in pending_choice's one slot, like the menu's question and the
numbered list, and lapse after the same two minutes:

  CONFIRM  — "Did you mean this?" ✅ Yes runs it, ❌ No drops it. ANY OTHER
             message drops it too, and is then read as itself: a confirmation is
             a yes-or-no, and a new command typed instead of an answer is a new
             command, not a "no" that swallows it.
  CLARIFY  — the model asked for a missing part ("How much did you spend?"). The
             reply is read together with the first sentence. A reply that is
             itself a command runs as that command.
"""

import asyncio
import logging
from dataclasses import dataclass

from telegram import ReplyKeyboardMarkup

import pending_choice
from bot.menu import MAIN_KEYBOARD, Composed
from config import INTERPRET_MAX_CHARS, INTERPRET_TIMEOUT
from services import interpret

logger = logging.getLogger(__name__)

CONFIRM = "confirm"
CLARIFY = "clarify"

YES_BUTTON = "✅ Yes"
NO_BUTTON  = "❌ No"

# Typed answers that count as the buttons. Lower-cased, stripped.
_YES = {YES_BUTTON.lower(), "yes", "y", "si", "sì", "ok", "ja"}
_NO  = {NO_BUTTON.lower(), "no", "n"}

CONFIRM_KEYBOARD = ReplyKeyboardMarkup([[YES_BUTTON, NO_BUTTON]], resize_keyboard=True)

DIDNT_GET_IT = "❓ I didn't get that. Try: 'Add e Carrefour 2.20', or tap 📋 Menu."


@dataclass(frozen=True)
class Proposal:
    """A command waiting for ✅ Yes. The slot reads only `expires_at`."""

    composed: Composed
    expires_at: float


@dataclass(frozen=True)
class Clarifying:
    """A question the model asked, waiting for its answer."""

    sentence: str
    question: str
    expires_at: float


async def _interpret(text, catalogue, earlier=None):
    """The model's reading, off the event loop and capped. (answer, error)."""
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(interpret.interpret, text, catalogue, earlier),
            timeout=INTERPRET_TIMEOUT)
    except asyncio.TimeoutError:
        return None, f"no answer within {INTERPRET_TIMEOUT} seconds"


async def understand(update, context, text, *, catalogue, matching, earlier=None):
    """Read a sentence as a command. Returns a Composed to run now — a read — or
    None, having replied itself: the confirmation, a question, or why not.

    `matching(text)` is the registry's list of commands that fullmatch `text`.
    """
    # A bare number on its own is never a sentence; as the answer to "how much?"
    # it is the whole point.
    if (not text or len(text) > INTERPRET_MAX_CHARS
            or (earlier is None and text.isdigit())):
        await update.message.reply_text(DIDNT_GET_IT)
        return None

    answer, err = await _interpret(text, catalogue, earlier)
    if err:
        await update.message.reply_text(
            f"{DIDNT_GET_IT}\n(Reading it as a sentence failed too: {err})")
        return None

    if not answer["command"]:
        if answer["question"]:
            sentence = text if earlier is None else f"{earlier[0]} — {text}"
            pending_choice.remember(
                context.user_data, CLARIFY,
                Clarifying(sentence, answer["question"], pending_choice.deadline()), None)
            await update.message.reply_text(f"❓ {answer['question']}",
                                            reply_markup=MAIN_KEYBOARD)
            return None
        await update.message.reply_text(DIDNT_GET_IT)
        return None

    commands = matching(answer["command"])
    if len(commands) != 1:
        logger.warning("Interpreter proposed %r, which matches %d commands — dropped",
                       answer["command"], len(commands))
        await update.message.reply_text(DIDNT_GET_IT)
        return None

    command = commands[0]
    composed = Composed(answer["command"], command.name, {})
    if command.read_only:
        return composed

    pending_choice.remember(context.user_data, CONFIRM,
                            Proposal(composed, pending_choice.deadline()), None)
    await update.message.reply_text(f"Did you mean this?\n{composed.text}",
                                    reply_markup=CONFIRM_KEYBOARD)
    return None


async def answer_confirmation(update, context, text):
    """✅ Yes → the command, to run. ❌ No → nothing. Anything else drops the
    proposal and is handed back to be read as itself."""
    proposal = pending_choice.peek(context.user_data, CONFIRM)
    pending_choice.clear(context.user_data)
    if proposal is None:
        await reply_stale(update)
        return None

    answer = text.strip().lower()
    if answer in _YES:
        return proposal.composed
    if answer in _NO:
        await update.message.reply_text("OK — nothing done.", reply_markup=MAIN_KEYBOARD)
        return None

    await update.message.reply_text(f"Not confirmed, so nothing done: {proposal.composed.text}",
                                    reply_markup=MAIN_KEYBOARD)
    return pending_choice.NOT_AN_ANSWER


async def answer_clarification(update, context, text, *, catalogue, matching):
    """The reply to the model's question, read with the sentence that raised it.
    A reply that is a command on its own is handed back to run as one."""
    asked = pending_choice.peek(context.user_data, CLARIFY)
    pending_choice.clear(context.user_data)
    if asked is None or matching(text):
        return pending_choice.NOT_AN_ANSWER
    return await understand(update, context, text, catalogue=catalogue, matching=matching,
                            earlier=(asked.sentence, asked.question))


def is_stale_tap(text):
    """✅ Yes / ❌ No arriving with no proposal open: tapped after it lapsed."""
    return text in (YES_BUTTON, NO_BUTTON)


async def reply_stale(update):
    await update.message.reply_text(
        f"That proposal has lapsed — it is answerable for "
        f"{pending_choice.PENDING_TTL_SECONDS // 60} minutes — so nothing was done. "
        f"Send it again if you still want it.",
        reply_markup=MAIN_KEYBOARD)
