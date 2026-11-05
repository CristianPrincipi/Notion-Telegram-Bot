"""The shared Anthropic client — the three gaps the old hand-rolled calls had.

WHAT THIS LOCKS DOWN
--------------------
learn.py, implement.py and implement_diet.py each built their own Anthropic
call, and all three shared the same three failures:

  1. NO RETRY. One 429 or 529 — after a transcript fetch, a Notion read and two
     minutes of generation — threw the whole job away, and the re-run paid for
     every token again.
  2. NO stop_reason CHECK. Truncation at max_tokens produced a half-written
     object, which surfaced as "JSON parse error: Expecting ',' delimiter" — a
     message that names neither the cause nor the fix.
  3. FRAGILE EXTRACTION. `re.search(r"\\{.*\\}", raw, re.DOTALL)` is greedy, so
     any trailing prose containing a brace was swallowed into the match.

The HTTP layer is a real httpx2 MockTransport rather than a stubbed
`complete_json`, because the retry under test belongs to the SDK client — a
stub would assert that the test agrees with itself.

httpx2, not httpx: anthropic 1.x is built on the httpx2 fork and refuses an
httpx client at construction, so the fixture has to speak the SDK's own
transport. A transport from the wrong package is a TypeError in every test's
setup, not a silent pass-through.

THE ANSWER'S SHAPE (since the move to Claude Sonnet 5.5)
--------------------------------------------------------
The answer used to arrive as a forced tool call. Sonnet 5.5 rejects a forced
`tool_choice` with a 400, so it now arrives as structured output: a `text` block
holding JSON that the API has checked against the schema, possibly AFTER a
`thinking` block (thinking is on by default and cannot be turned off with
`disabled`). The fake below speaks that shape; section 3 locks down the request.
"""

import json
import logging
from datetime import datetime

import anthropic
import httpx2
import pytest
import pytz

from clients import anthropic_client
from clients.anthropic_client import complete_json
from config import ANTHROPIC_EFFORT, ANTHROPIC_ROUTE_EFFORT

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "tldr": {"type": "string"}},
    "required": ["title"],
    "additionalProperties": False,
}


# ─── A FAKE ANTHROPIC ENDPOINT ─────────────────────────────────────────────────

def answer(obj) -> dict:
    """The text block structured output answers with: the object, as JSON."""
    return {"type": "text", "text": json.dumps(obj)}


# What adaptive thinking puts before the answer under the default
# `display: "omitted"`: a thinking block whose text is empty.
THINKING = {"type": "thinking", "thinking": "", "signature": "sig_1"}


def message(content=None, stop_reason="end_turn", input_tokens=120, output_tokens=45,
            stop_details=None):
    """One /v1/messages response body."""
    if content is None:
        content = [answer({"title": "Summary", "tldr": "the gist"})]
    return {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-sonnet-5-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }


@pytest.fixture
def anthropic_api(monkeypatch, tmp_path):
    """Serve queued responses to the SDK, and record every request it made.

    The client is built exactly as `_client()` builds it, reading the same
    config constants, so the retry behaviour under test is the shipping one.
    `test_the_real_client_carries_the_configured_retries` guards the mirroring.
    """
    monkeypatch.setattr(anthropic_client, "SPEND_FILE", str(tmp_path / "spend.json"))
    monkeypatch.setattr(anthropic_client, "_spend", {"day": "", "usd": 0.0, "calls": 0})
    monkeypatch.setattr(anthropic_client, "ANTHROPIC_KEY", "test-anthropic-key")

    state = {"queue": [], "requests": []}

    def handler(request: httpx2.Request) -> httpx2.Response:
        state["requests"].append(json.loads(request.content.decode()))
        status, body = state["queue"].pop(0) if state["queue"] else (200, message())
        return httpx2.Response(status, json=body)

    anthropic_client._thread_local.client = anthropic.Anthropic(
        api_key="test-anthropic-key",
        timeout=anthropic_client.ANTHROPIC_READ_TIMEOUT,
        max_retries=anthropic_client.ANTHROPIC_MAX_RETRIES,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    yield state
    del anthropic_client._thread_local.client


# ─── 1. RETRY ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("status", [429, 500, 529])
def test_a_transient_failure_is_retried_rather_than_thrown_away(anthropic_api, status):
    """THE gap. notion_request has retried since the beginning; these did not.

    529 is Anthropic's "overloaded" — it arrives more often than a 500 and is
    exactly the case where the job is minutes old and the tokens are already
    paid for.
    """
    anthropic_api["queue"] = [(status, {"type": "error", "error": {"type": "overloaded_error"}}),
                              (200, message())]

    result, err = complete_json("system", "user", SCHEMA)

    assert err is None, f"gave up on a {status} instead of retrying"
    assert result["title"] == "Summary"
    assert len(anthropic_api["requests"]) == 2, "never retried"


def test_a_client_error_is_not_retried(anthropic_api):
    """A 400 will never fix itself — retrying only triples the latency."""
    anthropic_api["queue"] = [(400, {"type": "error",
                                     "error": {"type": "invalid_request_error",
                                               "message": "bad schema"}})]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "400" in err
    assert len(anthropic_api["requests"]) == 1, "retried a client error"


def test_retries_are_bounded_and_the_last_failure_is_reported(anthropic_api):
    overloaded = (529, {"type": "error", "error": {"type": "overloaded_error"}})
    anthropic_api["queue"] = [overloaded] * (anthropic_client.ANTHROPIC_MAX_RETRIES + 1)

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "529" in err
    assert len(anthropic_api["requests"]) == anthropic_client.ANTHROPIC_MAX_RETRIES + 1


def test_the_real_client_carries_the_configured_retries(monkeypatch):
    """Guards the fixture above: it mirrors `_client()`, so a change to how the
    shipping client is built has to show up here rather than silently leaving the
    retry tests asserting a configuration nothing uses."""
    monkeypatch.delattr(anthropic_client._thread_local, "client", raising=False)

    client = anthropic_client._client()

    assert client.max_retries == anthropic_client.ANTHROPIC_MAX_RETRIES
    assert anthropic_client.ANTHROPIC_MAX_RETRIES >= 1, "retry is off"


# ─── 2. TRUNCATION AT THE OUTPUT CAP ───────────────────────────────────────────

def test_hitting_max_tokens_is_an_actionable_error_not_a_parse_crash(anthropic_api):
    """Truncated output used to surface as "JSON parse error: Expecting ','" —
    which says nothing about the cap being the cause or raising it being the fix.
    """
    anthropic_api["queue"] = [(200, message(
        content=[{"type": "text", "text": '{"title": "Half a summ'}],
        stop_reason="max_tokens"))]

    result, err = complete_json("system", "user", SCHEMA, max_tokens=8192)

    assert result is None
    assert "8192" in err and "cap" in err
    assert "Nothing was saved" in err
    assert "parse" not in err.lower(), "still reporting truncation as a parse failure"


def _truncated():
    """A response that stopped at the cap, holding half an object."""
    return [(200, message(
        content=[THINKING, {"type": "text", "text": '{"title": "Half a summ'}],
        stop_reason="max_tokens"))]


def test_the_truncation_error_says_where_to_raise_the_cap(anthropic_api):
    """The message is the whole interface to this failure — it arrives in Telegram
    after a fetch and two minutes of generation, with nothing saved.

    It used to advise raising ANTHROPIC_MAX_TOKENS in a sentence shaped exactly
    like the daily-budget refusal, which names a Railway variable. This one was a
    source literal, so following the advice meant a code change and a deploy. Now
    it is a variable, and the message has to keep saying so.
    """
    anthropic_api["queue"] = _truncated()

    _, err = complete_json("system", "user", SCHEMA)

    assert "ANTHROPIC_MAX_TOKENS" in err
    assert "Railway" in err, "names the lever without saying it is one you can pull"
    assert str(anthropic_client.ANTHROPIC_MAX_TOKENS) in err, (
        "does not say what the cap currently is, so you cannot tell what raising it means"
    )


def test_a_call_that_caps_itself_below_the_constant_says_so(anthropic_api):
    """THE BUG THIS FILE'S SIBLING EXISTS FOR, reported rather than hidden.

    `services/learn.py` passed 4096 while the constant said 8192, so the error
    named a lever the failing call ignored — you raise ANTHROPIC_MAX_TOKENS,
    redeploy, and hit the identical 4096 wall. `tests/test_output_cap.py` stops
    that call site from existing; this makes the message honest if one ever does,
    because the two guards fail in opposite directions and only this one survives
    a deliberate per-call cap.
    """
    anthropic_api["queue"] = _truncated()

    _, err = complete_json("system", "user", SCHEMA, max_tokens=1024)

    assert "1024" in err, "does not name the cap that actually stopped the answer"
    assert str(anthropic_client.ANTHROPIC_MAX_TOKENS) in err
    assert "below" in err, "does not say the call capped itself under the configured value"


def test_a_truncated_answer_is_never_partially_used(anthropic_api):
    """The half-written object is present in the response and must be discarded —
    a Manual merged from half a merge is worse than no merge."""
    anthropic_api["queue"] = [(200, message(
        # Half an object that HAPPENS to be valid JSON: the stop_reason, not the
        # parse, is what must reject it.
        content=[answer({"title": "Half"})],
        stop_reason="max_tokens"))]

    result, _ = complete_json("system", "user", SCHEMA)

    assert result is None


def test_a_refusal_is_reported_plainly(anthropic_api):
    anthropic_api["queue"] = [(200, message(content=[], stop_reason="refusal"))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "declined" in err


def test_a_refusal_names_its_category_when_the_api_gives_one(anthropic_api):
    """Sonnet 5.5's classifiers decline in five categories, and `general_harms`
    can fire on benign text. The category is the only clue to which one fired,
    and there is no other place the owner could read it."""
    anthropic_api["queue"] = [(200, message(
        content=[], stop_reason="refusal",
        stop_details={"type": "refusal", "category": "general_harms",
                      "explanation": "Declined by a safety classifier."}))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "declined" in err and "general_harms" in err


def test_a_refusal_is_checked_before_any_partial_answer_is_read(anthropic_api):
    """A mid-answer refusal can leave partial output that need not match the
    schema. It must be reported as a refusal, never parsed."""
    anthropic_api["queue"] = [(200, message(
        content=[answer({"title": "Partial"})], stop_reason="refusal"))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "declined" in err


# ─── 3. STRUCTURED OUTPUT — NO BRACE HUNTING ───────────────────────────────────

def test_the_answer_comes_back_as_a_parsed_object(anthropic_api):
    result, err = complete_json("system", "user", SCHEMA)

    assert err is None
    assert result == {"title": "Summary", "tldr": "the gist"}


def test_the_schema_is_enforced_by_the_api_not_by_a_forced_tool(anthropic_api):
    """RED on the Sonnet 4.5 client. Sonnet 5.5 answers a forced `tool_choice`
    with a 400 (`tool_choice: type "tool" and "any" are not supported for this
    model.`), so every Learn and Implement would fail on the first call.

    Structured output replaces it: the schema goes in `output_config.format`, the
    API constrains the answer to it, and there is still no branch where the model
    replies in prose."""
    complete_json("system", "user", SCHEMA)

    body = anthropic_api["requests"][0]
    assert "tool_choice" not in body, "a forced tool_choice is a 400 on Sonnet 5.5"
    assert "tools" not in body
    assert body["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}


def test_thinking_is_left_at_the_models_default(anthropic_api):
    """`thinking: {"type": "disabled"}` is a 400 on Sonnet 5.5. How much it thinks
    is set with `effort` instead, so the request carries no `thinking` field."""
    complete_json("system", "user", SCHEMA)

    assert "thinking" not in anthropic_api["requests"][0]


def test_effort_is_sent_and_defaults_to_the_configured_level(anthropic_api):
    """Sonnet 5.5 defaults to `high`, and thinking bills as output — the costly
    half. Leaving effort unset would make the migration MORE expensive, so the
    level is always sent, and named in config."""
    complete_json("system", "user", SCHEMA)
    complete_json("system", "user", SCHEMA, effort=ANTHROPIC_ROUTE_EFFORT)

    first, second = anthropic_api["requests"]
    assert first["output_config"]["effort"] == ANTHROPIC_EFFORT
    assert second["output_config"]["effort"] == ANTHROPIC_ROUTE_EFFORT


def test_a_thinking_block_before_the_answer_is_skipped(anthropic_api):
    """With thinking on, the answer is not the first block. Reading
    `content[0]` would find an empty thinking block instead of the JSON."""
    anthropic_api["queue"] = [(200, message(content=[
        THINKING, answer({"title": "Summary", "tldr": "the gist"})]))]

    result, err = complete_json("system", "user", SCHEMA)

    assert err is None
    assert result == {"title": "Summary", "tldr": "the gist"}


def test_braces_inside_the_answer_cannot_corrupt_it(anthropic_api):
    """The old extraction was `re.search(r"\\{.*\\}", raw, re.DOTALL)` — greedy,
    so a closing brace in the text broke the parse. The answer is now parsed as
    one JSON document, so a brace inside a value is just a character."""
    anthropic_api["queue"] = [(200, message(content=[
        answer({"title": "Use {braces} carefully }", "tldr": "the gist"})]))]

    result, err = complete_json("system", "user", SCHEMA)

    assert err is None
    assert result["title"] == "Use {braces} carefully }"


def test_an_answer_that_is_not_json_is_an_error_not_a_crash(anthropic_api):
    """Structured output makes this all but impossible; if it ever happens, it
    is reported as a sentence, not raised out of the client."""
    anthropic_api["queue"] = [(200, message(content=[
        {"type": "text", "text": "Here is the summary you asked for."}]))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "not valid JSON" in err and "Nothing was saved" in err


def test_an_answer_that_is_not_an_object_is_named(anthropic_api):
    anthropic_api["queue"] = [(200, message(content=[answer(["a", "list"])]))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "list" in err and "object" in err


def test_a_response_with_no_answer_block_is_reported(anthropic_api):
    """Thinking and nothing else: there is no answer to read, and an empty dict
    would read as "nothing to change"."""
    anthropic_api["queue"] = [(200, message(content=[THINKING]))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "did not return a structured answer" in err


def test_a_missing_required_key_is_named(anthropic_api):
    """The API enforces the schema now, so this is the second line, kept because
    it is free: a caller that names its required keys gets a clear error instead
    of a KeyError three frames later, whatever reaches it."""
    anthropic_api["queue"] = [(200, message(content=[answer({"tldr": "no title"})]))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "title" in err


def test_the_model_name_comes_from_config(anthropic_api):
    complete_json("system", "user", SCHEMA)

    assert anthropic_api["requests"][0]["model"] == anthropic_client.ANTHROPIC_MODEL


def test_the_configured_model_is_not_the_retiring_one():
    """Claude Sonnet 4.5 retires on 2026-11-30; after that every Learn and
    Implement call fails. RED until the model moves."""
    assert anthropic_client.ANTHROPIC_MODEL != "claude-sonnet-4-5"


# ─── 4. TOKEN ACCOUNTING ───────────────────────────────────────────────────────

def test_every_call_logs_its_token_counts(anthropic_api, caplog):
    with caplog.at_level(logging.INFO, logger="clients.anthropic_client"):
        complete_json("system", "user", SCHEMA)

    assert "in=120" in caplog.text
    assert "out=45" in caplog.text


def test_spend_accumulates_across_calls(anthropic_api):
    anthropic_api["queue"] = [(200, message(input_tokens=1_000_000, output_tokens=0))]
    complete_json("system", "user", SCHEMA)
    after_one = anthropic_client.spend_today()

    anthropic_api["queue"] = [(200, message(input_tokens=1_000_000, output_tokens=0))]
    complete_json("system", "user", SCHEMA)
    after_two = anthropic_client.spend_today()

    assert after_one["usd"] == pytest.approx(anthropic_client.ANTHROPIC_INPUT_COST_PER_MTOK)
    assert after_two["usd"] == pytest.approx(after_one["usd"] * 2)
    assert after_two["calls"] == 2


def test_output_tokens_are_priced_higher_than_input():
    """Output is the expensive half; costing it at the input rate would badly
    under-count a long merge and let the guard sail past the budget."""
    assert (anthropic_client.estimated_cost(0, 1000)
            > anthropic_client.estimated_cost(1000, 0))


# Published per-million rates (input, output) of each model David has run on.
# The rates in config.py were a COMMENT's promise to move with the model; this
# makes it a test. Changing ANTHROPIC_MODEL turns this red until its rates are
# added here AND set in config. A stale rate is wrong in both directions: after
# a move to a dearer model it under-counts, and the daily guard lets the day
# spend past its budget.
PUBLISHED_RATES = {
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-sonnet-5-5": (2.00, 10.00),
}


def test_the_cost_rates_are_the_configured_models():
    assert anthropic_client.ANTHROPIC_MODEL in PUBLISHED_RATES, (
        f"no published rates recorded for {anthropic_client.ANTHROPIC_MODEL}")
    assert (anthropic_client.ANTHROPIC_INPUT_COST_PER_MTOK,
            anthropic_client.ANTHROPIC_OUTPUT_COST_PER_MTOK) == \
        PUBLISHED_RATES[anthropic_client.ANTHROPIC_MODEL]


# ─── 5. THE DAILY SPEND GUARD ──────────────────────────────────────────────────

def test_calls_are_refused_once_the_budget_is_spent(anthropic_api, monkeypatch):
    """A runaway loop is invisible until the invoice arrives."""
    monkeypatch.setattr(anthropic_client, "ANTHROPIC_DAILY_BUDGET_USD", 0.01)
    anthropic_api["queue"] = [(200, message(input_tokens=1_000_000, output_tokens=0))]
    complete_json("system", "user", SCHEMA)          # blows the budget
    sent_so_far = len(anthropic_api["requests"])

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "budget" in err.lower()
    assert len(anthropic_api["requests"]) == sent_so_far, "sent a request anyway"


def test_the_refusal_says_what_was_spent_and_how_to_lift_it(anthropic_api, monkeypatch):
    """It reaches Telegram as the command's error — every Anthropic call in David
    is user-initiated, so there is always someone reading it. A bare 'budget
    exceeded' would send them to the code."""
    monkeypatch.setattr(anthropic_client, "ANTHROPIC_DAILY_BUDGET_USD", 0.01)
    anthropic_api["queue"] = [(200, message(input_tokens=1_000_000, output_tokens=0))]
    complete_json("system", "user", SCHEMA)

    _, err = complete_json("system", "user", SCHEMA)

    assert f"${anthropic_client.ANTHROPIC_INPUT_COST_PER_MTOK:.2f}" in err
    assert "$0.01" in err
    assert "ANTHROPIC_DAILY_BUDGET_USD" in err
    assert "Nothing was sent" in err


def test_the_call_that_crosses_the_budget_is_allowed_to_finish(anthropic_api, monkeypatch):
    """The threshold is a floor on spend, not a ceiling: bounding it exactly would
    mean predicting the response size before making the request."""
    monkeypatch.setattr(anthropic_client, "ANTHROPIC_DAILY_BUDGET_USD", 0.01)
    anthropic_api["queue"] = [(200, message(input_tokens=1_000_000, output_tokens=0))]

    result, err = complete_json("system", "user", SCHEMA)

    assert err is None and result is not None


def test_spend_survives_a_restart(anthropic_api, monkeypatch):
    """Railway restarts. An in-memory counter would hand back a fresh budget on
    every deploy, which is exactly when a runaway loop gets redeployed."""
    anthropic_api["queue"] = [(200, message(input_tokens=1_000_000, output_tokens=0))]
    complete_json("system", "user", SCHEMA)

    monkeypatch.setattr(anthropic_client, "_spend", {"day": "", "usd": 0.0, "calls": 0})

    assert anthropic_client.spend_today()["usd"] == pytest.approx(
        anthropic_client.ANTHROPIC_INPUT_COST_PER_MTOK)


def test_a_new_day_starts_from_zero(anthropic_api, monkeypatch):
    anthropic_api["queue"] = [(200, message(input_tokens=1_000_000, output_tokens=0))]
    complete_json("system", "user", SCHEMA)

    monkeypatch.setattr(anthropic_client, "_today", lambda: "2099-01-01")
    monkeypatch.setattr(anthropic_client, "_spend", {"day": "", "usd": 0.0, "calls": 0})

    assert anthropic_client.spend_today()["usd"] == 0.0


def test_the_budget_day_is_the_project_day_not_the_servers(monkeypatch):
    """`_today()` was `date.today()` — the HOST clock, UTC on Railway — so the
    day's budget turned over at 01:00 or 02:00 Rome time, the one clock in
    David that did not use now_local(). 00:30 on 1 March in Rome is still
    28 February in UTC: the budget day must be the 1st.

    raising=False so that, on code that never imported now_local, the patch
    installs harmlessly and the assertion is what fails.
    """
    rome = pytz.timezone("Europe/Rome")
    monkeypatch.setattr(anthropic_client, "now_local",
                        lambda: rome.localize(datetime(2031, 3, 1, 0, 30)), raising=False)

    assert anthropic_client._today() == "2031-03-01"


def test_an_unwritable_spend_file_does_not_fail_the_call(anthropic_api, monkeypatch):
    """Losing the record is a bounded over-spend; refusing the command is not."""
    monkeypatch.setattr(anthropic_client, "SPEND_FILE", "/nonexistent-dir/spend.json")

    result, err = complete_json("system", "user", SCHEMA)

    assert err is None and result is not None


def test_a_missing_api_key_is_reported_before_any_request(anthropic_api, monkeypatch):
    monkeypatch.setattr(anthropic_client, "ANTHROPIC_KEY", None)

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "ANTHROPIC_API_KEY" in err
    assert anthropic_api["requests"] == []
