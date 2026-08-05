# Notion-Telegram-Bot

**David** — a single-user personal Telegram bot that writes to Notion, reads Google
Calendar, and summarises content with the Anthropic API. Runs as a polling worker
on Railway (`worker: python david.py`).

## Access control

David is single-user by design: every command spends the owner's Notion and
Anthropic quota, and several write to or delete from Notion databases. Only the
Telegram account whose numeric ID matches `OWNER_ID` can use the bot.

Authorization is enforced with a python-telegram-bot filter, not a check inside
the handlers, so an unauthorized update is dropped by the dispatcher and never
reaches handler code — a newly added command cannot forget to check. Messages
from anyone else are logged at `WARNING` and answered with silence (a reply would
confirm to whoever probed the bot that it is live).

To find your ID, send `/start` to [@userinfobot](https://t.me/userinfobot).

## Environment variables

David validates all of these at startup (`config.validate()`). If any **required**
variable is missing or blank, the process exits immediately and lists *every*
problem at once, so a misconfigured deploy takes one fix rather than one redeploy
per variable. Missing **optional** variables only log a warning — the bot starts
fine without them and loses the feature named below.

| Variable | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | **Required** | Telegram bot token from [@BotFather](https://t.me/botfather). |
| `OWNER_ID` | **Required** | Numeric Telegram user ID allowed to use the bot. Everyone else is ignored. Must be numeric. |
| `CHAT_ID` | **Required** | Telegram chat that receives scheduled briefings and error reports. |
| `NOTION_KEY` | **Required** | Notion internal integration secret. |
| `EXPENSES_ID` | **Required** | Notion Expenses database ID. |
| `MONTH_ID` | **Required** | Notion page ID of the current month; expenses relate to it. **Seed value only** — David rolls it forward itself (see [Monthly rollover](#monthly-rollover)). |
| `LETTI_ID` | **Required** | Notion Books ("Letti") database ID. |
| `LITERATURE_ID` | **Required** | Notion page ID of the Literature area; books relate to it. |
| `LEARN_ID` | **Required** | Notion Learn database ID for videos, articles, podcasts and PDFs. |
| `ANTHROPIC_API_KEY` | **Required** | Anthropic API key used to summarise Learn and Implement content. |
| `SUPADATA_KEY` | Optional | Supadata API key for YouTube transcripts. Without it, `Learn video` fails. |
| `GOOGLE_CREDENTIALS_JSON` | Optional | Service-account JSON for Google Calendar. Without it, reminders fail. |
| `GOOGLE_CALENDAR_ID` | Optional | Target calendar. Defaults to `primary`. |
| `DIET_ID` | Optional | Notion Diet area database ID. Needed by `Implement [Page] - Diet`. |
| `BRAIN_ID` | Optional | Notion Brain area database ID. Needed by `Implement [Page] - Brain`. |
| `FINANCE_ID` | Optional | Notion Finance area database ID. Needed by `Implement [Page] - Finance`. |
| `BUDGET_CEILING` | Optional | Monthly budget ceiling in euros. Defaults to `300`. |
| `ANTHROPIC_DAILY_BUDGET_USD` | Optional | Estimated Anthropic spend allowed per day before `Learn` and `Implement` are refused. Defaults to `5`. |
| `ANTHROPIC_SPEND_FILE` | Optional | Where the running daily spend is recorded. Defaults to `.anthropic_spend.json`. |
| `MONTHS_DB_ID` | Optional | Notion database the month pages live in. Discovered from the Expenses `Account` relation when unset. |
| `MONTH_STATE_FILE` | Optional | Where the resolved month page ID is cached between restarts. Defaults to `.month_state.json`. |
| `DATABASE_ID` | Unused | Read in `david.py` but never referenced anywhere. Left in place; safe to drop. |

`Implement [Page] - [Area]` and `Get [Topic] - [Area]` both resolve their target
from `{AREA}_ID` (see `get_area_db_id` in `implement.py`), so adding a new area
means adding the matching environment variable.

## Commands

Send `h`, `help` or `aiuto` to the bot for the in-chat version.

| Command | What it does |
| --- | --- |
| `Add b [Name] - [Author] - [Genre]` | Add a book. Genres: `s` `h` `m` `p` `a` `ph` |
| `Add q [Book] - [Title] - [Quote]` | Add a quote to a book |
| `Add q [Book] - [Title] - [Begin] / [End]` | Extract a quote from an attached PDF (send as the file's caption) |
| `Add e [Name] [Amount] [Category]` | Add an expense. Categories: `s` `f` `g` `o` (default `f`) |
| `U e [Name] [Amount] [Category]` | Update an expense |
| `D e [Name]` | Delete (archive) an expense |
| `B` | Monthly budget recap |
| `Month` | Force the monthly page rollover now and report the page ID |
| `Remind [Name] [DD.MM] - [HH.MM]` | Create a Google Calendar event |
| `Learn video\|article\|podcast\|book\|pdf [source]` | Summarise into the Learn database |
| `Implement [Page] - [Area]` | Merge a Learn page into an Area manual |
| `Get [Topic] - [Area]` | Read a section back out of that Area's manual. `Get ? - [Area]` lists every topic |
| `Diag` / `Find [name]` / `DBs` | Notion ID diagnostics |

## Scheduled messages

All times Europe/Rome, configured in `config.py` and attached by
`david.register_jobs()`.

| Time | Job | Message |
| --- | --- | --- |
| 00:05 daily | `month_rollover` | The new month's expense page. Silent unless something moved |
| 07:30 daily | `morning_briefing` | Today's calendar events + a one-line budget pace |
| 13:00 daily | `budget_pacing` | Overspend projection — **only** when trending meaningfully over |
| 20:00 daily | `evening_briefing` | Tomorrow's events. Silent when tomorrow is empty |
| 09:30 Sunday | `budget_recap` | Full per-category budget recap |

The two briefings replaced the old `send_daily_reminders` job, which sent both
today's and tomorrow's events at 07:30. Running both would have sent today's
events twice each morning.

## Monthly rollover

Every expense relates to a month page through the Expenses `Account` column.
That page's ID used to be `MONTH_ID` in Railway, updated by hand on the 1st —
and forgetting did not fail loudly: expenses kept being written into *last*
month's page and `B` kept answering with last month's total.

`month.py` now answers "which page do this month's expenses belong to?" from
Notion instead. A month page is identified by its title, in one format —
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
| Notification | Only when something moved (created, renamed, adopted) or failed — a nightly "still August" would train you to ignore it |

The database the month pages live in is discovered from the Expenses `Account`
relation, so there is no second ID to keep correct; `MONTHS_DB_ID` overrides that
discovery. The resolved page ID is cached in `.month_state.json` so a mid-month
restart does not fall back to the seed `MONTH_ID` — the cache can be deleted at
any time, since Notion is the source of truth.

`MONTH_ID` is still read, as the **seed**: with no cache file it is taken to be
the current month's page, which is what it was when you set it. From the first
rollover on it can go stale without consequence. Each rollover message includes
the new page ID in backticks, so keeping the Railway variable current is one tap
if you want to.

### Input rules

Commands are matched with `re.fullmatch` against the stripped message, so a
command only runs when the whole message *is* that command — mentioning one
mid-sentence does nothing. Surrounding whitespace is ignored.

Amounts accept either decimal separator (`2,20` and `2.20` are the same) and
must be greater than zero. An omitted category falls back to `Food`; a supplied
but unrecognised one is reported as an error rather than silently defaulted.

Editing a sent message does **not** re-run the command — otherwise fixing a typo
would create a second Notion entry.

Expense dates use Europe/Rome, not the host clock, so an expense logged after
local midnight is not filed under the previous day.

### File uploads

PDFs are accepted on the `Learn pdf` and `Add q ... / ...` captions. Both paths
reject non-PDF MIME types and files over **15 MB**, and both cap the download at
30 s per request / 2 minutes overall — Telegram's own download helper has no
timeout and can hang a worker indefinitely.

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
`notion_client.notion_request`'s per-request timeout and its bounded retries.

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
(`david.run_detached`, built on `Application.create_task` so failures still reach
the error handler and in-flight work is awaited on shutdown). Everything else
runs inline.

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
| `U e` / `D e` | `EXPENSES_ID` | queues (writes take ~1s) |
| `Remind` | `CALENDAR_ID` | queues |

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
between two threads acquires without ever blocking. `month.py` serialises it with
a `threading.RLock` instead — same rule, right primitive.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The test suite runs fully offline: `tests/conftest.py` installs a fake
environment and every HTTP call is intercepted, so nothing reaches Notion,
Telegram or Google. CI (`.github/workflows/ci.yml`) runs the same two commands on
every push.

`tests/test_router.py` is the pre-deploy gate — a table of
`input → handler → parsed args` covering every command. Rows marked `known_bug`
assert current, wrong behaviour on purpose; fixing one of those bugs is expected
to turn its row red, and the row should be updated in the same commit.
