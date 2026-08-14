# David — implementation roadmap

_Last updated: 2026-08-13 · written against `main` @ `7e01e0b`_

Six milestones, each independently shippable. This is the **backlog**; it is not
a per-task plan.

## How to work from this file

1. **Pick one milestone.** They are ordered below by suggested sequence, not by
   impact — read "Suggested order" before choosing.
2. **Create `PLAN.md`** for that milestone only, following the format in
   `CLAUDE.md` → "Implementation Plan Tracking". `PLAN.md` is the per-task file
   that gets re-read every session; this file is the list of tasks. Copy the
   milestone's to-do list into it as the starting point and expand.
3. **Tick items here as they land**, in the same commit as the work. Never
   batch the updates at the end.
4. **One branch and one PR per milestone.** Never commit to `main` — Railway
   keeps the previous version running when a deploy fails, so a broken deploy is
   silent. CI (`ruff check .` + `pytest`) is the real gate.

**Running the suite on this machine:** use `.venv\Scripts\python.exe -m pytest`,
not the global `python`. Baseline at the time of writing: **900 passed**,
`ruff check .` clean. Every milestone below should end at ≥ 900 passing.

**Read `CLAUDE.md` first.** Every milestone here is constrained by its hard
rules — in particular Rule 2 (never delete before the replacement is committed),
Rule 4 (a destructive command never guesses which row it meant), the
`(value, error)` convention, and the layering rule that nothing under
`services/` may import `telegram` or take an `update` parameter.

## Suggested order

| Order | Milestone | Why here |
| --- | --- | --- |
| ~~1~~ ✅ | ~~**M1** — One truncation budget per Claude call~~ | Done. Smallest, self-contained, no new surface. Fixed a documented doctrine violation. |
| ~~2~~ ✅ | ~~**M2** — `compute_budget()` returns `(value, error)`~~ | Done. Blast radius was wider than "contained": eight test files touch `budget()`, because every command-level test doubles it. |
| ~~3~~ ✅ | ~~**M3** — The Learn-nudge job~~ | Done. Highest product value. Independent of everything else. |
| ~~4~~ ✅ | ~~**M4** — Takeaway of the week~~ | Done. Same shape as M3, and done while the proactive-builder pattern was fresh. |
| ~~5~~ ✅ | ~~**M5** — Split the last four `update`-taking modules~~ | Done. Pure refactor. Unlocks M6. |
| 6 | **M6** — On-demand calendar: `Agenda`, and cancelling a reminder | Wants `reminder.py` split first (M5). |

M1–M4 are independent of each other and of M5/M6 — reorder them freely. **M6
depends on M5** (at least on the `reminder.py` half of it).

---

# M1 — One truncation budget per Claude call ✅ done

_Landed on `implement-one-budget`. `PLAN.md` holds the decisions and the
guard-revert record._

## Why

`CLAUDE.md` states the rule plainly: *"A budget belongs to whatever consumes it,
and there is one of it."* It exists because the article extractor capped at
12,000 characters while the summariser accepted 100,000, so every long article
was summarised from its first eighth — silently, and indistinguishably from a
full summary.

**The Implement paths have the same bug, five times over, with three different
numbers, and none of them says so:**

| Site | Slice |
| --- | --- |
| `services/implement.py:331` — `route_sections` | `source_text[:60000]` |
| `services/implement.py:404` — `merge_sections` | `source_text[:60000]` |
| `services/implement.py:641` — `build_manual` | `source_text[:60000]` |
| `services/implement_diet.py:367` — routing | `summary_text[:50000]` |
| `services/implement_diet.py:432` — merge | `summary_text[:50000]` |

None of these relate to `config.SUMMARY_INPUT_CHARS` (`config.py:189`), which is
100,000 and is documented as *the* input budget. So a long Learn page merges
from its first 60k characters (50k on the Diet path) and the reply reads exactly
like a full merge. You find out from a thin Manual entry months later.

## Decisions to make before writing code

- **How many budgets are there really?** Implement's prompt carries section
  *contents* as well as the source, so its budget is genuinely not Learn's. The
  recommendation is **two new named constants** in `config.py` — one for the
  Manual path, one for the Diet path — rather than reusing
  `SUMMARY_INPUT_CHARS`. Two budgets, each named for what consumes it, is the
  rule; five anonymous copies of three numbers is the violation.
- **Where is the single cut?** `run_implement` reads the source once
  (`source_text = blocks_to_text(source_blocks)`, ~`services/implement.py:844`).
  Cut there. The three prompt builders must then **not** re-slice — same
  reasoning as "extractors must return the text WHOLE": if both the caller and
  the callee cap, a source of exactly the budget and one cut down to it are the
  same string and the warning becomes unreliable at its own boundary.
- **Does the unverified marker survive the cut?** `config.UNVERIFIED_MARKER` is
  written as the *first* block of a Learn page, so a head-slice keeps it and
  `is_unverified_source(source_text)` still fires. Confirm this and assert it —
  if it were ever written at the bottom, truncation would silently disarm the
  additions-only rule.

## To-do

- [x] Add ~~`MANUAL_INPUT_CHARS`~~ **`MANUAL_SOURCE_CHARS`** to `config.py`,
      beside `SUMMARY_INPUT_CHARS`, with a comment saying why it is a separate
      budget (the prompt carries section contents as well as the source) and
      that this is the only place the number lives. Named for the INPUT it
      bounds rather than for the path, so a second budget on the same path can
      be added beside it instead of overloading one number.
- [x] Add ~~`DIET_INPUT_CHARS`~~ **`DIET_SUMMARY_CHARS`** the same way. Kept
      separate from the Manual one, at its existing 50k; the comment does not
      claim a reason for the difference, because there isn't one beyond history.
- [x] Cut once in `run_implement`, immediately after `blocks_to_text`, before
      `is_unverified_source` is evaluated.
- [x] Cut once in the Diet entry point (`services/implement_diet.py`), the same
      way — through the same `fit_to_budget`, imported rather than copied, so
      there is one wording of the warning.
- [x] Delete `[:60000]` from `route_sections`, `merge_sections` and
      `build_manual`; add a one-line comment at each saying the caller owns the
      cap. The merge builders also say the SECTION texts are deliberately
      uncapped and why clipping them would be worse than the bug being fixed.
- [x] Delete `[:50000]` from both Diet prompt builders, same comment.
- [x] Report the truncation in the reply, matching `run_learn`'s wording — a
      partial merge you are told about is a different object from one you find
      months later. Also logged at WARNING with the pre-truncation length: the
      reply scrolls away, the log is what is left afterwards.
- [x] Update `CLAUDE.md`: the budget paragraph currently names only the
      Learn path. Add the Implement paths.

## Tests

- [x] A source longer than the budget is cut **exactly once** — assert the text
      handed to `route_sections` and `merge_sections` is already at the cap and
      that neither slices again. The two halves needed two different tests: the
      "already at the cap" half reads what the stubbed builders were given, the
      "neither slices again" half drives the REAL builders with text the caller
      never cut, which is the only way a second cap is visible at all.
- [x] The reply says the source was truncated, and says it only when it was.
- [x] A source of exactly `MANUAL_SOURCE_CHARS` is **not** reported as truncated
      ~~(the boundary — this is the assertion that proves the two-cap bug is
      gone)~~ — the boundary, but NOT the proof: see the changelog, it stays
      green with the second cap in place. Its docstring now says so.
- [x] The unverified marker survives truncation of a long recollection page, and
      `_hold_back_rewrites` still engages.
- [x] Same for the Diet path, in `tests/test_diet_routing.py` (nothing was needed
      in `tests/test_diet_tree.py` — it covers the write path, which this does
      not touch), minus `_hold_back_rewrites`, which the Diet path does not have:
      asserted there as the plan message still naming the source unverified.
- [x] **Guard-revert pass:** five guards reverted one at a time. Four turned a
      named test red; the fifth turned nothing red and **found a missing test** —
      see the changelog.
- [x] Full suite green (**913 passed**, up from 900), `ruff check .` clean.

---

# M2 — `compute_budget()` returns `(value, error)` ✅ done

_Landed on `budget-value-error`. `PLAN.md` holds the decisions and the
guard-revert record._

## Why

`budget.py:36` returns `dict | None`, and `None` means both *"Notion failed"*
and *"there is nothing to report"*. This is the exact collapse that
`CLAUDE.md` documents under "An error is never the same value as an empty
result" — the one that made the evening briefing stop arriving with no signal,
and made the morning briefing announce a clear day during an outage. It is
already fixed one layer up and still present one layer down.

Two readers act on the collapsed value:

- `proactive/briefing.py:49` — `_budget_line` silently drops the budget line
  from the morning briefing.
- `proactive/budget_watch.py:18` — `_should_warn` silently never fires, so a
  month you are overspending goes unreported for as long as Notion is unhappy.

The gap is **already asserted on purpose**:
`tests/test_briefings.py:274` — `test_pacing_is_silent_when_notion_is_down`,
whose docstring says "KNOWN GAP, asserted so it is visible rather than
forgotten." Per the `known_bug` convention in `CLAUDE.md`, **fixing this is
expected to turn that test red, and the row is updated in the same commit.**

## Decisions to make before writing code

- **`budget()` keeps its contract or changes it?** `budget.py:124` returns
  `str | None` and is called by `david.send_budget_recap` and the `B` command.
  Recommendation: change it too — a weekly recap that stays silent during an
  outage has the same defect. `david.py:115` already prints "Could not fetch
  budget from Notion" on `None`, so that call site is half-right already and
  just needs the real error interpolated.
- **What does the morning briefing say on error?** Follow
  `proactive/scheduler._run_job`'s three states: send the briefing *without* the
  budget line **and** report the error, rather than choosing one.

## To-do

- [x] `compute_budget()` returns `(dict, error)`; never a bare `None`.
- [x] `format_budget(b)` unchanged — it takes a dict and is not fallible.
- [x] `budget()` returns `(str, error)`.
- [x] `proactive/briefing.py` — `_budget_line` and `build_morning_briefing`
      propagate the error into the `(text, error)` tuple the scheduler already
      knows how to handle. Two additions beyond the letter of this line: the
      message SAYS the budget half is unknown (a vanished line is the same
      silent degradation, one level down), and two errors are joined so a double
      outage reports both instead of picking one.
- [x] `proactive/budget_watch.py` — `_should_warn` / `build_pacing_warning`
      likewise: a read failure is `(None, error)`, a healthy month under the
      threshold stays `(None, None)`. `_should_warn` now takes a dict — its
      `if not b: return False` WAS the collapse, one function over.
- [x] `david.send_budget_recap` — interpolate the real error instead of the
      generic sentence. Remember it sends **plain text**: it is one of the
      reporters `CLAUDE.md` exempts from `telegram_text` on purpose.
- [x] `bot/commands.py` — `cmd_budget` reports the error to the user.
- [x] Update `CLAUDE.md`'s "Open questions" — this is the first of the three
      named `(value, error)` gaps to close; strike it through with the reason
      rather than deleting the entry.

## Tests

- [x] `tests/test_budget.py:160` `test_budget_returns_none_when_notion_rejects_the_query`
      and `:168` `test_budget_returns_none_on_notion_auth_failure` — both assert
      the current wrong behaviour. Rewrite them in the same commit to assert the
      error is returned and is non-empty. Renamed off "returns_none", plus a new
      mirror asserting an EMPTY month is a dict and not an error.
- [x] `tests/test_briefings.py:274` `test_pacing_is_silent_when_notion_is_down` —
      rewrite: a Notion failure now *reports*, and the docstring's "KNOWN GAP"
      note comes out. `test_morning_still_sends_events_when_notion_is_down`
      asserted `err is None` on the same collapse and was rewritten with it.
- [x] New: the morning briefing during a Notion outage still sends the calendar
      half **and** reports the budget error — the two must not be traded off.
- [x] New: a genuinely quiet month (query succeeds, nothing to warn about) is
      still silent with no error. This is the assertion that stops the fix from
      turning every quiet day into an alert.
- [x] New, not planned: the Sunday recap's failure branch had **no test at all**
      — found by the guard-revert pass. See the changelog.
- [x] `tests/test_budget.py:246` `test_there_is_only_one_budget_implementation`
      must still pass — do not grow a second copy while refactoring. Untouched.
- [x] Full suite green (**918 passed**, up from 913), `ruff check .` clean.

---

# M3 — The Learn-nudge job (proactive Step 6) ✅ done

_Landed on `learn-nudge`. `PLAN.md` holds the decisions and the guard-revert
record._

## Why

Both Implement paths tick an `Implemented` checkbox
(`services/implement.py:883`, `services/implement_diet.py:699` and `:753`), and
**nothing in the codebase reads it.** `CLAUDE.md` lists it under Open questions;
`proactive/__init__.py` lists it as Step 6 of the roadmap.

The consequence is structural, not cosmetic: the capture half of the knowledge
pipeline is automated and the act-on-it half depends entirely on you remembering
which pages you saved and never merged. This closes the loop and, as a
side-effect, makes the checkbox mean something.

## Decisions to make before writing code

- **What counts as "pending"?** Recommendation: `Implemented` unchecked **and**
  created more than N days ago (N in `config.py`, start at 7). Nudging about
  something you saved an hour ago trains you to ignore the nudge.
- **How many does it list?** Cap it (3–5) and say how many more there are.
  A list of forty is the same as no list.
- **What if the column does not exist?** `clients/notion_client.py:219`
  `database_property_type(db_id, property_name)` already answers this and is
  cached per database. ~~Follow the `Source URL` asymmetry in
  `services/learn.py`: the safeguard is optional, so **degrade and say so
  once**, do not refuse.~~ **Decided the other way, and the to-do below already
  said so:** it REFUSES with an explanatory error. The `Source URL` asymmetry
  does not transfer, because there the safeguard is optional and refusing costs
  you the command, whereas here the checkbox IS the feature — there is nothing
  to degrade to, and both alternatives (list everything, list nothing) are worse
  than saying why.
  Note the checkbox write at `services/implement.py:883` currently discards
  `update_page`'s `(ok, error)` — if the column is missing, that write has been
  400ing silently on every run. Worth checking your Notion database before
  assuming the data is there.
- **Cadence.** Weekly is probably right — daily makes it noise. Pick a slot
  clear of the 20:00 evening briefing and the 20:30 Sunday heartbeat.

## To-do

- [x] `config.py`: nudge day/hour/minute constants (use the named weekday
      constants — **never a bare integer at a `run_daily` call site**; that is
      how the budget recap ran on the wrong days for months), the staleness
      threshold in days, and the list cap.
- [x] New `proactive/learn_nudge.py` with `build_nudge() -> (text, error)`.
      Model it on `proactive/budget_watch.py` — a builder never sends.
- [x] Query `LEARN_ID` with a compound filter (`Implemented` is `false`,
      `created_time` before the cutoff). Use `query_database`, which paginates.
- [x] Schema pre-check with `database_property_type`; on absence return
      `(None, <explanatory error>)` rather than an empty nudge.
- [x] Sort oldest-first and cap the list; name the overflow count.
- [x] Register in `proactive/scheduler.register_all` with a `name=` and a
      `chat_id=`, following the existing five jobs exactly.
- [x] Decide Markdown vs plain: page titles are user data and can contain `*`
      and `_`. Either send plain (like the briefings) or escape every
      interpolated title via `telegram_text.escape_md`.
- [x] `README.md` — add the row to the "Scheduled messages" table.
- [x] `CLAUDE.md` — the Open-questions entry saying the checkbox has no reader
      is now false. Strike it through with the reason.
- [x] `proactive/__init__.py` — mark Step 6 implemented.

## Tests

- [x] New `tests/test_learn_nudge.py`:
  - [x] Pages older than the threshold with `Implemented` unchecked are listed.
  - [x] A page checked as implemented is **never** listed — asserted on the
        FILTER, not the output. The sieve runs in Notion, so a fake returns
        whatever rows it is handed and an output-level assertion would pass
        against a filter asking for entirely the wrong thing.
  - [x] A page newer than the threshold is not listed (the boundary: exactly N
        days old — decide which side it falls on and assert it). Decided:
        `before` is strict, so exactly N days old is NOT yet named. Same caveat
        — asserted as the cutoff and the operator sent to Notion, which is the
        whole of what decides it.
  - [x] Nothing pending → `(None, None)`, i.e. genuinely silent.
  - [x] A Notion read failure → `(None, error)`, **not** silence. This is the
        whole point of the `(text, error)` contract; without this test the
        builder repeats the bug M2 exists to fix.
  - [x] A missing `Implemented` column reports rather than silently listing
        everything or nothing.
  - [x] More pending than the cap → the message names the overflow count.
  - [x] A page title containing `*` and `_` does not break the send — driven
        through the real scheduler job, since that is where a `parse_mode` would
        be added.
  - [x] Not planned, added: a failed schema read is not reported as a missing
        column, a column of the wrong TYPE says so, an undated page is still
        listed rather than shown as "0 days ago", and the cutoff comes from
        `now_local()` rather than `datetime.now()`.
- [x] `tests/test_scheduler.py` — the job is registered, with the right name and
      on the right day/time. `test_every_job_is_registered` asserts the exact set
      of names, so it fails until the new one is added — by design.
- [x] `tests/test_layering.py` should pass untouched (`proactive/` is already in
      the scanned package list) — confirm rather than assume. Confirmed.
      `tests/test_async_io.py`'s `PROACTIVE_JOBS` table also needed the new job:
      it is what asserts the builder runs off the event loop.
- [x] Full suite green (**939 passed**, up from 918), `ruff check .` clean.

---

# M4 — Takeaway of the week (proactive Step 5) ✅ done

_Landed on `takeaway-of-the-week`. `PLAN.md` holds the decisions and the
guard-revert record._

## Why

Listed as Step 5 in `proactive/__init__.py` and never built. A weekly message
resurfacing one `✅ Key Takeaways` bullet from a random Learn page.

Cheapest item on the roadmap: pure Notion read, **no Anthropic call**, no new
dependency, and it reuses `blocks_to_text`. The value is that a personal
knowledge base you only ever write to is not a knowledge base.

## Decisions to make before writing code

- **The heading string is about to exist in two places.** `services/learn.py:529`
  writes `_heading2("✅ Key Takeaways")` and this reader will look for it. That
  is exactly how `UNVERIFIED_MARKER` was nearly reworded in one place and
  silently undetected in the other. **Put the heading text in `config.py` and
  have both the writer and the reader use it** — one constant, same pattern as
  `is_unverified_source`.
- **Randomness must be injectable.** A test cannot assert against
  `random.choice`. Take the chooser as a parameter defaulting to the real one,
  or seed it — decide which and be consistent.
- **Pages with no takeaways section.** Older Learn pages, and every `Learn book`
  page whose summary came back without `key_takeaways`, will have none. Skip
  them and pick again; bound the retries so a database with no takeaways
  anywhere cannot spin.

## To-do

- [x] Move `"✅ Key Takeaways"` into `config.py` as a named constant; update
      `services/learn.py:529` to use it.
- [x] `config.py`: day/hour/minute for the job (named weekday constant).
- [x] New `proactive/takeaway.py` with `build_takeaway() -> (text, error)`.
- [x] Query `LEARN_ID`, choose a page, read its children, locate the takeaways
      heading, collect the bullets that follow it until the next heading.
- [x] Skip-and-retry for pages with no takeaways, with a bounded attempt count;
      exhausting it is `(None, None)`, not an error.
- [x] Include the source page title so the takeaway is traceable.
- [x] Register in `proactive/scheduler.register_all`.
- [x] `README.md` scheduled-messages table; `proactive/__init__.py` mark Step 5.

## Tests

- [x] New `tests/test_takeaway.py`:
  - [x] A page with takeaways produces a message naming the page.
  - [x] Bullets are collected only up to the next heading — not the whole rest
        of the page. Asserted on the COLLECTED LIST, not on the message: written
        the other way it stayed green with the boundary removed. See the
        changelog.
  - [x] A page with no takeaways section is skipped, not reported as an error.
  - [x] A database where **no** page has takeaways → `(None, None)` after a
        bounded number of attempts (assert it terminates).
  - [x] A Notion read failure → `(None, error)`. Split in two, because the
        distinction is the whole milestone: no takeaway found AND a read failed
        is `(None, error)`, while a takeaway found DESPITE a failed read is
        `(text, error)` — send and report, never one or the other.
  - [x] The chooser is injectable, so the test is deterministic.
- [x] A test that the writer and the reader use the **same** constant — write a
      page via the Learn builder, read it back via the takeaway builder. Two
      `in` checks in two modules is how a string gets reworded in one of them.
  - [x] Not planned, added: the same page is never opened twice (the bound
        counts pages, not dice rolls), a bullet BEFORE the heading is not a
        takeaway, and a takeaway lifted from an unverified page still says so.
- [x] `tests/test_scheduler.py` — registration, plus one asserting the three
      Sunday jobs stay an hour apart rather than merely not colliding.
- [x] `tests/test_async_io.py` — `PROACTIVE_JOBS` gains the job.
- [x] Full suite green (**962 passed**, up from 939), `ruff check .` clean.

---

# M5 — Split the last four `update`-taking modules ✅ done

_Landed on `split-update-modules`, one commit per module. `PLAN.md` holds the
decisions and the spy-retarget verification record._

## Why

`reminder.py`, `pkm.py`, `month.py` and `notion_ids.py` are still at the repo
root and still take `update`, replying through `telegram_text` themselves.
`CLAUDE.md` names them as deliberate follow-ups from the layering split, and
`bot/commands.py` exists only to hold their one-line adapters until this
happens.

Three concrete gains:

- **They become callable from a job** — M6's `Agenda` and M3's nudge both want
  this.
- **They become testable without a fake `Update`.**
- **They come under `tests/test_layering.py`**, which currently cannot see them
  because they are not under `services/`.

## Decisions to make before writing code

- **Order.** `reminder.py` first — M6 depends on it and it is the cleanest
  split. Then `pkm.py`, then `notion_ids.py`, then `month.py` **last**:
  `month.py` is imported by `budget.py`, `services/expenses.py` and
  `proactive/heartbeat.py`, and it carries a `threading.RLock` whose reasoning
  (worker threads, not coroutines) must survive the move intact.
- **One module per PR.** A four-module refactor in one PR is unreviewable, and
  `CLAUDE.md` is explicit that a fix hidden inside a move is a fix nobody
  reviewed. **Move code only.** Every defect you notice on the way — note it
  here in the backlog, do not fix it in the same commit.
  **Landed as one COMMIT per module on one branch**, because this file's own
  workflow section asks for one PR per milestone and the two rules collide.
  Each commit leaves `ruff check .` clean and the suite green on its own, so a
  reviewer still reads them one at a time; CI gates them once. Move-only held.
- **`_send_long`.** `pkm.py:221` and `notion_ids.py:271` each carry their own
  message-splitting helper. They are telegram concerns and must stay in `bot/`
  — and this is the moment to notice they are two copies of one thing.

## To-do

- [x] **`reminder.py`** → `services/reminder.py` (the work, `notify` /
      `notify_md`, no `update`) + a thin adapter in `bot/`.
      - [x] `REMIND_PATTERN` and the token grammar stay with the service; what a
            token *means* stays in `clients/calendar_client.py`. **Do not
            collapse that split** — a shorthand resolved in the regex is a date
            rule nothing can unit-test.
      - [x] The `page_lock(CALENDAR_ID)` acquisition moves with the service.
- [x] **`pkm.py`** → `services/pkm.py` + adapter; `_send_long` stays in `bot/`.
- [x] **`notion_ids.py`** → `services/notion_ids.py` + adapters for `Diag`,
      `Find`, `DBs`; its `_send_long` stays in `bot/` and is merged with pkm's.
- [x] **`month.py`** → `services/month.py` + adapter for the `Month` command.
      - [x] Update the importers: `budget.py`, `services/expenses.py`,
            `proactive/heartbeat.py`, `proactive/month_rollover.py`.
      - [x] The `threading.RLock` and its comment move unchanged.
- [x] Delete the now-empty one-line delegators from `bot/commands.py` as each
      module lands, or repoint them.
- [x] `CLAUDE.md` — module map rows, and strike the "five modules never got the
      treatment" entry under "Left by the layering split".
- [x] `README.md` — the Layout section.

## Tests

- [x] **`tests/test_router.py` `SPY_HOMES` must move with each function.** This
      is the one that bites: a spy only works if it is installed on the module
      whose namespace the caller resolves the name through *at call time*. A
      stub left on the old module keeps the test green against nothing. Verify
      by deliberately breaking the moved function and watching its router row go
      red.
- [x] `tests/test_layering.py` now covers four more modules — it should go red
      first if any `import telegram`, `update` parameter, or direct
      `reply_text` / `send_message` survives the move. Run it after each module,
      not once at the end.
- [x] `tests/test_reminder_dates.py` (812 lines), `tests/test_pkm.py`,
      `tests/test_month.py` (782 lines) — retarget imports; assertions should
      not need to change. **If an assertion changes, that is a behaviour change
      hiding in a refactor — stop and split it out.**
- [x] Add one test per module that drives the service with a list's `append` as
      `notify` and **no Update at all** — that is the proof the split worked,
      and it is what `conftest.with_update` exists alongside.
- [x] `tests/test_concurrency.py`'s lock-key scan reads source paths — check it
      still finds the `CALENDAR_ID` lock after `reminder.py` moves.
- [x] Full suite green after **each** module, not just at the end.

---

# M6 — On-demand calendar: `Agenda`, and cancelling a reminder

_Depends on M5 (`reminder.py` split)._

## Why

`clients/calendar_client.py` already has `get_events_for_day` and
`_list_events_between`, but the only callers are the 07:30 and 20:00 jobs —
**no command in the registry reads the calendar.** If you want to know what is
on at 3pm you wait until tomorrow morning. And `Remind` creates events with
nothing anywhere to delete them.

## Decisions to make before writing code

- **`_list_events_between` does not return event IDs.** Verified: each item is
  `{"summary", "start_dt", "end_dt", "all_day"}` (`clients/calendar_client.py:379`).
  Cancellation needs `ev.get("id")` added to the returned dicts, plus a new
  `delete_event(event_id)` in the client. **Do this first** — it is the
  prerequisite for the whole second half.
- **Cancelling is a destructive command, so Hard Rule 4 applies in full:**
  - Ordering must be *defined*. The calendar API is queried with
    `orderBy="startTime"`, which gives that for free — say so in a comment, the
    way `CREATED_DESC` does for Notion.
  - Scope must be bounded. Pick a window (today + N days) and **refuse rather
    than widen** if it cannot be resolved.
  - **More than one match writes nothing** — list them and wait for a number,
    reusing `expense_safety`'s pending-choice state machine and its 2-minute
    expiry. Note that machine currently lives in `expense_safety.py` and is
    named for expenses; decide whether to generalise it or add a sibling, and
    record the decision.
  - Take `page_lock(CALENDAR_ID)` across **lookup and delete**, not just the
    delete — splitting find-from-mutate is what let two queries resolve to the
    same row in the expense path.
- **Undo.** Every destructive write in David records its own reversal. A deleted
  calendar event can be re-created from the body you already hold — snapshot it
  from the object the *lookup* returned, never from a re-read. If you decide not
  to build undo, say so here explicitly rather than leaving it unmentioned.
- **Registry placement.** Adding a command means adding a `Command`, a
  `SPY_TARGETS` entry and router rows. Every pattern must be anchored on a
  distinct literal prefix — `test_no_input_can_be_claimed_by_two_commands`
  fails otherwise. Watch `Agenda` against the existing bare-word commands.

## To-do

### Prerequisite — the client

- [ ] `_list_events_between` returns `"id"` on each item.
- [ ] New `delete_event(event_id) -> (ok, error)` in
      `clients/calendar_client.py`, with the same retry/error shape as
      `create_event`.

### `Agenda` (read-only)

- [ ] Service function returning `(text, error)`, reusing `get_events_for_day`
      and the day tokens `Remind` already defines (`td` / `tr` / spelled out).
- [ ] Reuse `proactive/briefing._format_events_inline` rather than writing a
      second formatter — one renderer, or the two drift.
- [ ] A calendar read failure **says so**; an empty day says "nothing
      scheduled". These must not be the same message. This is the same rule the
      briefings were fixed for.
- [ ] `Command` entry + `Help` entry in `david.COMMANDS`. Runs **inline**, not
      detached: it is read-only.

### Cancelling

- [ ] Service function: find matches by name inside the bounded window.
- [ ] Zero matches → say so. One → confirm and delete. Several → list with times
      and wait for a number.
- [ ] Lock across lookup **and** delete on `CALENDAR_ID`.
- [ ] Record the reversal before reporting success, if undo is in scope.
- [ ] `Command` entry with `destructive=True` — the flag drives the shared
      warning in the generated help, and `tests/test_router.py` asserts the flag
      and the guarded path agree.

### Docs

- [ ] `README.md` command table, and a subsection for cancellation mirroring
      "Deleting and updating an expense".
- [ ] `CLAUDE.md` — module map, the write-locks table (a second `CALENDAR_ID`
      cycle), and the `Remind` section.

## Tests

- [ ] `tests/test_router.py` — rows for every new form, a `SPY_TARGETS` entry
      per new handler, and confirmation that no input matches two commands.
      **The registry tests fail until the table covers the new commands** — that
      is by design, not a problem to work around.
- [ ] `Agenda`: a populated day, an empty day, and a **failed read** produce
      three distinguishable messages. The third is the one that matters.
- [ ] `Agenda tr` reads tomorrow, not today.
- [ ] Cancel: two events with the same name write **nothing** and produce a
      numbered list.
- [ ] Cancel: answering with a number deletes the event that was offered at that
      index — assert the ID, not the name.
- [ ] Cancel: the pending list expires after 2 minutes and a later number is an
      unrecognised message again.
- [ ] Cancel: an out-of-range number leaves the list answerable.
- [ ] `tests/test_concurrency.py` — the new cycle is locked on a *database-level*
      id (`CALENDAR_ID`), never an event id. The lock-key scan reads the source
      and fails on anything else.
- [ ] A test driving two cancels concurrently through the real handlers, in the
      style of
      `test_no_two_expense_cycles_are_ever_in_flight_together`.
- [ ] `tests/test_async_io.py` — `Agenda` runs inline and does not block the
      loop (it is a network read; it must go through `asyncio.to_thread`).
- [ ] **Guard-revert pass** on the three Rule-4 guards: remove the multi-match
      refusal, remove the lock's coverage of the lookup, and widen the window —
      each must turn a named test red.
- [ ] Full suite green, `ruff check .` clean.

---

# Backlog — not scheduled

Smaller items found in the same survey. Not milestones; pick one up if a
related milestone puts you in the file already.

- [ ] `Add e` cannot backdate — every expense is filed at "now". Adding a date
      token means inheriting `Remind`'s refuse-rather-than-resolve discipline.
- [ ] `B` only reports the current month; no `B 07` / `B July`.
- [ ] Inline keyboard buttons for the ambiguous `U e` / `D e` list — one tap
      instead of typing a number. Needs an owner-gated `CallbackQueryHandler`,
      which is a new update type the access-control test must cover.
- [ ] `tests/test_anthropic_client.py::test_every_call_logs_its_token_counts`
      passes in the full suite and fails as a single file — an order dependency,
      probably the daily-spend state. Pre-existing.
- [ ] `tests/test_async_io.py::test_a_slow_command_no_longer_freezes_the_bot`
      can **hang** the suite rather than fail it: its watcher coroutine spins on
      `while not in_flight.is_set()` with no timeout. A bound turns that into a
      normal red.
- [ ] `Learn book` is not de-duplicated — it has no URL. The equivalent is a
      title match against `LETTI_ID`, which is a fuzzy-match decision of its own.
- [ ] `DATABASE_ID`, `LEARN_ID`, `DIET_ID`, `BRAIN_ID` and `FINANCE_ID` in
      `david.py` have no reader. Left deliberately during the layering split.
- [ ] `services/books.py` bounds `extract_quote_from_pdf` with
      `clients.telegram_files.DOWNLOAD_TIMEOUT_SECONDS` — right duration, wrong
      name.
- [ ] `bot/long_messages.py` splits by LENGTH, so a long Markdown report can be
      cut between an opening `*` and its closing one, leaving an unbalanced
      entity in each half. Inherited from `notion_ids.py`'s copy and mitigated
      rather than fixed: `telegram_text.reply` retries such a chunk plain. Now
      that there is one splitter, fixing it once is finally possible — it wants
      a chunker that tracks open entities across a boundary.
- [ ] `bot/long_messages.py` does not break a single line longer than the limit;
      it goes out as its own oversized chunk and Telegram rejects it. Nothing in
      David produces one today (the longest is a 64-char UUID in backticks), and
      inventing a hard split during a pure move would have been a fix nobody
      reviewed.

# Considered and declined

Recorded so they are not re-proposed.

- **Prompt caching for Implement's two Claude calls.** `route_sections` and
  `merge_sections` send the same `source_text` within one run, so caching looks
  like an obvious saving. It is not: caching is a prefix match over
  `tools → system → messages`, and the two calls have **different system prompts
  and different tool schemas** — the prefix diverges at position 0, so no cache
  entry could ever be read. Making it work means one shared system prompt across
  both calls, which is a large restructure for cents.
- **Voice notes → commands.** Would need a non-Anthropic transcription provider,
  breaking the "one model, one client" rule (`config.ANTHROPIC_MODEL` is the only
  place a model is named) for a feature the text commands already cover.

# Changelog

- **2026-08-14** — **M5 landed.** Three things worth carrying forward:
  - **A `SPY_HOMES` entry cannot be verified by reading it.** A spy only works
    if it is installed on the module whose namespace the caller resolves the
    name through at CALL time, and every wrong answer still *looks* right in the
    table. Each of the four was checked by bypassing its `handle_*` inside
    `cmd_*` and watching the router rows go red — 4 rows for `Remind`, 12 for
    the other three together. Do this on every move; it costs a minute and it is
    the only thing standing between the router table and passing against
    nothing.
  - **The two `_send_long` copies were never tested, and that was the
    transport's fault rather than an oversight.** Both were reachable only
    through a fake Update, and no test built a message long enough to split — so
    the merge into `bot/long_messages.py` could have stopped splitting entirely
    with the suite green. The same shape explains why `notion_ids.py` had no
    test file at all: when asserting anything costs you a fake Update, thin
    coverage is what you get. Splitting the module is what made five tests
    cheap.
  - **Which channel splits had to stay per-adapter.** `Get` splits the PLAIN
    channel (a retrieved section is arbitrary Notion content); `Diag`/`Find`/
    `DBs` split the MARKDOWN one (every ID is in a `code span`). A single global
    decision would have been one line shorter and would have silently cost one
    of the two — the IDs stop being one tap to copy, or a section with a stray
    asterisk stops arriving.

- **2026-08-14** — **M4 landed.** Two findings, one of them about testing:
  - **A test that asserts on the final MESSAGE cannot see a collection bug that
    lands outside the chooser's pick.** `test_the_bullets_stop_at_the_next_heading`
    was written end-to-end and stayed GREEN with the heading boundary removed:
    the deterministic chooser takes the first bullet, so a wrongly-collected
    extra one at the end never reached the text it asserted on. Found by the
    guard-revert pass, and the fix is the general lesson — put the assertion on
    what the guard PRODUCES (`takeaways_in`), and keep the end-to-end one with a
    chooser that picks the last item.
  - **Notion's write shape and read shape differ, and a writer/reader
    cross-check has to model the round trip.** `notion_client.rich()` emits
    `{"text": {"content": …}}`; Notion's response carries `{"plain_text": …}`,
    which is what `extract_rich_text` reads. Handing the writer's own blocks
    straight to the reader tests a shape production never sees. The test
    converts (`as_notion_returns_it`) rather than the reader widening to accept
    both — widening production code to satisfy a fixture is the wrong direction.
- **2026-08-14** — **M3 landed.** Two notes for whoever builds M4, which reads
  the same database:
  - **A predicate evaluated by Notion cannot be tested from the builder's
    output.** "Pending" is one compound filter, so the fake returns whatever
    rows it is handed and only the REQUEST carries the behaviour. The tests
    assert the filter body, and the boundary (`before` is strict, so exactly N
    days old is not yet named) is asserted there too. M4 chooses a page in
    PYTHON, so its equivalent assertions can be on the output — the difference
    is worth noticing rather than copying this file's shape blindly.
  - **The missing-column case refuses rather than degrades**, reversing this
    file's own recommendation. See M3's decision list for why the `Source URL`
    asymmetry does not transfer; the short version is that the checkbox is the
    feature, not a safeguard on it.
- **2026-08-14** — **M2 landed.** Three things worth carrying forward:
  - **The Sunday recap's failure branch had no test at all.** Found by the
    guard-revert pass: dropping `err` from `david.send_budget_recap` turned
    nothing red. It is the reader you are least likely to notice by hand —
    nobody is waiting at the keyboard for a Sunday-morning job — and it now has
    two tests in `tests/test_error_reporting.py`, next to the reporters it
    shares its plain-text rule with.
  - **A source-level revert cannot reach a reader that stubs its source.**
    Collapsing `compute_budget` back to a bare `None` turned only
    `tests/test_budget.py` red; every reader doubles `compute_budget` or
    `budget`, so each needed its own revert (drop the error in `cmd_budget`, in
    `build_pacing_warning`, in `build_morning_briefing`, in the recap). Four
    reverts, four named tests. Worth knowing before the next `(value, error)`
    gap: budget the revert pass per READER, not per function.
  - **Doubles are the real blast radius of a contract change.** Eight test files
    touch `budget()` and five install a double for it. Each one had to start
    returning the pair, or it would have kept passing against a shape production
    no longer has — the `written_ok` lesson, one function over.
- **2026-08-14** — **M1 landed.** Two things worth carrying forward:
  - **The accumulating inputs were checked and there is nothing there.** The
    obvious worry — that a Manual outgrowing `manual_text[:40000]`, or a Diet
    tree outgrowing `[:30000]`, is being silently clipped — is out of date by one
    milestone: both caps were removed by the sectioning work and survive only as
    prose describing the code that was deleted (`services/implement.py:12`,
    `services/implement_diet.py:296`, and two test docstrings).
    `test_a_huge_manual_does_not_get_its_tail_dropped` drives a 60,000-character
    section through the real path and asserts it arrives whole. Restoring a cap
    there would be a regression, not a safeguard: a section is sent whole or not
    at all and the merge's reply *replaces* it, so clipping the input deletes the
    tail from Notion. A ceiling on that half has to REFUSE the run.
  - **A guard-revert pass found a missing test, which is what it is for.**
    Putting `[:50000]` back into the Diet merge builder left the entire file
    green — because a builder capping at the number the caller already cut to is
    a no-op that no end-to-end test can observe. The boundary tests cannot see it
    either. `test_the_prompt_builders_do_not_cut_again` (both test files) drives
    the builders directly with text the caller never cut, and is the only thing
    standing between the code and a quiet return of the second cap.
- **2026-08-13** — File created from the repo survey at `7e01e0b`. Four ideas
  from that survey are deliberately **not** here: the model upgrade off
  `claude-sonnet-4-5`, receipt-photo expenses, Anthropic spend in the heartbeat,
  and the two discarded-error one-liners (`reminder.py:152`,
  `services/implement.py:883`). The second of those is referenced inside M3,
  because the nudge job makes the `Implemented` column load-bearing.
