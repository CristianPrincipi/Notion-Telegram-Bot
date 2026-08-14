# Plan: M2 — `compute_budget()` returns `(value, error)`

_Last updated: 2026-08-14_

Branch: `budget-value-error`, off `main` at `97c1521`.
Baseline before any change: **913 passed**, `ruff check .` clean.

`budget.py:36` returns `dict | None`, and `None` means both *"Notion failed"* and
*"there is nothing to report"*. This is the collapse `CLAUDE.md` documents under
"An error is never the same value as an empty result" — the one that made the
evening briefing stop arriving with no signal and the morning briefing announce a
clear day during an outage. Fixed one layer up in `proactive/`, still present one
layer down, and two readers act on the collapsed value:

- `proactive/briefing.py:49` — `_budget_line` drops the budget line silently.
- `proactive/budget_watch.py:18` — `_should_warn` never fires, so a month you are
  overspending goes unreported for as long as Notion is unhappy.

Both are already asserted **as wrong on purpose** (`test_pacing_is_silent_when_notion_is_down`,
"KNOWN GAP, asserted so it is visible rather than forgotten"). Per the `known_bug`
convention, fixing this turns those rows red and they are rewritten in the same
commit.

## Decisions taken before writing code

**1. `budget()` changes contract too.** It is `str | None` today, read by
`david.send_budget_recap` and the `B` command. A weekly recap that goes quiet
during an outage has the same defect as the briefings did, and `david.py:121`
already prints "Could not fetch budget from Notion" on `None` — it is half-right
and just needs the real error interpolated. `format_budget(b)` is NOT fallible
and does not change.

**2. There is no third state.** `compute_budget` returns `(dict, None)` or
`(None, error)` — never `(None, None)`. A month with no expenses is a perfectly
good dict whose total is `0.0`, which is the whole point: the empty month and the
failed read stop being the same value.

**3. The morning briefing gains a visible line, not just an error.** The roadmap
says "send the briefing *without* the budget line **and** report the error". That
is right about the error and one step short on the message: the budget line
simply vanishing is a silent degradation of exactly the kind being fixed, and the
calendar half four lines above already has the pattern — say the half is unknown,
and return the error so it is reported too. So a budget failure adds
"⚠️ I could not read your budget." and the error goes back in the tuple.

**4. Two errors are joined, unlabelled.** The morning briefing is the only reader
with two sources, and `_run_job` takes one error. `" · ".join(...)` means a single
failure propagates VERBATIM — which keeps every existing assertion honest, and
they assert on the exact string the client produced — while a double outage
reports both rather than picking one. A Notion 401 and a Google 403 do not look
alike, and `_report_error` already prefixes the job name.

**5. `build_morning_briefing` can no longer return `(None, None)`, and that is a
behaviour change worth stating.** Today that branch is reachable only when the
calendar is empty AND the budget query returned `None` — i.e. only via the
collapsed error this milestone removes. After the fix the morning message always
has something to say, and a total outage produces a message saying both halves
are unknown plus both errors, where it used to produce silence plus one error.
The module docstring says so. The evening briefing is unaffected: it has no
budget half, and its `(None, None)` on a genuinely empty tomorrow stays.

**6. `_should_warn` keeps taking a dict.** The tuple is unpacked by
`build_pacing_warning`, which returns `(None, err)` before `_should_warn` is
consulted. A predicate that also carries error handling is two jobs in one
function, and its `if not b: return False` line is exactly the collapse being
removed.

## Milestone 1: `budget.py`

- [x] `compute_budget() -> tuple[dict | None, str | None]`. The `logger.error` on
      failure stays; the return becomes `(None, err)`.
- [x] Docstring: the return contract, and why the empty month is not the failure
      value.
- [x] `budget() -> tuple[str | None, str | None]`.
- [x] `format_budget(b)` untouched.
- [x] The module docstring's three-line API summary at the top is now wrong in two
      of its three lines — rewrite it.

## Milestone 2: the readers

- [x] `proactive/briefing.py` — `build_morning_briefing` unpacks both sources,
      adds the "could not read your budget" line, joins the errors. `_budget_line`
      keeps taking `dict | None` and stays a pure formatter.
- [x] `proactive/briefing.py` — module docstring: the morning briefing's
      `(None, None)` is gone, and both halves now degrade the same way.
- [x] `proactive/budget_watch.py` — `build_pacing_warning` returns `(None, err)`
      on a read failure; `(None, None)` only for a healthy month under the
      threshold. Its docstring currently explains the gap as unfixable-for-now —
      rewrite it. `_should_warn` now takes a dict, never None, and says why.
- [x] `david.send_budget_recap` — interpolate the real error. Plain text, and it
      stays plain: it is one of the three reporters `CLAUDE.md` exempts from
      `telegram_text` on purpose.
- [x] `bot/commands.py` — `cmd_budget` reports the error to the user, plain, for
      the same reason (a raw Notion error can carry `_` and `*`).

## Milestone 3: tests

- [x] `tests/test_budget.py` — `david.budget()` now returns a tuple at ~8 call
      sites. Retarget; assertions themselves should not change.
- [x] `tests/test_budget.py:160` `test_budget_returns_none_when_notion_rejects_the_query`
      and `:168` `test_budget_returns_none_on_notion_auth_failure` — both assert
      the wrong behaviour. Rewrite to assert the error comes back and is
      non-empty, and rename them off "returns_none".
- [x] `tests/test_budget.py:246` `test_there_is_only_one_budget_implementation`
      must still pass untouched — do not grow a second copy while refactoring.
- [x] `tests/test_briefings.py` — the `notion` fixture returns the pair. Keep its
      shape so the ~8 passing tests that set `notion["budget"] = budget(...)` do
      not change; add `notion["err"]` for the failure cases.
- [x] `tests/test_briefings.py:274` `test_pacing_is_silent_when_notion_is_down` —
      rewrite: a Notion failure now REPORTS. The "KNOWN GAP" docstring comes out.
- [x] `tests/test_briefings.py:148` `test_morning_still_sends_events_when_notion_is_down`
      asserts `err is None` on a Notion failure — the same collapse, rewritten in
      the same commit.
- [x] New: the morning briefing during a Notion outage still sends the calendar
      half **and** reports the budget error. The two must not be traded off.
- [x] New: both sources down → both errors reported, and the message says both
      halves are unknown (decision 5). Plus one asserting a LONE error is passed
      through verbatim, which is what decision 4 buys.
- [x] New: a genuinely quiet month (query succeeds, nothing to warn about) is
      still silent with no error. This is the assertion that stops the fix from
      turning every quiet day into an alert.
- [x] New: an empty month is a dict, not an error — `total == 0.0` and no error,
      driven through the real `compute_budget` against a 200-with-no-rows.
- [x] `tests/test_data_integrity.py:85,96` — two pagination tests read
      `david.budget()` and `compute_budget()["total"]`. Retarget only.
- [x] `tests/test_async_io.py:517` — `slow_budget` must return the pair, or the
      double it installs no longer describes the shape production returns. Four
      more doubles needed the same: `test_router`'s `SPY_TARGETS` entry,
      `test_async_io`'s `budget_stub`, two in `test_concurrency`, one in
      `test_expense_safety`.
- [x] **Guard-revert pass:** ~~collapse `compute_budget`'s failure back to a bare
      `None` and confirm a *named* test goes red in each of the three readers~~ —
      that is not how it works, see `## Changelog`. Done as FIVE reverts: the
      source, then each reader's own handling. All five turned a named test red,
      but only after the fourth reader got the test it never had.
- [x] Full suite green: **918 passed**, up from 913. `ruff check .` clean.

## Milestone 4: docs

- [x] `CLAUDE.md` — strike the first of the three named `(value, error)` gaps
      under "Open questions" with the reason, per the file's own convention;
      leave the other two. Module-map row for `budget.py` updated too.
- [x] `ROADMAP.md` — tick M2 as it lands; changelog entry if anything is found
      that the next milestone needs to know.

## Milestone 5: ship

- [ ] Commit on `budget-value-error`, push, PR, CI green, merge.

## Changelog

- **A source-level revert cannot reach a reader that stubs its source, so the
  planned revert pass would have proved nothing.** Collapsing `compute_budget`
  back to `(None, None)` turned only `tests/test_budget.py` red — every reader
  doubles `compute_budget` or `budget`, which is correct for those tests and
  makes them blind to the source. The pass was redone per READER: drop the error
  in `cmd_budget`, in `build_pacing_warning`, in `build_morning_briefing`, in
  `send_budget_recap`. Four reverts, four named tests.
- **The fourth of those found a hole rather than confirming a guard.**
  `david.send_budget_recap`'s failure branch had **no test at all**, so dropping
  `err` there turned nothing red. It is the least-noticed reader — nobody is
  waiting at the keyboard for a Sunday-morning job — and it now has two tests in
  `tests/test_error_reporting.py`, beside the reporters whose plain-text rule it
  shares. Written first, watched go red, then the code restored.
- **Five test doubles had to change shape, in four files that never mention
  budget maths.** `budget()` is stubbed by every command-level test that routes
  `B`. A double still returning a bare string would have gone on passing against
  a shape production no longer has — the same lesson as `conftest.written_ok`,
  one function over.

## Open questions

- **`calendar_client` returning `[], err` is the same class and is NOT in
  scope.** There the failure value IS the legitimate empty value, which is why
  every caller checks `err` first. It ripples into `find_conflicts`,
  `reminder.handle_remind` and three test files, and is named separately in
  `CLAUDE.md`.
- **`reminder.handle_remind` discards the `find_conflicts` error into `_`** — a
  deliberate degradation (a failed conflict check does not block the reminder),
  documented, and untouched here.
