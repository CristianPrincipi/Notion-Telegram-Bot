# Architecture

How David is built and why. [Features](features.md) describes what each command
does from the chat; this page is for changing the code. The rules contributors
must keep are stated tersely in [`CLAUDE.md`](../CLAUDE.md), and the reasoning
behind each one — usually the bug that produced it — is in
[Design notes](design-notes.md). This page is the overview.

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

## Module map

What each file owns — and, as important, what it must not.

| File | Owns | Must NOT own |
| --- | --- | --- |
| `david.py` | Entry point (`__main__`), the `COMMANDS` registry + its dispatch loop, the generated help (and `cmd_help`, which renders it), the owner filter and handler registration, job registration, `on_error` / `notify_error` | Any command's work, Notion, argument parsing beyond the patterns |
| `config.py` | Constants, schedule times, timeouts, shortcut maps, weekday constants, the unverified-source marker + `is_unverified_source`, `TAKEAWAYS_HEADING` (two layers need each, neither owns it) and `CANCEL_SEARCH_DAYS`, the NOTION SCHEMA (every column name and every page title David looks up, per database), the env contract (`REQUIRED_ENV`/`OPTIONAL_ENV`), `env_or` and `validate()` | Reading feature IDs — each module reads its own `os.environ` |
| `bot/notify.py` | `for_update(update) -> (notify, notify_md)` — the **only** place a service's callbacks are bound to a message | Anything a service could decide |
| `bot/tasks.py` | `run_detached` — the per-command decision to background a long one | Which commands are long (that is the registry) |
| `bot/expenses.py` | `Add e` / `U e` / `D e` / a bare number: the `AMOUNT` grammar, `parse_amount`, `resolve_category` | Which row a command means, the lock, the undo — and `undo` itself, which is `bot/undo.py` now that it spans two services |
| `bot/books.py` | `Add b` and `Add q` in their typed form | Notion, PyPDF2 |
| `bot/learn.py`, `bot/implement.py` | The `update`-taking wrappers, and (for Learn) the PDF upload, which needs a `context.bot` | Extraction, merging, routing |
| `bot/documents.py` | `handle_document` — the caption router for uploads | The work either caption triggers |
| `bot/reminder.py`, `bot/pkm.py`, `bot/notion_ids.py`, `bot/month.py`, `bot/agenda.py`, `bot/cancel.py` | The adapters: bind the notify pair, call the service, nothing else | Any of the work — and any second copy of the message splitter |
| `bot/undo.py` | `undo` — which SERVICE reverses the kind of thing last destroyed (`REVERSERS`). Peeks the kind, never consumes the record | How to reverse anything; the take-and-put-back pair stays in the service that knows when a reversal did not happen |
| `bot/budget.py` | `B`. The one handler that never needed a split: `budget.py` is telegram-free, so this does the offloading and picks the channel itself | Aggregation, recap wording |
| `bot/long_messages.py` | `split_for_telegram` / `send_long` — the **one** splitter for a reply over Telegram's limit, and it is bound where `notify` is | Which channel splits — that is each adapter's decision |
| `services/expenses.py` | The expense writes, `find_expense_matches`, the `EXPENSES_ID` lock, and the find-choose-write cycle | Telegram, argument parsing |
| `services/books.py` | Book + quote writes, `extract_quote_from_pdf`, the quote-from-PDF flow (its download is INJECTED) | Fetching from Telegram |
| `services/learn.py` | `Learn [type] [source]` — extract, Claude-summarise, write to Notion. Owns the trafilatura→BS4 parser ladder, the one place source text is cut to fit, and URL identity (`normalise_source_url`, the duplicate check on the `Source URL` column and its ` !` override) | Manual merging; the unverified marker's TEXT and any column NAME (those are `config.py`) |
| `services/implement.py` | `Implement [Page] - [Area]` — index a Manual by heading, route, merge and rewrite **only** the affected sections. Owns `get_area_db_id`, the `📚 Sources` ledger (`record_source`, and the two guards that keep it unwritable by a merge) and the additions-only rule for unverified sources | Diet (delegates to `services/implement_diet.py`); the marker's TEXT (that is `config.py`) |
| `services/implement_diet.py` | The Diet page's H1>H2>H3 toggle tree: skeleton, breadth-first read, surgical updates | Generic Manual merging |
| `clients/notion_client.py` | The **only** place that speaks HTTP to Notion: headers, per-thread `Session`, retry/backoff, pagination, block builders | Any feature logic |
| `clients/anthropic_client.py` | The **only** place that speaks to Anthropic: `complete_json`, retry, `stop_reason` checks, token logging, the daily spend guard | Prompts — each feature owns its own system prompt and schema |
| `clients/calendar_client.py` | The **only** place that speaks to Google Calendar; per-thread service. `now_local()` is the project clock — never `datetime.now()` | Telegram, Notion |
| `clients/telegram_files.py` | Attachment validation and the bounded PDF download | What the bytes are for |
| `page_lock.py` | Per-database asyncio locks (`page_lock`, `PageBusy`) | Anything else |
| `telegram_text.py` | `escape_md`, and the **only** safe senders (`reply`, `send`) — the sole place `parse_mode` reaches Telegram | Feature logic, message wording |
| `observability.py` | `setup_logging`, the correlation-ID contextvar, the heartbeat counters | Telegram, Notion, any probe |
| `pending_choice.py` | The ONE pending slot and the ONE undo slot, both tagged by the kind that owns them, plus the rules that must not differ between two destructive commands: the 2-minute expiry, the strict-digit selection, the range check | Which fields tell two matches apart, any message wording, the shape of a reversal — those are per-feature |
| `expense_safety.py` | The expense half of the above: `Choice` / `Pending` / `Undo` shaped for a Notion row, and every message `U e` / `D e` print | Notion calls, Telegram sends — it decides and formats, `services/expenses.py` acts. And the machine itself, which is `pending_choice.py` |
| `calendar_safety.py` | The same, for `Cancel`: what tells two same-named events apart, and the re-create-from-snapshot undo record | Google calls, Telegram sends — `services/cancel.py` acts |
| `services/month.py` | Which page this month's expenses relate to: naming, find-or-create, cache, `run_month` | Expense writes, budget maths |
| `budget.py` | Expense aggregation + recap text (`compute_budget`, `format_budget`, `budget` — the two fallible ones return `(value, error)`) | Notion HTTP, Telegram |
| `services/pkm.py` | `Get [Topic] - [Area]` — read a section back out of a Manual: index, fuzzy resolve, discovery. Read-only, no Claude call | Writing anything; knowing how Manuals are built |
| `services/reminder.py` | `Remind …` — the command pattern (which tokens a date and a time may be), conflict-check, create the calendar event | Calendar HTTP (that is `clients/calendar_client.py`), and what a token MEANS — `td` becoming a date, and `t` becoming a refusal, are the client's job |
| `services/agenda.py` | `Agenda [day]` — read one day back out of the calendar, and `format_events_inline`, the ONE event renderer (`proactive/briefing.py` imports it) | What a day token means (`parse_day`, in the client); sending |
| `services/cancel.py` | `Cancel [Name]` — the window-scoped `find_event_matches`, the `CALENDAR_ID` lock over lookup **and** delete, and the re-create undo | The window's SIZE (that is `config.CANCEL_SEARCH_DAYS`); the messages (that is `calendar_safety.py`) |
| `services/notion_ids.py` | `Diag` / `Find` / `DBs` — read-only ID + schema diagnostics | Any write |
| `proactive/` | Scheduled push messages. One builder module per feature; `scheduler.py` does all JobQueue wiring and sending. Never imports `david.py` | Sending from a builder — builders return `(text, error)` |
| `proactive/heartbeat.py` | `build_heartbeat` — the weekly liveness proof; runs the Calendar/Notion/month probes | Sending (that is `scheduler.py`) |
| `proactive/learn_nudge.py` | `build_nudge` — the weekly list of Learn pages never merged into a Manual. Owns what "pending" means (one Notion filter) | Sending; un-ticking the checkbox (nothing does); the `Implemented` column's NAME, which both Implement paths write and so lives in `config.py` |
| `proactive/takeaway.py` | `build_takeaway` — one takeaway bullet resurfaced weekly. Owns finding the takeaways section in a page (`takeaways_in`) and the bounded skip-and-retry over pages that have none | Sending; the heading's TEXT (that is `config.TAKEAWAYS_HEADING`) |

`budget.py` is the last module at the root, and it belongs there: it is
telegram-free already, so `bot/budget.py` is a real adapter rather than a
placeholder. The other four — `month.py`, `pkm.py`, `reminder.py`,
`notion_ids.py` — were split into `services/` + `bot/` and are now under
`tests/test_layering.py`, which could not see them at the root.

New features get a module. `david.py` routes to them; it does not absorb them.

## The rules the code keeps

Each one exists because its absence shipped a bug; the history is in
[Design notes](design-notes.md#hard-rules).

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

On this repository that is enforced: `main` is protected, and a merge needs a pull
request, up to date with `main`, with the `test` check green — for the owner too.
A fork starts unprotected. To get the same, add a branch protection rule for `main`
under Settings → Branches: require a pull request (no approvals, if you work
alone), require the `test` status check — with GitHub Actions as its source, not
"any source" — and the branch to be up to date, and do not allow administrators
to bypass it. The reasons are in the
[design notes](design-notes.md#main-is-protected-its-owner-included).

**Shipping with Claude Code.** `/ship-feature` (`.claude/skills/ship-feature/`)
turns a finished branch into a pull request: it checks the diff against the
Documentation contract in `CLAUDE.md` and updates the docs and `CHANGELOG.md` the
change requires, runs `ruff` and `pytest`, opens the PR from a fixed template and
waits for CI and the review. It never merges.

### The automated review on a fork

`.github/workflows/claude-code-review.yml` reviews every pull request with
Claude. On a fork it needs a `CLAUDE_CODE_OAUTH_TOKEN` repository secret
(`claude setup-token`, then `gh secret set CLAUDE_CODE_OAUTH_TOKEN`); without
one, remove the workflow or ignore its failures.

**The review's answer is its comment, not its green check.** When it finds
something it comments inline on the pull request, as `claude[bot]`. A green check
with no comment has told you nothing yet: open the review job and read its last
step, "Show what the review concluded", which prints the review's final message
and the tool calls it was refused. That is the only place that says whether the
review finished or stopped. Check a finding before applying it — the review can
be right about the line and wrong about the reason.

The workflow needs three settings to review and report, and ran without any of
them until October 2026: `--comment` at the end of the prompt, the inline-comment
tool in `--allowedTools`, and `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` in
`settings`. `tests/test_review_workflow.py` fails if one goes missing;
[the design notes](design-notes.md#the-reviews-answer-is-its-comment) have the
story. A review usually takes 6 to 9 minutes and has taken 30; the job is
cancelled at 45.

If the review starts failing **within seconds**, with `is_error` and no tokens
spent, the token has stopped working — replace it before suspecting anything
else. A pull request that edits a workflow file is never reviewed: the action
refuses a workflow that differs from `main`'s, and GitHub shows that skip as a
pass.
