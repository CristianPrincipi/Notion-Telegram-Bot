# Plan: M6 — On-demand calendar: `Agenda`, and cancelling a reminder

_Last updated: 2026-08-15_

Branch: `claude/milestone-5-review-6-start-e1iq5r`, off `main` at the M5 merge
(`41281af`). Baseline before any change: **979 passed**, `ruff check .` clean.

`clients/calendar_client.py` already has `get_events_for_day` and
`_list_events_between`, and the only callers are the 07:30 and 20:00 jobs — **no
command in the registry reads the calendar.** And `Remind` creates events with
nothing anywhere to delete them.

This milestone adds the two commands that close that: `Agenda` (read-only) and
`Cancel [Name]` (destructive, so Hard Rule 4 applies in full).

## Decisions taken before writing code

**1. The pending-choice machine is GENERALISED, not forked, and there is ONE
pending slot.** `ROADMAP.md` asks for this decision explicitly. `expense_safety`
carries the machine today and is named for expenses; `Cancel` needs the same
"more than one match writes nothing" behaviour with different fields and
different wording.

A sibling module with its own `PENDING_KEY` would be two independent live lists,
and then a bare `2` means whichever one you had scrolled to — the exact ambiguity
`remember_pending`'s docstring already argues against *within* expenses ("David
prints one list at a time, so the only list a number can sensibly refer to is the
last one printed"). That argument does not stop at the feature boundary.

So the generic half moves to a new root module `pending_choice.py` — the TTL, the
strict-digits `parse_selection`, expiry, the range check, and **one** slot tagged
with the KIND that owns it. `expense_safety.py` keeps its whole public surface
and its expense-shaped `Choice` / `Pending` / `Undo`, built on top; a new
`calendar_safety.py` is its mirror for events. The dispatch loop in `david.py`
asks `pending_choice` whether a list is live and routes the number by kind.

The **undo record is the same shape of decision and gets the same treatment**:
one slot, tagged by kind, so `undo` reverses the last destructive thing David did
rather than the last destructive *expense*.

**2. `Cancel` records a reversal, and it re-creates rather than un-deletes.**
Google's `events.delete` has no archive an integration can flip back the way
Notion's does. The reversal is therefore an insert built from a snapshot taken
from the object the LOOKUP returned — the same ordering rule `expense_safety`
already states, for the same reason.

Be precise about what that buys, here and in the docs:

- **Restored:** summary, start, end, all-day-ness, description, location,
  recurrence and the event's own reminder overrides — a whitelist of the fields
  Google accepts on insert, copied verbatim off the snapshot.
- **Not restored:** the event ID (it comes back as a new event), and anything
  attached to that ID — attendee responses, the original creator, per-guest
  state. `Remind` creates none of those, which is why this is worth having at
  all, and the reply says so rather than claiming a clean undo.

**3. A day token means one thing in `Remind` and a slightly different thing in
`Agenda`, and the difference is stated, not hidden.** `parse_date_time` refuses a
`td` whose time has already passed and rolls a comfortably-past bare `DD.MM` to
next year. Both rules exist because *a reminder in the past never pings*. Neither
transfers to a read: "what did I have on Tuesday?" is a legitimate question.

So `clients/calendar_client.parse_day` is a second entry point beside
`parse_date_time`, and it takes a bare `DD.MM` **at face value in the current
year, past or not** — no rollover, no refusal. That is safe to do without
guessing because the reply NAMES the resolved date in full (weekday, day, month,
year), which is the same backstop `Remind`'s confirmation uses. The split
`CLAUDE.md` insists on is kept: the service's pattern decides which tokens are
LEGAL, the client decides what they MEAN.

**4. One event renderer, and it moves down a layer.**
`proactive/briefing._format_events_inline` is the renderer `Agenda` must reuse or
the two drift. `services/` cannot import `proactive/` upward-and-sideways and
`proactive/` importing a private from a sibling is worse, so it becomes
`services/agenda.format_events_inline` and `proactive/briefing.py` imports it.
`proactive/` already imports `budget` and `clients/`, so the direction is the one
that already exists.

**5. Two service modules, not one.** `services/agenda.py` is a read; `services/cancel.py`
is a find-then-mutate under a lock with an undo. Sharing a file would put the one
command that cannot write next to the one that Hard Rule 4 governs.

**6. `Agenda` runs INLINE.** It is read-only, so it cannot reorder against a
write the way a detached command could — the same reasoning `Get` already
carries. `Cancel` runs inline too: it is a short write, like `D e`.

**7. The `destructive` flag now covers three commands, and the router test that
asserts it has to be generalised in the same commit.**
`test_destructive_is_exactly_what_goes_through_the_expense_guard` hard-codes
`find_expense_matches`. `Cancel` goes through a *different* scoped finder, so the
test becomes a check that a destructive command routes through **one of the
declared scoped finders** — and `test_the_destructive_commands_are_the_two_expected_ones`
becomes three.

## Milestone 1: the client prerequisite

- [x] `_list_events_between` returns `"id"` on every item, and carries the raw
      Google item so a snapshot can be taken without a second read.
- [x] `delete_event(event_id) -> (ok, error)`, same retry/error shape as
      `create_event`.
- [x] `restore_event(body) -> (link, error)` + `restorable_body(raw)` — the
      whitelist of fields an insert can carry back.
- [x] `parse_day(token) -> (date, error)` — `td`/`today`, `tr`/`tomorrow`,
      `DD.MM`, `DD.MM.YYYY`, and `t` refused by name exactly as `parse_date_time`
      refuses it.
- [x] A comment at the `orderBy="startTime"` call site saying it is what makes
      "the first match" DEFINED, the way `CREATED_DESC` does for Notion.

## Milestone 2: the shared pending-choice + undo slot

- [x] New `pending_choice.py`: `PENDING_KEY`, `UNDO_KEY`, `PENDING_TTL_SECONDS`,
      `parse_selection`, `remember`, `has_pending`, `kind_of`, `take`, `clear`,
      `remember_undo`, `undo_kind`, `take_undo`.
- [x] `expense_safety.py` rebuilt on it with its public surface unchanged.
- [x] `david.handle_message` asks `pending_choice` and routes by kind.
- [x] `bot/undo.py` — `cmd_undo` peeks the kind and calls the owning service,
      which consumes the record itself.

## Milestone 3: `Agenda`

- [x] `services/agenda.py` — `format_events_inline` (moved) + `run_agenda`.
- [x] `proactive/briefing.py` imports the renderer instead of defining it.
- [x] A read failure, an empty day and a populated day are three distinguishable
      messages.
- [x] `bot/agenda.py`, `Command` + `Help` entry, inline.

## Milestone 4: `Cancel`

- [x] `services/cancel.py` — window-scoped `find_event_matches`, `run_cancel`,
      `run_cancel_selection`, `run_undo`.
- [x] `config.CANCEL_SEARCH_DAYS`; the window is refused, never widened.
- [x] `page_lock(CALENDAR_ID)` across lookup **and** delete; the ambiguous path
      releases it while it waits for a number.
- [x] `calendar_safety.py` — the choices, the messages, the undo record.
- [x] `bot/cancel.py`, `Command(destructive=True)` + `Help`.

## Milestone 5: docs

- [x] `README.md` — command table, "Cancelling a reminder", the write-locks
      table, the scheduled-messages note left alone.
- [x] `CLAUDE.md` — module map, write-locks table, the `Remind` section.
- [x] `ROADMAP.md` — tick M6.

## Milestone 6: tests

- [x] `tests/test_router.py` — rows, `SPY_TARGETS`, the two generalised registry
      tests.
- [x] `tests/test_agenda.py`, `tests/test_cancel.py`.
- [x] `tests/test_concurrency.py` — the new lock, the concurrent-cancel drive,
      `LOCKING_MODULES`.
- [x] `tests/test_async_io.py` — both commands offload.
- [x] **Guard-revert pass** on the Rule-4 guards.
- [x] Full suite green, `ruff check .` clean.

## Milestone 7: ship

- [x] Commit on `claude/milestone-5-review-6-start-e1iq5r`.
- [ ] Push, PR, CI green, merge.

## Found while building

- **`get_events_for_day` localises midnight with a plain `localize()`**, not the
  `is_dst=None` of `_localize`. Correct as it stands (midnight is never the
  Europe/Rome transition hour, and this is a read path over data Google owns),
  but it is the same call shape the DST work refused elsewhere, so it is worth
  knowing it was looked at rather than missed.
- **`services/reminder.py` still discards the `find_conflicts` error into `_`.**
  Named in `CLAUDE.md`'s open questions and unchanged here — it is not this
  milestone's, and a fix hidden inside a feature is a fix nobody reviewed.

## Guard-revert record

Seven guards, reverted one at a time, each watched turn a NAMED test red. A
guard that cannot go red reads like protection and is not.

| Guard reverted to | Turned red |
| --- | --- |
| `len(matches) >= 1` — act on the first match instead of refusing an ambiguous one | 6 tests, including `test_two_matching_events_delete_nothing_and_ask` |
| lock the DELETE only, leaving both lookups free to overlap | `test_two_overlapping_cancels_do_not_both_delete_the_same_event` |
| retry the failed window read at 365 days | `test_a_failed_lookup_is_refused_and_never_widened` |
| `events = []` on a failed calendar read, falling through to the renderer | `test_the_three_outcomes_are_three_different_messages` |
| `parse_day` rolls a past `DD.MM` to next year, as `parse_date_time` does | `test_a_bare_date_is_read_in_the_current_year_and_not_rolled_forward`, `test_the_reply_names_the_full_date_including_the_year` |
| record the reversal before checking the delete succeeded | `test_a_failed_delete_records_no_undo` |
| store the raw Google item instead of `restorable_body`'s whitelist | `test_the_undo_body_carries_no_read_only_fields` |

Every guard was restored and the full suite re-run afterwards: **1048 passed**,
`ruff check .` clean.

## Changelog

- **The `destructive` router test could not survive a second destructive
  command, and that was the point of it.** It asserted `find_expense_matches in
  chain == command.destructive`, so `Cancel` — which is destructive and never
  touches an expense — turned it red on the first run. Generalised to a declared
  set of scoped finders, which is the property that actually matters: a
  destructive command resolves its target through a scoped, ordered lookup
  before it writes.
- **`find_event_matches` is the offloaded unit, not the client call it wraps.**
  The first version of `test_cancel_runs_its_lookup_and_its_delete_off_the_loop`
  stubbed `get_events_in_window` and failed, correctly: `run_cancel` hands
  `find_event_matches` to `to_thread`, because the Google round trip and the
  title filter are one piece of blocking work. Mirrors how the destructive
  expense pair is stubbed at `find_expense_matches` rather than at
  `query_database`.
- **One pending slot rather than two was decided on the strength of an argument
  already written down.** `remember_pending`'s docstring says a second ambiguous
  command REPLACES the first because David prints one list at a time. Two
  feature-local slots would have quietly broken that: a live expense list and a
  live event list at once, with `2` meaning either.
