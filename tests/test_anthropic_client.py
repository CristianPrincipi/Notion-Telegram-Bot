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

The HTTP layer is a real httpx MockTransport rather than a stubbed
`complete_json`, because the retry under test belongs to the SDK client — a
stub would assert that the test agrees with itself.
"""

import json
import logging

import anthropic
import httpx
import pytest

import anthropic_client
from anthropic_client import ANSWER_TOOL, complete_json

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "tldr": {"type": "string"}},
    "required": ["title"],
}


# ─── A FAKE ANTHROPIC ENDPOINT ─────────────────────────────────────────────────

def message(content=None, stop_reason="tool_use", input_tokens=120, output_tokens=45):
    """One /v1/messages response body."""
    if content is None:
        content = [{"type": "tool_use", "id": "toolu_1", "name": ANSWER_TOOL,
                    "input": {"title": "Summary", "tldr": "the gist"}}]
    return {
        "id": "msg_1", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-5", "content": content,
        "stop_reason": stop_reason, "stop_sequence": None,
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

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(json.loads(request.content.decode()))
        status, body = state["queue"].pop(0) if state["queue"] else (200, message())
        return httpx.Response(status, json=body)

    anthropic_client._thread_local.client = anthropic.Anthropic(
        api_key="test-anthropic-key",
        timeout=anthropic_client.ANTHROPIC_READ_TIMEOUT,
        max_retries=anthropic_client.ANTHROPIC_MAX_RETRIES,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
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
        content=[{"type": "tool_use", "id": "toolu_1", "name": ANSWER_TOOL,
                  "input": {"title": "Half a summ"}}],
        stop_reason="max_tokens"))]

    result, err = complete_json("system", "user", SCHEMA, max_tokens=8192)

    assert result is None
    assert "8192" in err and "cap" in err
    assert "Nothing was saved" in err
    assert "parse" not in err.lower(), "still reporting truncation as a parse failure"


def test_a_truncated_answer_is_never_partially_used(anthropic_api):
    """The half-written object is present in the response and must be discarded —
    a Manual merged from half a merge is worse than no merge."""
    anthropic_api["queue"] = [(200, message(
        content=[{"type": "tool_use", "id": "toolu_1", "name": ANSWER_TOOL,
                  "input": {"title": "Half"}}],
        stop_reason="max_tokens"))]

    result, _ = complete_json("system", "user", SCHEMA)

    assert result is None


def test_a_refusal_is_reported_plainly(anthropic_api):
    anthropic_api["queue"] = [(200, message(content=[], stop_reason="refusal"))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "declined" in err


# ─── 3. STRUCTURED OUTPUT — NO BRACE HUNTING ───────────────────────────────────

def test_the_answer_comes_back_as_a_parsed_object(anthropic_api):
    result, err = complete_json("system", "user", SCHEMA)

    assert err is None
    assert result == {"title": "Summary", "tldr": "the gist"}


def test_the_model_is_forced_to_answer_through_the_tool(anthropic_api):
    """This is what replaces the regex: there is no branch where the model
    replies in prose, so there is no text to hunt for braces in."""
    complete_json("system", "user", SCHEMA)

    body = anthropic_api["requests"][0]
    assert body["tool_choice"] == {"type": "tool", "name": ANSWER_TOOL}
    assert body["tools"][0]["input_schema"] == SCHEMA


def test_trailing_prose_with_braces_cannot_corrupt_the_answer(anthropic_api):
    """The old extraction was `re.search(r"\\{.*\\}", raw, re.DOTALL)` — greedy,
    so it ran from the first brace to the last one ANYWHERE in the response and
    a closing brace in trailing prose broke the parse. Text blocks alongside the
    tool call are now simply ignored."""
    anthropic_api["queue"] = [(200, message(content=[
        {"type": "text", "text": "Here you go! (note: use {braces} carefully.)"},
        {"type": "tool_use", "id": "toolu_1", "name": ANSWER_TOOL,
         "input": {"title": "Summary", "tldr": "the gist"}},
    ]))]

    result, err = complete_json("system", "user", SCHEMA)

    assert err is None
    assert result["title"] == "Summary"


def test_a_missing_required_key_is_named(anthropic_api):
    """The schema is advisory on the tool-use path, so the required keys are
    checked here — otherwise the caller meets a KeyError three frames later."""
    anthropic_api["queue"] = [(200, message(content=[
        {"type": "tool_use", "id": "toolu_1", "name": ANSWER_TOOL,
         "input": {"tldr": "no title"}}]))]

    result, err = complete_json("system", "user", SCHEMA)

    assert result is None
    assert "title" in err


def test_the_model_name_comes_from_config(anthropic_api):
    complete_json("system", "user", SCHEMA)

    assert anthropic_api["requests"][0]["model"] == anthropic_client.ANTHROPIC_MODEL


# ─── 4. TOKEN ACCOUNTING ───────────────────────────────────────────────────────

def test_every_call_logs_its_token_counts(anthropic_api, caplog):
    with caplog.at_level(logging.INFO, logger="anthropic_client"):
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

    assert "$3.00" in err and "$0.01" in err
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
