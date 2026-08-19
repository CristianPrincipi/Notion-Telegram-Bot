# Plan: the Learn output cap is an anonymous literal that shadows the constant
_Last updated: 2026-08-19_

Branch: to be cut off `main` at `b09739d`.

## The bug

`Learn video https://youtu.be/A_kETOxjeWQ` failed with:

> Claude hit the 4096-token output cap and the answer was cut off mid-object.
> Nothing was saved. Try a shorter source, or raise ANTHROPIC_MAX_TOKENS.

`config.ANTHROPIC_MAX_TOKENS` is **8192**. `services/learn.py:470` passes
`max_tokens=4096`, so the one call that can hit an output cap runs at half the
configured budget — and the advice in the message is false twice over: raising
the constant would not have changed this call, and the constant is not read from
the environment, so "raise it" means a code change and a redeploy.

Same class as `SUMMARY_INPUT_CHARS` and `MANUAL_SOURCE_CHARS`: one budget, two
numbers. Here the literal shadows the constant rather than drifting from it.

## Milestone 1: one number per budget

- [x] Drop `max_tokens=4096` from `services.learn.summarize_with_claude` — it
      inherits `ANTHROPIC_MAX_TOKENS` like the two merge calls already do.
- [x] Name the routing budget: `config.ANTHROPIC_ROUTE_MAX_TOKENS = 2048`, read
      by `services/implement.py:374` and `services/implement_diet.py:377`. These
      are a genuinely different budget (a list of section names, not prose), but
      they are the same anonymous literal in two files.
- [x] Grep for any remaining bare `max_tokens=` outside `config.py`.

## Milestone 2: make the message's advice true

- [x] `ANTHROPIC_MAX_TOKENS` reads `os.environ` with 8192 as the default, so the
      lever the error names is one Railway variable away — a truncated summary
      is exactly when you want to raise it without a deploy.
- [x] Add it to `OPTIONAL_ENV` and the README env table.
- [x] Correct the config comment: the SDK's ~16k non-streaming refusal never
      fires here (`messages.create` only runs that check when the client timeout
      is the SDK default, and `_client()` sets one). The real ceiling is
      `ANTHROPIC_READ_TIMEOUT`; say that instead.
- [x] Reword the truncation error so it names the effective cap and says where
      to raise it, in the shape `_budget_error` already uses.

## Milestone 3: tests that go red without the fix

- [x] `tests/test_output_cap.py` (new file, not `test_learn.py` — there is none):
      the summarise call is made at `ANTHROPIC_MAX_TOKENS` —
      asserted by driving the real `summarize_with_claude` with a spy on
      `complete_json` and reading `max_tokens` off the call. Red at HEAD.
- [x] `test_anthropic_client.py`: an env override changes the cap the API is
      actually called with, not just the constant.
- [x] `test_anthropic_client.py`: the truncation message names the cap that was
      used. Red if a call site's literal ever shadows the constant again.
- [x] Full suite + `ruff check .` clean.

## Milestone 4: ship

- [ ] Branch, commit, push, PR, CI green, merge. Never commit to `main`.
- [ ] Re-run the failing command against the same URL and confirm it saves.

## Open questions

- **Is 8192 enough for this video?** Unknown until re-run — a ~50-minute podcast
  against a schema asking 5-7 sections of 2-4 paragraphs plus quotes and
  takeaways. 8192 is double what failed. If it still truncates, the env override
  from Milestone 2 is the answer rather than another literal, and the ceiling to
  watch is `ANTHROPIC_READ_TIMEOUT` (300s), not the SDK.
- **Not doing: an automatic retry at a higher cap.** It doubles the cost of the
  worst case silently, and the whole point of this fix is that the cap is one
  legible number.

## Changelog

**2026-08-19** — the tests landed in `tests/test_output_cap.py`, a new file,
rather than `test_learn.py`, which does not exist: Learn is covered across
`test_article_extraction`, `test_learn_idempotency` and `test_unverified_sources`
and none of them owns the cap. One file per budget matches `test_safe_writes.py`
and `test_partial_writes.py`.

Also added a SOURCE SCAN that was not in the plan. Driving the code cannot see
this bug — a run capped at 4096 and one capped at 8192 both succeed, and differ
only in how much of the podcast survived — so the behavioural test alone would
guard the one call site I happened to fix. The scan refuses an anonymous
`max_tokens=` literal anywhere under `bot/ clients/ services/ proactive/` and the
repo root, in the family of `test_layering.py`.

Every guard was verified by reverting the fix:
- `max_tokens=4096` back in `services/learn.py` → `test_no_call_site_passes_an_anonymous_output_cap`
  and `test_the_learn_summary_runs_at_the_configured_cap` both red.
- `ANTHROPIC_MAX_TOKENS = 8192` back to a literal →
  `test_the_environment_variable_the_error_names_reaches_the_api_call` red.

Suite: **1058 passed**, `ruff check .` clean.
