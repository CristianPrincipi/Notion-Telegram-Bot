# Plan: `Remind` gets two shorthands — `td` for today, `tr` for tomorrow

_Last updated: 2026-08-11_

Branch: `remind-td-tr`, off `main` at `a3aa627`.
Baseline before any change: **873 passed**, `ruff check .` clean.

`t` currently means tomorrow. It is the only one-letter date token, which is what
makes it a bad one: there is no room for today next to it, and the letter itself
does not say which day it picked. The command grows a matched pair instead —
`td` today, `tr` tomorrow — and `t` is retired.

Two decisions were taken before writing this, both in the direction the rest of
this command already leans (refuse rather than resolve):

1. **`t` is refused BY NAME, not dropped from the grammar.** It stays a legal
   token in `REMIND_PATTERN` and `parse_date_time` refuses it with a message
   naming both replacements. Dropping it from the pattern would produce the
   generic usage message, which does not say why the thing you have always typed
   stopped working — and the one outcome that must be impossible is `t` quietly
   meaning a different day than it used to.
2. **`td` with a time already past today is refused.** `t` could never name a
   past moment; `td` can. A calendar event in the past pings nothing — Google's
   1-hour alert is gone and the morning poll has already run — so booking it is a
   silent no-op confirmed as "Reminder set!". That is the exact shape of the bug
   `PAST_GRACE` exists for, one token over.

The split the command is built on is kept: **`REMIND_PATTERN` decides which
tokens are legal, `parse_date_time` decides what they mean.** Both refusals above
are meanings, so both live in `clients/calendar_client.py`.

## Milestone 1: The meaning layer — `clients/calendar_client.py`

- [x] Replace `TOMORROW_TOKENS = {"t", "tomorrow"}` with three sets:
      `TODAY_TOKENS = {"td", "today"}`, `TOMORROW_TOKENS = {"tr", "tomorrow"}`,
      `RETIRED_TOKENS = {"t"}`. The long forms stay — a shorthand nobody
      remembers is worse than none, which is why `tomorrow` was accepted already.
- [x] `parse_date_time`: refuse `RETIRED_TOKENS` first, before any date maths.
      The message names both replacements and echoes the time that was typed
      (`Use `tr 10` for tomorrow or `td 10` for today`) — it cannot name the
      appointment, because `parse_date_time` is not given it and passing the name
      down would be a handler concern leaking into the date rules.
      **Refused before `_parse_clock` too**, which was not in the plan: with a bad
      time as well, `t` is the thing to fix either way, and a message about the
      time sends the reader after the wrong problem.
- [x] `TODAY_TOKENS` branch: build today's date, `_localize` it (so a DST-broken
      hour is still refused, and refused for the DST reason rather than as
      "past"), then refuse if the result is **strictly** before `now`.
- [x] The past-today refusal names the resolved date, the time asked for and the
      time it is now, and offers both escapes: `tr` for tomorrow, or an explicit
      year (`06.08.2026`) to record something that already happened — an explicit
      year is already taken at face value, past or not, so the escape exists.
- [x] `dt == now` is NOT past. The boundary is asserted rather than left to luck.
- [x] The `TOMORROW_TOKENS` branch keeps its current behaviour unchanged, and its
      comment keeps saying why it needs no past check: tomorrow is a future
      calendar day by construction.
- [x] Update the three comments that name `t`: the token block above
      `TOMORROW_TOKENS`, `_localize`'s note on why a refusal names the date rather
      than the token, and `parse_date_time`'s docstring table of accepted forms.
- [x] The `Invalid date format` message (thrown for an unparseable `DD.MM`) still
      ends with "or `t` for tomorrow" — retarget it to `td`/`tr`.

## Milestone 2: The token layer — `reminder.py`

- [x] `REMIND_PATTERN`'s date group becomes
      `tomorrow|today|tr|td|t|\d{1,2}\.\d{1,2}(?:\.\d{2,4})?`, longest-first.
      ~~and a test pins every token rather than trusting that~~ — the test exists
      and passes, but the ORDER turned out not to be what makes it pass. See
      `## Changelog`; the comment now says so rather than claiming a guard.
- [x] The existing `(?![\w.])` lookahead is left exactly as it is — it already
      covers the new tokens (`td4` fails it the same way `t4` does). Verified by
      removal, with a test named for the new tokens, because a guard that only
      has a test for the token it was written against is half a guard.
- [x] Rewrite the pattern's comment block: it currently explains `t`, and the
      parenthetical about "today" being rejected by luck rather than design is now
      wrong — `today` is a legal token.
- [x] The usage message (`handle_remind`'s no-match reply) advertises `td` and
      `tr` with an example of each.
- [x] Module docstring examples updated.

## Milestone 3: The generated help — `david.py`

- [x] The `Remind` `Help` entry: usage lines gain `Remind [Name] td [HH]` and
      `Remind [Name] tr [HH]` in place of `Remind [Name] t [HH]`; the notes say
      `td` is today, `tr` is tomorrow, that the words work too, and that a past
      time today is refused rather than booked.
- [x] Help is generated from `COMMANDS`, so this is the only place the advertised
      forms live — and `test_the_help_advertises_only_date_forms_the_parser_accepts`
      runs them through the real pattern and parser.

## Milestone 4: Tests — `tests/test_reminder_dates.py`

**Retargeted (existing coverage, `t` → `tr`)** — these are not new guarantees,
they are the same ones under the new token, so they move rather than multiply:

- [x] The eleven tests naming `t`: the bare-hour form, the accepted-forms table,
      the multi-word name, the space requirement, the run-together time, the
      no-time case, the out-of-range hour, both DST cases, the never-in-the-past
      case, and both confirmation tests.
- [x] `test_today_does_not_silently_become_tomorrow` is **rewritten, not deleted**.
      Its subject — "today" must never resolve to tomorrow — is now enforced by
      the alternation rather than by the accident it documented, and that is worth
      more coverage, not less. It becomes an assertion that `today` and `td`
      resolve to TODAY.
- [x] `test_the_help_advertises_only_date_forms_the_parser_accepts` freezes at
      06:00 rather than 10:00. With `td [HH]` advertised and `[HH]` filled as
      `10`, a 10:00 clock makes the example land exactly on `now` — it would pass,
      on the boundary, for a reason unrelated to what the test is for.

**New coverage:**

- [x] `td` / `today` book today at the hour given.
- [x] `tr` / `tomorrow` book tomorrow — one parametrized test over all four live
      tokens asserting the day each names. ~~This is the alternation-order
      guard.~~ It is not; see `## Changelog`. It stays as the guard on the
      OUTCOME, which is the property that has to survive a rearrangement.
- [x] `td` with a time already past is refused: nothing is booked, the message
      names the current time and offers `tr`.
- [x] `td` for a time still ahead today is accepted.
- [x] `dt == now` is not refused (the boundary, mirroring the `PAST_GRACE` one).
- [x] A bare `t` is refused by name, and the message names BOTH `td` and `tr` —
      a message naming only one would be a nudge toward a specific day.
- [x] A bare `t` never reaches `create_event`, driven end-to-end through
      `handle_remind`.
- [x] `td4` / `tr4` inside a name do not become the date (the lookahead, under
      the new tokens).
- [x] `td` is DST-checked, and the DST refusal wins over the past refusal when
      both apply — the ordering is a decision, so it is asserted.
- [x] A `td` refusal names the resolved date, not the raw token.
- [x] The confirmation spells out today's weekday and date for `td`.
- [x] Two not in the original plan, both from writing the revert script: `t` is
      refused before the clock is parsed (an ordering decision that needed
      pinning), and a past `td` does not roll to next year (`PAST_GRACE` must not
      reach this path — rolling it would be the original bug with a new token).
- [x] **Guard-revert pass**: 12 guards reverted one at a time, each run against
      the test named for it. 11 went red. The 2 that did not are in
      `## Changelog` — one was a weak assertion and is fixed, one was never a
      guard and is now labelled as such in the pattern's comment.

## Milestone 5: The other test files

- [x] `tests/test_router.py`, `tests/test_async_io.py`, `tests/test_concurrency.py`
      all drive `Remind` with the long `12.06 - 14.30` form, so they should need
      no change. Confirmed by reading, then by running them — no edits needed.
- [x] `tests/test_learn_idempotency.py:230` refers to "`Remind`'s `t` lookahead"
      in a comment about a different guard. Update the reference so it points at
      something that still exists.
- [x] `tests/test_reminder_dates.py`'s module docstring gains the third subject it
      now covers — which DAY a shorthand names.

## Milestone 6: Docs

- [x] `CLAUDE.md` — the date table under "**`Remind` never guesses which moment
      you meant**" gains `td`/`today` and `tr`/`tomorrow` rows; the refusals list
      grows from four to six (the retired `t`, and a past time today), and the
      existing `t`-running-into-the-next-token bullet is retargeted. Plus a
      paragraph on why the shorthands are a two-letter PAIR, and one recording
      that the alternation order is not a guard.
- [x] `CLAUDE.md` module map, `reminder.py` row: "`t` becoming a date is the
      client's job" → the new tokens.
- [x] `README.md` command table row for `Remind`.
- [x] This file: steps ticked as they land, not batched at the end.

## Milestone 7: Ship

- [x] `ruff check .` clean, full suite green: **900 passed**, up from 873.
- [ ] Commit on `remind-td-tr`, push, open a PR, let CI go green before merging.
      Never straight to `main` — Railway keeps the old version on a failed deploy,
      so a broken one is silent.

## Changelog

- **The alternation order was written as a guard and is not one.** `REMIND_PATTERN`
  lists the tokens longest-first, which reads like the thing stopping `t` from
  claiming the head of `today`. Reversing it to `t|td|tr|today|tomorrow` left
  `test_every_date_token_resolves_to_the_day_it_names` green: the `(?![\w.])`
  lookahead fails `t` on the `o` and the engine backtracks to the branch that
  fits, so every order works. The order is kept for legibility and both the
  pattern comment and the test docstring now say plainly that it is not
  protection. This is the same class as the note already in the test it replaced —
  a line that reads like a guard and is not is worse than no line.
- **A refusal that names the same date twice needs an assertion that says which
  one.** `test_the_past_today_refusal_says_what_time_it_is` asserted
  `"06.08.2026" in err`. The message contains that date in two places: the clause
  saying what is past, and the `spell the date out` escape at the end. Rewording
  the first to "09.00 today is already past" — deleting exactly the thing the test
  claims to guard — left it green, satisfied by the escape. Now asserted as
  `"09.00 on 06.08.2026"`, and the sibling test asserts the escape in its
  backticked form for the same reason.
- **Two guards were found by writing the revert script, not by planning.**
  Refusing `t` before `_parse_clock` runs, and `PAST_GRACE` not reaching the `td`
  path. Both are ordering decisions that read as arbitrary until something asserts
  them; both now have a named test.

## Open questions

- **Nothing migrates old reminders.** Events already on the calendar are
  unaffected; this changes only what David accepts when creating one. No action
  needed, recorded so it is not mistaken for an oversight.
- **`td` is the only token that can now be refused for being past.** If that
  turns out to be annoying in practice — booking a just-finished meeting as a
  record — the escape is already there (spell out the date with its year), and the
  refusal says so. Loosening it would mean confirming "Reminder set!" for
  something that cannot remind.
