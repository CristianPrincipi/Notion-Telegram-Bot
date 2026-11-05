"""
The only place David talks to the Anthropic API.

WHY THIS EXISTS
---------------
`learn.summarize_with_claude`, `implement.merge_with_claude` and
`implement_diet.decide_updates` (since split into route_sections/merge_sections)
were three hand-rolled copies of the same call, and all three shared the same
three gaps:

  1. NO RETRY. notion_client has retried 429/5xx since the beginning; these did
     not. A single 429 or 529 — arriving after a transcript fetch, a Notion read
     and two minutes of generation — threw the whole job away, and the re-run
     paid for every one of those tokens again.
  2. NO stop_reason CHECK. `max_tokens` is a hard cap. Hit it and the response
     is a *truncated* object, which failed at `json.loads` and surfaced as
     "JSON parse error: Expecting ',' delimiter" — a message that says nothing
     about the real cause or the fix.
  3. FRAGILE EXTRACTION. `re.search(r"\\{.*\\}", raw, re.DOTALL)` is greedy: it
     spans from the first brace to the LAST one anywhere in the response, so any
     trailing prose containing a brace was swallowed into the match and broke
     the parse.

All three are gone here, and the fix for (3) is structural rather than a better
regex: the answer is constrained to a JSON Schema by the API, so what comes back
is one JSON document, parsed whole, with no prose around it to search.

STRUCTURED OUTPUT, AND WHY IT USED TO BE A FORCED TOOL CALL
-----------------------------------------------------------
The schema goes in `output_config.format`, and the API constrains the answer to
it: the reply is a `text` block holding JSON that matches the schema. That is a
guarantee, not a request — which also means the API checks the SCHEMA, and
answers a 400 for one it cannot enforce (every object needs
`additionalProperties: false`; no `minLength`, `maximum` and the like). Every
schema passed here is checked offline against those rules by
tests/test_claude_schemas.py.

Until 2026-11 this was a forced tool call (`tool_choice={"type": "tool"}`),
because Claude Sonnet 4.5 had no structured output. Sonnet 5.5 is the other way
round: it answers a forced `tool_choice` with a 400, so the old path would have
failed on every call. The required-keys check below survives the move, as a free
second line behind the API's.

THINKING AND EFFORT
-------------------
Sonnet 5.5 thinks before it answers, and `thinking={"type": "disabled"}` is a
400, so no `thinking` field is sent. How much it thinks is `effort`, always sent
and always named in config (ANTHROPIC_EFFORT, ANTHROPIC_ROUTE_EFFORT): its
default is "high", thinking is billed as output, and it counts toward
`max_tokens`. Two consequences shape the code below:

  - the answer is not necessarily the first block: a `thinking` block (empty
    text, by default) can come before it, so blocks are read by TYPE;
  - the output cap must leave room for thinking, which is why
    ANTHROPIC_MAX_TOKENS defaults to 16000.

No refusal fallback (the `fallbacks` beta, which retries a declined request on
another model): on Sonnet 5.5 it retries only two refusal categories, neither of
them likely for a summary, and it would make this module depend on a beta API.
Decided 2026-11-05; a refusal is reported, with its category.

BLOCKING, LIKE EVERYTHING ELSE
------------------------------
`complete_json` is synchronous and must be reached through `asyncio.to_thread`,
with the caller's `asyncio.wait_for(ANTHROPIC_TIMEOUT)` around it. See the async
note at the top of david.py.
"""

import json
import logging
import os
import threading

import anthropic

from config import (
    ANTHROPIC_DAILY_BUDGET_USD,
    ANTHROPIC_EFFORT,
    ANTHROPIC_INPUT_COST_PER_MTOK,
    ANTHROPIC_MAX_RETRIES,
    ANTHROPIC_MAX_TOKENS,
    ANTHROPIC_MODEL,
    ANTHROPIC_OUTPUT_COST_PER_MTOK,
    ANTHROPIC_READ_TIMEOUT,
    env_or,
)
from clients.calendar_client import now_local

logger = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

SPEND_FILE = env_or("ANTHROPIC_SPEND_FILE", ".anthropic_spend.json")


# ─── CLIENT ────────────────────────────────────────────────────────────────────
# One client per thread. The SDK's httpx2 client is not documented as thread-safe
# and every call here runs inside an asyncio.to_thread worker — the same hazard
# notion_client.py solves for requests.Session and calendar_client.py for
# httplib2, for the same reason.

_thread_local = threading.local()


def _client() -> anthropic.Anthropic:
    """The calling thread's SDK client, built on first use.

    Retries live here rather than in a hand-written loop: the SDK already backs
    off exponentially on 429, 408, 409, 5xx (which covers 529 overloaded) and
    connection errors, and it reads the `retry-after` header when the API sends
    one — which a hand-rolled `2 ** attempt` cannot.
    """
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = anthropic.Anthropic(
            api_key=ANTHROPIC_KEY,
            timeout=ANTHROPIC_READ_TIMEOUT,
            max_retries=ANTHROPIC_MAX_RETRIES,
        )
        _thread_local.client = client
    return client


# ─── DAILY SPEND GUARD ─────────────────────────────────────────────────────────
# Guarded by a threading.Lock, not page_lock: this is reached from worker
# threads, and an asyncio.Lock between two threads acquires without ever
# blocking. Same reasoning as services/month.py.

_lock = threading.Lock()
_spend = {"day": "", "usd": 0.0, "calls": 0}


def estimated_cost(input_tokens: int, output_tokens: int) -> float:
    """USD for one call, at the configured per-million rates."""
    return (input_tokens  * ANTHROPIC_INPUT_COST_PER_MTOK  / 1_000_000
            + output_tokens * ANTHROPIC_OUTPUT_COST_PER_MTOK / 1_000_000)


def _today() -> str:
    # The PROJECT day, not the host's. This was `date.today()` — UTC on Railway —
    # so the budget turned over at 01:00 or 02:00 Rome time, the one date in David
    # not read through now_local(). tests/test_data_integrity.py now refuses a
    # clock read with no timezone anywhere in production code.
    return now_local().date().isoformat()


def _load_spend() -> dict:
    """Today's running total, from disk. A different day starts from zero."""
    try:
        with open(SPEND_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"day": _today(), "usd": 0.0, "calls": 0}
    except (OSError, ValueError) as e:
        logger.warning("Ignoring unreadable spend file %s: %s", SPEND_FILE, e)
        return {"day": _today(), "usd": 0.0, "calls": 0}

    if not isinstance(data, dict) or data.get("day") != _today():
        return {"day": _today(), "usd": 0.0, "calls": 0}
    return {"day": data["day"],
            "usd": float(data.get("usd") or 0.0),
            "calls": int(data.get("calls") or 0)}


def _save_spend() -> None:
    """Best-effort. Losing the file resets the day's budget, which is a bounded
    over-spend, not a broken bot."""
    try:
        with open(SPEND_FILE, "w", encoding="utf-8") as fh:
            json.dump(_spend, fh)
    except OSError as e:
        logger.warning("Could not record Anthropic spend to %s: %s", SPEND_FILE, e)


def _refresh_spend() -> None:
    """Roll the counter over at midnight, and pick up a previous run's total."""
    if _spend["day"] != _today():
        _spend.update(_load_spend())


def spend_today() -> dict:
    """{"day", "usd", "calls"} — today's estimated spend. Safe to call anywhere."""
    with _lock:
        _refresh_spend()
        return dict(_spend)


def _record(input_tokens: int, output_tokens: int) -> float:
    """Add one call to the day's total. Returns that call's estimated cost."""
    cost = estimated_cost(input_tokens, output_tokens)
    with _lock:
        _refresh_spend()
        _spend["usd"] += cost
        _spend["calls"] += 1
        _save_spend()
    return cost


def _budget_error() -> str | None:
    """The refusal message, or None when there is budget left.

    Checked BEFORE the call, so the threshold is a floor on what is spent, not a
    ceiling: the call that crosses it is allowed to finish. Bounding it exactly
    would mean predicting the response size before making the request.
    """
    with _lock:
        _refresh_spend()
        if _spend["usd"] < ANTHROPIC_DAILY_BUDGET_USD:
            return None
        spent, calls = _spend["usd"], _spend["calls"]

    logger.error("Anthropic daily budget reached: $%.2f of $%.2f over %d call(s).",
                 spent, ANTHROPIC_DAILY_BUDGET_USD, calls)
    return (f"Daily Anthropic budget reached — about ${spent:.2f} of "
            f"${ANTHROPIC_DAILY_BUDGET_USD:.2f} spent across {calls} call(s) today. "
            f"Nothing was sent. Raise ANTHROPIC_DAILY_BUDGET_USD on Railway, or "
            f"wait until tomorrow.")


# ─── THE ONE ENTRY POINT ───────────────────────────────────────────────────────

def complete_json(system: str, user: str, schema: dict,
                  max_tokens: int = ANTHROPIC_MAX_TOKENS,
                  effort: str = ANTHROPIC_EFFORT,
                  model: str | None = None) -> tuple[dict | None, str | None]:
    """Ask Claude for a JSON object. Returns (result, error) — never raises.

    `schema` is the JSON Schema of the object wanted, in the subset structured
    output accepts (tests/test_claude_schemas.py checks every caller's). Its
    top-level `required` list is checked again here after the call, so a caller
    that names its required keys gets a clear error instead of a KeyError three
    frames later.

    `effort` is how hard the model thinks first; a caller passes one of config's
    named levels, never a literal.

    Every failure mode comes back as a sentence the owner can act on, because
    all three call sites forward it straight to Telegram.
    """
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY is not set in environment."

    err = _budget_error()
    if err:
        return None, err

    try:
        response = _client().messages.create(
            model=model or ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            # The API constrains the answer to the schema: there is no branch
            # where the model replies in prose and we are back to regexing.
            output_config={
                "format": {"type": "json_schema", "schema": schema},
                "effort": effort,
            },
        )
    except anthropic.APIStatusError as e:
        # Retries are already exhausted by the time this escapes the SDK.
        return None, f"Anthropic {e.status_code}: {str(e)[:200]}"
    except anthropic.APIConnectionError as e:
        return None, f"Could not reach Anthropic: {e}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    usage = response.usage
    cost = _record(usage.input_tokens, usage.output_tokens)
    logger.info("Anthropic %s: in=%d out=%d ≈$%.4f (day ≈$%.2f) stop=%s effort=%s",
                response.model, usage.input_tokens, usage.output_tokens,
                cost, spend_today()["usd"], response.stop_reason, effort)

    # ── Truncated at the cap ───────────────────────────────────────────────────
    # Checked before reading the content: an answer cut off mid-object is not
    # partially usable, even when what arrived happens to parse, and the old
    # code's "JSON parse error" for this named neither the cause nor the fix.
    #
    # The message names the cap that was ACTUALLY USED, and says where to raise
    # it, because for a while it did neither usefully: it advised raising
    # ANTHROPIC_MAX_TOKENS while services/learn.py passed its own 4096, so the
    # one lever the message named was the one the failing call ignored. It also
    # read like ANTHROPIC_DAILY_BUDGET_USD's refusal below, which IS a Railway
    # variable — while this one was a source literal, so acting on the advice
    # meant a code change and a deploy. Both halves are true now; keep them
    # true. If a caller ever passes a cap of its own again, say so here.
    if response.stop_reason == "max_tokens":
        note = ("" if max_tokens == ANTHROPIC_MAX_TOKENS else
                f" (this call caps itself at {max_tokens}, below the configured "
                f"{ANTHROPIC_MAX_TOKENS})")
        return None, (f"Claude hit the {max_tokens}-token output cap and the answer was cut "
                      f"off mid-object. Nothing was saved{note}. Try a shorter source, or "
                      f"raise ANTHROPIC_MAX_TOKENS on Railway — it is {ANTHROPIC_MAX_TOKENS} "
                      f"now.")

    # ── Declined ───────────────────────────────────────────────────────────────
    # Also before the content: a refusal can arrive after part of the answer,
    # and that part need not match the schema. The category is the only clue to
    # which of the model's safety classifiers fired.
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None)
        why = f" (category: {category})" if category else ""
        return None, f"Claude declined to answer this one{why}. Nothing was saved."

    # ── The answer ─────────────────────────────────────────────────────────────
    # By TYPE, never by position: with thinking on, the first block can be an
    # empty `thinking` block.
    text = "".join(block.text for block in response.content if block.type == "text")
    if not text.strip():
        return None, (f"Claude did not return a structured answer "
                      f"(stop_reason: {response.stop_reason}).")
    try:
        result = json.loads(text)
    except ValueError as e:
        logger.error("Unparseable structured answer (%s): %.200r", e, text)
        return None, f"Claude's answer was not valid JSON ({e}). Nothing was saved."
    if not isinstance(result, dict):
        return None, f"Claude returned a {type(result).__name__}, expected an object."
    missing = [k for k in schema.get("required", []) if k not in result]
    if missing:
        return None, f"Claude's answer is missing: {', '.join(missing)}."
    return result, None
