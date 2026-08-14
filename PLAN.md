# Plan: M3 — the Learn-nudge job (proactive Step 6)

_Last updated: 2026-08-14_

Branch: `learn-nudge`, off `main` at `4a2c3f1`.
Baseline before any change: **918 passed**, `ruff check .` clean.

Both Implement paths tick an `Implemented` checkbox (`services/implement.py:947`,
`services/implement_diet.py:722` and `:776`) and **nothing in the codebase reads
it**. `CLAUDE.md` lists it under Open questions; `proactive/__init__.py` lists it
as Step 6.

The consequence is structural rather than cosmetic: the capture half of the
knowledge pipeline is automated and the act-on-it half depends entirely on you
remembering which pages you saved and never merged. This closes the loop, and as
a side effect makes the checkbox mean something.

## Decisions taken before writing code

**1. "Pending" = `Implemented` unchecked AND created more than N days ago.**
Nudging about something saved an hour ago trains you to ignore the nudge. Both
halves go in ONE Notion filter (`{"and": [...]}`) rather than being fetched and
sieved here — the query is the right place for a predicate Notion can evaluate,
and a client-side copy would be a second definition of "pending".

**Consequence worth stating: the boundary is Notion's, not ours.** `before` is
strict, so a page created exactly N days ago is NOT yet nudged; it becomes
eligible a moment later. Since the sieve runs in Notion, no test of this module
can assert the boundary from its OUTPUT — the fake returns whatever rows it is
given. So the test asserts the **request**: that the cutoff sent is `now - N
days` and the operator is `before`. That is the whole of what decides the
boundary, and it is the honest thing to pin.

**2. A missing `Implemented` column is `(None, error)` — this job refuses.**
Deliberately the OPPOSITE asymmetry to `services/learn.py`'s `Source URL` check,
and the reason is that the two safeguards sit differently to their feature.
There, the dedup check is optional and refusing costs you the whole command, so
it degrades. Here the checkbox IS the feature: with no column there is no nudge
to degrade to, and the alternatives are listing every Learn page ever (noise) or
listing nothing (indistinguishable from "you are all caught up" — the exact
collapse M2 just finished removing). It is reported weekly, not silently.

`database_property_type` has three outcomes and all three are handled
separately: `("checkbox", None)` proceeds, `(None, None)` is the missing column,
`(None, error)` is a schema read that failed and says so in different words.

**3. Plain text, not Markdown.** Page titles are user data and can carry `*` and
`_`. `_run_job`'s default is plain and the briefings already use it; the nudge
carries nothing that needs formatting.

**4. No page links.** Telegram plain text cannot carry an inline link, and five
raw Notion URLs is a wall. Each row is a title plus its age, and the message
closes with the command shape (`Implement [page] - [Area]`) so the action is one
copy away.

**5. Cadence — weekly, Saturday 10:00.** Daily makes it noise. Every existing
slot is taken (00:05 rollover, 07:30 morning, 09:30 Sunday recap, 13:00 pacing,
20:00 evening, 20:30 Sunday heartbeat) and `test_no_two_jobs_share_a_slot`
asserts uniqueness, so the slot is deliberately clear of all six. Saturday
morning is when acting on it is realistic.

**6. `now_local()` is the clock.** Never `datetime.now()` — `CLAUDE.md` names
`clients/calendar_client.now_local` as the project clock, and both the cutoff and
every "N days ago" are computed from it.

## Milestone 1: config

- [x] `config.py`: `LEARN_NUDGE_DAY` (the named `SATURDAY` constant, **never a
      bare integer at a `run_daily` call site** — that is how the budget recap
      ran on the wrong days for months), `LEARN_NUDGE_HOUR`, `LEARN_NUDGE_MINUTE`,
      `LEARN_NUDGE_STALE_DAYS`, `LEARN_NUDGE_MAX_ITEMS`, with the reasoning above.

## Milestone 2: the builder

- [x] New `proactive/learn_nudge.py`, `build_nudge() -> (text, error)`, modelled
      on `proactive/budget_watch.py`. A builder never sends.
- [x] Schema pre-check via `database_property_type(LEARN_ID, "Implemented")`,
      all three outcomes handled distinctly.
- [x] `query_database` (it paginates) with the compound filter and
      `created_time` ascending, so the oldest debt is named first.
- [x] Cap at `LEARN_NUDGE_MAX_ITEMS` and name the overflow count. A list of forty
      is the same as no list.
- [x] Title via `get_page_title` — it resolves whatever the title column is
      called, which is the bug `search_page_in_db` had.
- [x] `LEARN_ID` unset → `(None, error)`, in the same family as
      `implement_diet`'s `DIET_ID` check.

## Milestone 3: registration

- [x] `proactive/scheduler.py` — `_learn_nudge_job` + a `run_daily` with
      `days=(LEARN_NUDGE_DAY,)`, `chat_id=`, `name="learn_nudge"`, following the
      heartbeat's shape exactly. Add it to the log line at the end of
      `register_all`.

## Milestone 4: tests

- [x] New `tests/test_learn_nudge.py`:
  - [x] Pages older than the threshold with `Implemented` unchecked are listed.
  - [x] A page checked as implemented is never listed — asserted through the
        FILTER, since that is where the exclusion happens.
  - [x] The boundary: the cutoff sent to Notion is `now - N days` and the
        operator is `before` (decision 1).
  - [x] Nothing pending → `(None, None)`, genuinely silent.
  - [x] A Notion read failure → `(None, error)`, **not** silence. Without this
        test the builder repeats the bug M2 exists to fix.
  - [x] A missing `Implemented` column reports, and its message differs from a
        failed schema read.
  - [x] More pending than the cap → the message names the overflow count.
  - [x] A title containing `*` and `_` does not break the send — driven through
        the real scheduler job, because that is where a parse_mode would be.
  - [x] Oldest first.
- [x] `tests/test_scheduler.py` — `test_every_job_is_registered` asserts the
      exact set and must gain the new name; plus its slot, its day, and that it
      still shares no slot with anything.
- [x] `tests/test_layering.py` passes untouched (`proactive/` is already scanned)
      — confirm rather than assume.
- [x] **Guard-revert pass**: four guards reverted one at a time — the read
      failure collapsed to silence, `if err` / `if prop_type is None` folded into
      one `if not prop_type`, the cap removed, and the sort flipped to
      descending. All four turned a named test red.
- [x] Full suite green: **939 passed**, up from 918. `ruff check .` clean.

## Milestone 5: docs

- [x] `README.md` — the "Scheduled messages" table.
- [x] `CLAUDE.md` — the Open-questions entry saying the checkbox has no reader is
      now false; strike it with the reason. Module map gains the row.
- [x] `proactive/__init__.py` — mark Step 6 implemented.
- [x] `ROADMAP.md` — tick M3 as it lands.

## Milestone 6: ship

- [ ] Commit on `learn-nudge`, push, PR, CI green, merge.

## Changelog

- **Two error cases were not in the plan and are now in the code.** A column
  named `Implemented` that is not a CHECKBOX (a text field, say) would make the
  filter a 400 reported weekly as an outage; it now says what the type is. And
  `database_property_type`'s failure branch needed wording that could not be
  mistaken for the missing-column one — "add a column" and "Notion is down" want
  opposite reactions, and the guard-revert pass confirmed a single
  `if not prop_type` merges them.
- **The age is decoration and is treated as such.** A page with no `created_time`
  is still listed, without an age, rather than dropped or shown as "0 days ago" —
  which would put the oldest debt in the list looking like the newest. The parse
  catches `TypeError` as well as `ValueError`, because `now_local()` is tz-aware
  and a naive stamp fails at the subtraction rather than at the parse.

## Open questions

- **Whether the `Implemented` column actually exists in the live database is not
  knowable from here.** `services/implement.py:947` discards `update_page`'s
  `(ok, error)`, so if the column is missing that write has been 400ing silently
  on every Implement run, and the first nudge will report the missing column
  rather than a list. That is the correct outcome either way — it surfaces a
  failure that has been invisible — but it is worth checking the Notion database
  before assuming the data is there. Fixing the discarded error itself is
  **out of scope**: it is a one-line change in a file this milestone does not
  otherwise touch, and it is already named in `ROADMAP.md`'s M3 notes.
- **Nothing un-ticks the checkbox.** A page merged into a Manual is implemented
  forever, even if the Manual section is later rewritten. Recorded so it is not
  mistaken for an oversight.
