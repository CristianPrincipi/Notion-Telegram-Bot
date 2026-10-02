---
paths:
  - "services/reminder.py"
  - "services/agenda.py"
  - "clients/calendar_client.py"
  - "tests/test_reminder_dates.py"
  - "tests/test_agenda.py"
---

# Dates and times: `Remind` and `Agenda`

The grammar both commands share. Each guard below is a bug that shipped or nearly
did; the stories are in `docs/design-notes.md` § Dates and times. Read the note
before loosening a rule.

Accepted: date `12.06` · `12.06.2027` · `td`/`today` · `tr`/`tomorrow`; time
`14.30` · `10` (a bare hour means o'clock); the ` - ` between them is optional.

- `reminder.REMIND_PATTERN` decides which TOKENS are legal;
  `calendar_client.parse_date_time` (Remind) and `parse_day` (Agenda) decide what
  they MEAN. Never resolve a shorthand in the regex — nothing can unit-test it.
- `td` and `tr` are a matched pair, two letters on purpose; neither may be a prefix
  of the other, and the long forms stay.
- `Remind` refuses rather than resolves:
  - a bare `DD.MM` within the last 24 hours (`PAST_GRACE`); further back it rolls
    to next year;
  - `td` naming a time already past — `PAST_GRACE` must not reach this path, and
    the refusal offers both `tr` and the date with its year;
  - a bare `t`, refused BY NAME and naming BOTH `td` and `tr` — never read
    silently, never dropped from the grammar;
  - a local time that is not one real instant (`is_dst=None`), checked BEFORE
    "already past";
  - a shorthand running into the next token (the lookahead; it costs `td10`);
  - a run-together time (`1030`).
- `REMIND_PATTERN`'s longest-first alternation order is legibility, not
  protection — the lookahead is the guard. Do not re-promote the order to one.
- The `Remind` confirmation names the weekday and the full date, plus a line of
  its own when the year is not this year. It is the backstop for everything
  above: never make it terser.
- `Agenda` reads a bare `DD.MM` at face value in the current year, past or not —
  no rollover. That is safe only because its reply names the day in full; never
  remove that line.
- `t` is refused by name in both parsers, and both refusals name both
  replacements.
- `Agenda`'s three outcomes — a failed read, an empty day, a full day — are three
  messages. The error branch returns before rendering, because `[]` renders as
  "nothing scheduled" (`test_the_three_outcomes_are_three_different_messages`).
- Every guard here was verified by removing it and watching a named test go red.
  A new guard gets the same treatment.
