"""
The only place David talks to the Anthropic API.

WHY THIS EXISTS
---------------
`learn.summarize_with_claude`, `implement.merge_with_claude` and
`implement_diet.decide_updates` were three hand-rolled copies of the same call,
and all three shared the same three gaps:

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
regex: the model returns its answer as a TOOL CALL, so the SDK hands back a
parsed dict and there is no text to extract from at all.

WHY FORCED TOOL USE AND NOT `output_config.format`
--------------------------------------------------
The API has a dedicated structured-output mode, and it is the obvious choice —
but it is not available on every model, and `config.ANTHROPIC_MODEL` is one of
the ones it does not cover. Forced tool use (`tool_choice={"type": "tool"}`)
works everywhere, and for our purposes it buys the same thing: the model fills
in a declared JSON Schema and the SDK returns `block.input` already parsed.

The schema is advisory on this path — the model is told the shape but the API
does not hard-validate it, so `complete_json` checks the required keys itself.
On a model that supports it, adding `"strict": True` to the tool definition
upgrades that to an API-enforced guarantee.

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
from datetime import date

import anthropic

from config import (
    ANTHROPIC_DAILY_BUDGET_USD,
    ANTHROPIC_INPUT_COST_PER_MTOK,
    ANTHROPIC_MAX_RETRIES,
    ANTHROPIC_MAX_TOKENS,
    ANTHROPIC_MODEL,
    ANTHROPIC_OUTPUT_COST_PER_MTOK,
    ANTHROPIC_READ_TIMEOUT,
)

logger = logging.getLogger(__name__)

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")

SPEND_FILE = os.environ.get("ANTHROPIC_SPEND_FILE", ".anthropic_spend.json")

# The tool the model is forced to call. The name is arbitrary but it is what the
# model sees, so it reads as an instruction: emit the answer, structured.
ANSWER_TOOL = "emit_answer"

# Used when a caller passes no schema. Permissive on purpose — it still routes
# the answer through a tool call, which is what removes the regex.
_ANY_OBJECT = {"type": "object", "properties": {}}


# ─── CLIENT ────────────────────────────────────────────────────────────────────
# One client per thread. The SDK's httpx client is not documented as thread-safe
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
# blocking. Same reasoning as month.py.

_lock = threading.Lock()
_spend = {"day": "", "usd": 0.0, "calls": 0}


def estimated_cost(input_tokens: int, output_tokens: int) -> float:
    """USD for one call, at the configured per-million rates."""
    return (input_tokens  * ANTHROPIC_INPUT_COST_PER_MTOK  / 1_000_000
            + output_tokens * ANTHROPIC_OUTPUT_COST_PER_MTOK / 1_000_000)


def _today() -> str:
    return date.today().isoformat()


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

def complete_json(system: str, user: str, schema: dict | None = None,
                  max_tokens: int = ANTHROPIC_MAX_TOKENS,
                  model: str | None = None) -> tuple[dict | None, str | None]:
    """Ask Claude for a JSON object. Returns (result, error) — never raises.

    `schema` is a JSON Schema for the object wanted. Its top-level `required`
    list is enforced here after the call, so a caller that names its required
    keys gets a clear error instead of a KeyError three frames later.

    Every failure mode comes back as a sentence the owner can act on, because
    all three call sites forward it straight to Telegram.
    """
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY is not set in environment."

    err = _budget_error()
    if err:
        return None, err

    tool = {
        "name": ANSWER_TOOL,
        "description": "Return the answer as a structured object.",
        "input_schema": schema or _ANY_OBJECT,
    }

    try:
        response = _client().messages.create(
            model=model or ANTHROPIC_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            tools=[tool],
            # Forced: the model has exactly one way to answer, so there is no
            # branch where it replies in prose and we are back to regexing.
            tool_choice={"type": "tool", "name": ANSWER_TOOL},
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
    logger.info("Anthropic %s: in=%d out=%d ≈$%.4f (day ≈$%.2f) stop=%s",
                response.model, usage.input_tokens, usage.output_tokens,
                cost, spend_today()["usd"], response.stop_reason)

    # ── Truncated at the cap ───────────────────────────────────────────────────
    # Checked before reading the content: a tool call cut off mid-object is not
    # partially usable, and the old code's "JSON parse error" for this named
    # neither the cause nor the fix.
    if response.stop_reason == "max_tokens":
        return None, (f"Claude hit the {max_tokens}-token output cap and the answer was cut "
                      f"off mid-object. Nothing was saved. Try a shorter source, or raise "
                      f"ANTHROPIC_MAX_TOKENS.")

    if response.stop_reason == "refusal":
        return None, "Claude declined to answer this one. Nothing was saved."

    for block in response.content:
        if block.type == "tool_use" and block.name == ANSWER_TOOL:
            result = block.input
            if not isinstance(result, dict):
                return None, f"Claude returned a {type(result).__name__}, expected an object."
            missing = [k for k in (schema or {}).get("required", []) if k not in result]
            if missing:
                return None, f"Claude's answer is missing: {', '.join(missing)}."
            return result, None

    # Forced tool_choice makes this all but unreachable; if the contract ever
    # changes, say so plainly rather than returning an empty dict.
    return None, f"Claude did not return a structured answer (stop_reason: {response.stop_reason})."
