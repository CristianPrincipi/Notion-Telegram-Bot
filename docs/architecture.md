# Architecture

How David is built and why. [Features](features.md) describes what each command
does from the chat; this page is for changing the code. The rules contributors
must keep are stated tersely in [`CLAUDE.md`](../CLAUDE.md); this page carries
the reasoning.

David is a single-user Telegram bot that runs as one **polling worker**
(`python david.py`): no web server, no port, no database of its own. Notion
holds everything it knows.

## Access control

David is single-user by design: every command spends the owner's Notion and
Anthropic quota, and several write to or delete from Notion databases. Only the
Telegram account whose numeric ID matches `OWNER_ID` can use the bot.

Authorization is enforced with a python-telegram-bot filter, not a check inside
the handlers, so an unauthorized update is dropped by the dispatcher and never
reaches handler code — a newly added command cannot forget to check. Messages
from anyone else are logged at `WARNING` and answered with silence (a reply would
confirm to whoever probed the bot that it is live).

## Layers

```
david.py     entry point: the command registry, its dispatch loop, the generated
             help, job + handler registration, the global error handler
bot/         Telegram adapters — parse the update, call a service, send the reply
services/    the work itself: expenses, books, learn, implement, implement_diet,
             reminder, agenda, cancel, pkm, notion_ids, month
clients/     the wire: Notion, Google Calendar, Anthropic, Telegram file download
proactive/   the scheduled messages: one builder per job, plus the scheduler
config.py    constants, schedules, timeouts, the Notion schema and the
             environment contract
```

Dependencies point one way: **bot → services → clients**.

Nothing under `services/` may import `telegram` or take an `update` — services
report progress through a `notify` callback that the bot layer binds to
`reply_text`, a test binds to a list's `append`, and a job could bind to a
logger. `tests/test_layering.py` fails if that is ever broken, so the rule holds
by test rather than by habit.

## The rules the code keeps

Each one exists because its absence shipped a bug. The full statements, with the
history, are in [`CLAUDE.md`](../CLAUDE.md#hard-rules).

1. **Notion is the single source of truth.** No local database and no cached
   copy treated as authoritative — Railway's disk is wiped on every deploy. A
   cache is allowed only if deleting it is harmless.
2. **Never delete before the replacement is committed.** Notion has no
   transactions, so rewriting a section is: snapshot the old blocks, append the
   new ones, and only if that worked, delete the snapshot. A failure in between
   leaves old *and* new on the page — never neither.
3. **Never send a section the source did not touch.** Anything sent to the model
   can come back reworded, so `Implement` routes first (a cheap call over section
   *names*) and then merges only the affected sections.
4. **A destructive command never guesses which row it meant** — see below.
5. **Concurrency is deliberately narrow** — see below.

And the conventions the codebase relies on:

- **Fallible functions return `(value, error)`**, and an error is never the same
  value as an empty result: "the calendar is empty" and "the calendar could not
  be read" must never produce the same message.
- **Every Notion name lives in `config.py`'s NOTION SCHEMA**, enforced by a
  source scan (`tests/test_notion_schema.py`).
- **Everything sent with Markdown goes through `telegram_text`**, which escapes
  interpolated values; one stray `*` in a Notion title used to make Telegram
  reject a reply after the write had already succeeded.
- **A blank environment variable is an unset one**, read through
  `config.env_or`.

## Destructive commands

`U e` and `D e` find a row by name and then change it, and both used to act on
whichever match Notion returned first. Notion documents no ordering for query
results, so with two Coffees on the page the row that changed was arbitrary —
and the reply said "deleted successfully" either way. Three things now stand
between a command and the wrong row:

| | |
| --- | --- |
| Ordering | Every lookup sorts `created_time` **descending**, so "the first match" means the most recent one, the same way on every call |
| Scope | The search covers **this month only** — `D e Coffee` cannot reach a coffee from last December, and the row you mean is one of this month's anyway |
| Disambiguation | More than one match writes **nothing**. David lists the matches with their amount, date and category, and waits for a number |

The list lapses after **2 minutes**, after which the number goes back to being an
unrecognised message — a `2` typed an hour later must not archive a row you have
forgotten was offered. An out-of-range number leaves the list answerable, so a
mistyped `5` costs a keystroke rather than the whole command.

Every delete and update then records how to reverse itself, and **`undo`**
applies it. A delete is un-archived; an update is put back to the amount and
category it had, snapshotted from the row as it was found — Notion keeps no
property history an integration can read, so a snapshot taken after the write
would restore the new value over itself. `undo` is consumed when used, so it
cannot run twice.

If David cannot work out which month page to search, the lookup is **refused**
rather than widened. Falling back to an unscoped search would restore the exact
reach the month filter exists to remove, at the moment David is least sure of
its own state.

### `Cancel`

`Cancel [Name]` is the same shape of command as `D e`, on the calendar, and it
carries the same three guards:

| | |
| --- | --- |
| Ordering | Google is queried with `orderBy="startTime"`, so "the first match" means the earliest one, the same way on every call |
| Scope | The search covers **the next 30 days** from midnight today (`CANCEL_SEARCH_DAYS`). A failed read is **refused**, never widened to an unbounded one |
| Disambiguation | More than one match deletes **nothing**. David lists the matches with their weekday, date and time, and waits for a number |

Matching is a case-insensitive substring of the event **title only**. The
Calendar API's own search covers descriptions, locations and attendees too, so
`Cancel Gym` would have matched an unrelated event that merely mentions the gym —
a destructive command that matches on prose is one that surprises you.

**`undo` re-creates the event; it does not un-delete it.** Google has no
reversible archive, so the reversal is an insert built from a snapshot taken from
the event as the lookup found it — which is the only order that could work, since
after the delete there is nothing left to read. The confirmation says
"re-created" rather than "restored" because the event's ID, and everything Google
keys to it (guest replies above all), does not come back.

There is **one pending list and one undo record across every destructive
command**, not one per feature (`pending_choice.py`). David prints one list at a
time, so a bare number can only sensibly answer the last one printed — two
independent lists would make `2` mean whichever prompt you had scrolled to.

## Monthly rollover

Every expense relates to a month page through the Expenses `Account` column.
That page's ID used to be `MONTH_ID` in Railway, updated by hand on the 1st —
and forgetting did not fail loudly: expenses kept being written into *last*
month's page and `B` kept answering with last month's total.

`services/month.py` now answers "which page do this month's expenses belong to?"
from Notion instead. A month page is identified by its title, in one format —
`August 2026` — and each run:

1. uses the page titled `August 2026` (ignoring case and extra spaces), renaming
   it if the spelling differs;
2. or renames a single page titled bare `August` to `August 2026`;
3. or creates `August 2026` if neither exists.

so **running it twice cannot produce two pages for one month**. Ambiguity is
never guessed at: two pages titled `August` with no year is reported as an error
rather than picked from.

| | |
| --- | --- |
| When | `month_rollover`, 00:05 Europe/Rome — daily, though it only has work on the 1st, so a missed or failed rollover retries the next night instead of a month later |
| On demand | `Month` — the same idempotent call, and it prints the current page ID |
| Safety net | `current_month_id()` re-resolves when the cached month is older than today, so a rollover missed while David was redeployed is fixed by the first expense of the day rather than at the next midnight |
| Notification | Only when the month actually moved, when the page was created, or on failure — a nightly "still August" would train you to ignore it, and so would one per deploy |

The database the month pages live in is discovered from the Expenses `Account`
relation, so there is no second ID to keep correct; `MONTHS_DB_ID` overrides that
discovery.

### Where the answer is cached

**In memory, for the life of the process, and nowhere else.** The first
`current_month_id()` call in a fresh container asks Notion — two API calls — and
every call after that is a memory read until the month turns.

There used to be a `.month_state.json` alongside it. It saved a restart that one
resolve, which is not worth a persistence story on a platform that deletes the
file every deploy. But it had quietly acquired a second job: while the file
existed, the fallback behind it was never reached — and that fallback stamped
`MONTH_ID` with *today's* period, so `current_month_id()` saw a fresh-looking
cache and returned it **without asking Notion at all**. Every container that
booted without the file (i.e. every deploy) filed expenses against last month's
page until the next 00:05 job, and `B` answered for the wrong month. Both look
completely normal.

So the file is gone and a fresh process starts knowing nothing, which forces it
to ask. `MONTH_ID` keeps exactly one job: **the outage fallback.** If that first
resolve fails, David uses it rather than nothing — a stale page beats no page —
but only after Notion has been asked and could not answer.

## Concurrency

Every Notion, Anthropic and PyPDF2 call in David is a synchronous, blocking
call. python-telegram-bot runs updates on one event loop, so making one of those
calls directly inside an `async def` stops the **entire** bot for its duration —
no other command answered, no scheduled job fired. A `Learn video` on a long
transcript could sit in a 300-second Anthropic read and take David down with it
for five minutes.

So the blocking functions stay synchronous (they remain directly testable) and
every handler reaches them through `asyncio.to_thread`. The operations that could
otherwise run forever — the Anthropic calls, article and transcript fetches, PDF
parsing — also get an `asyncio.wait_for` cap from `config.py`, and answer with a
clean Telegram message when it fires.

Only **reads** are capped that way. `wait_for` cancels the waiting coroutine but
cannot cancel the worker thread, so timing out a write would report a failure
while it was still in flight. Notion calls are already bounded by
`clients.notion_client.notion_request`'s per-request timeout and its bounded
retries.

Notion requests reuse a pooled `requests.Session`, one per worker thread —
`requests.Session` is not thread-safe, and a shared one can hand the same socket
to two threads at once.

### What runs when

Freeing the event loop is not the same as letting two updates run at once.
python-telegram-bot will not look at the next update until the current handler
returns, so a five-minute `Learn` still held every other command behind it even
with the loop idle.

The fix is per-command, not a global switch. The long commands — `Learn`,
`Implement`, and both PDF upload paths — are dispatched as background tasks
(`bot.tasks.run_detached`, built on `Application.create_task` so failures still
reach the error handler and in-flight work is awaited on shutdown). Everything
else runs inline.

| | Runs | Ordering |
| --- | --- | --- |
| `Learn`, `Implement`, PDF uploads | detached, in the background | may finish in any order |
| everything else | inline, one at a time | strictly ordered |

**`concurrent_updates` stays off, deliberately.** It would add nothing on top of
the above and would cost the guarantee sequential dispatch still gives:
`Add e Carrefour 5` followed by `B` always reports the new total. Locks cannot
give that back — they stop two cycles interleaving, they do not decide which
runs first. `tests/test_async_io.py` fails if it is ever enabled.

### Write locks

Detached commands *can* overlap each other, so every find-then-mutate cycle is
serialised with `page_lock.py`:

| Cycle | Key | On contention |
| --- | --- | --- |
| Implement → area Manual | `area_db_id` | refused (a merge takes tens of seconds) |
| Implement → Diet page | `DIET_ID` | refused |
| `U e` / `D e` (lookup **and** write) | `EXPENSES_ID` | queues (writes take ~1s) |
| `Remind` | `CALENDAR_ID` | queues |
| `Cancel` (lookup **and** delete) | `CALENDAR_ID` | queues |

**Keys are always database ids, never page ids.** A page id is not known until
the lookup the lock has to cover, so keying on it forces the find-or-create
outside the lock — which is how the Diet flow could once build two Diet pages.
Database ids also keep the lock table bounded; a user-controlled key like an
expense name would not. `tests/test_concurrency.py` reads the call sites and
fails on any key that is not one.

`Add e` is deliberately unlocked: a bare create with no preceding read cannot
double-target a row.

The month rollover is a find-then-mutate cycle too, but it is **not** in that
table: it is reached from worker threads (an expense write resolving a stale
month, the nightly job) rather than from coroutines, and an `asyncio.Lock`
between two threads acquires without ever blocking. `services/month.py`
serialises it with a `threading.RLock` instead — same rule, right primitive.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The test suite runs fully offline: `tests/conftest.py` installs a fake
environment and every HTTP call is intercepted, so nothing reaches Notion,
Telegram or Google. CI (`.github/workflows/ci.yml`) runs the same two commands on
every push and pull request, on Python 3.12.

`tests/test_router.py` is the pre-deploy gate — a table of
`input → handler → parsed args` covering every command, driving the real
dispatcher. Rows marked `known_bug` assert current, wrong behaviour on purpose;
fixing one of those bugs is expected to turn its row red, and the row should be
updated in the same commit.

Several tests read the **source** rather than run it, because no runtime check
can tell a database ID from a page ID, or a service from a handler: the layering
rule, the Notion schema scan, the lock-key scan, the Markdown sender scan, the
environment-read scan. Each carries a test that it can actually fail.

**Deploys.** Pushing to `main` deploys on Railway, and a failed deploy keeps the
previous version running silently — so CI on the pull request is the real gate.
Work on a branch, open a PR, merge when CI is green.

### The automated review on a fork

`.github/workflows/claude-code-review.yml` reviews every pull request with
Claude. On a fork it needs a `CLAUDE_CODE_OAUTH_TOKEN` repository secret
(`claude setup-token`, then `gh secret set CLAUDE_CODE_OAUTH_TOKEN`); without
one, remove the workflow or ignore its failures.

If the review starts failing **within seconds**, with `is_error` and no tokens
spent, the token has stopped working — replace it before suspecting anything
else. A pull request that edits a workflow file is never reviewed: the action
refuses a workflow that differs from `main`'s, and GitHub shows that skip as a
pass.
