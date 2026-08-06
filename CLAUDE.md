# CLAUDE.md

## What this is

**David** — a single-user personal Telegram bot that writes to Notion, reads Google
Calendar, and summarises content with the Anthropic API. Runs as a polling worker on Railway
(`Procfile: worker: python david.py`). Single-user is a design constraint: every command
spends the owner's Notion/Anthropic quota, so `OWNER_ID` gates everything through a PTB
filter — an unauthorized update is dropped by the dispatcher and never reaches handler code.

## Module map

| File | Owns | Must NOT own |
| --- | --- | --- |
| `david.py` | Entry point (`__main__`), the `handle_message`/`handle_document` regex routers, owner filter, job registration, expense + book + quote writes, PDF download | Feature logic, budget maths, month resolution, raw Notion HTTP |
| `config.py` | Constants, schedule times, timeouts, shortcut maps, weekday constants, the env contract (`REQUIRED_ENV`/`OPTIONAL_ENV`) and `validate()` | Reading feature IDs — each module reads its own `os.environ` |
| `notion_client.py` | The **only** place that speaks HTTP to Notion: headers, per-thread `Session`, retry/backoff, pagination, block builders | Any feature logic |
| `anthropic_client.py` | The **only** place that speaks to Anthropic: `complete_json`, retry, `stop_reason` checks, token logging, the daily spend guard | Prompts — each feature owns its own system prompt and schema |
| `calendar_client.py` | The **only** place that speaks to Google Calendar; per-thread service. `now_local()` is the project clock — never `datetime.now()` | Telegram, Notion |
| `page_lock.py` | Per-database asyncio locks (`page_lock`, `PageBusy`) | Anything else |
| `telegram_text.py` | `escape_md`, and the **only** safe senders (`reply`, `send`) — the sole place `parse_mode` reaches Telegram | Feature logic, message wording |
| `observability.py` | `setup_logging`, the correlation-ID contextvar, the heartbeat counters | Telegram, Notion, any probe |
| `expense_safety.py` | The guards on `U e` / `D e`: the pending-choice state machine in `context.user_data`, its 2-minute expiry, the undo record, and every message either prints | Notion calls, Telegram sends — it decides and formats, `david.py` acts |
| `month.py` | Which page this month's expenses relate to: naming, find-or-create, cache, `Month` handler | Expense writes, budget maths |
| `budget.py` | Expense aggregation + recap text (`compute_budget`, `format_budget`, `budget`) | Notion HTTP, Telegram |
| `learn.py` | `Learn [type] [source]` — extract, Claude-summarise, write to Notion | Manual merging |
| `implement.py` | `Implement [Page] - [Area]` — index a Manual by heading, route, merge and rewrite **only** the affected sections. Owns `get_area_db_id` | Diet (delegates to `implement_diet`) |
| `implement_diet.py` | The Diet page's H1>H2>H3 toggle tree: skeleton, breadth-first read, surgical updates | Generic Manual merging |
| `pkm.py` | `Get [Topic] - [Area]` — read a section back out of a Manual: index, fuzzy resolve, discovery. Read-only, no Claude call | Writing anything; knowing how Manuals are built |
| `reminder.py` | `Remind …` — parse, conflict-check, create the calendar event | Calendar HTTP (that is `calendar_client`) |
| `notion_ids.py` | `Diag` / `Find` / `DBs` — read-only ID + schema diagnostics | Any write |
| `proactive/` | Scheduled push messages. One builder module per feature; `scheduler.py` does all JobQueue wiring and sending. Never imports `david.py` | Sending from a builder — builders return `(text, error)` |
| `proactive/heartbeat.py` | `build_heartbeat` — the weekly liveness proof; runs the Calendar/Notion/month probes | Sending (that is `scheduler.py`) |

New features get a module. `david.py` routes to them; it does not absorb them.

## Conventions

**Fallible functions return `(value, error)`, never a bare `None` and never a raised
exception across a module boundary.** A lone `None` conflates "this failed" with "there is
nothing here", and the two need opposite handling. That is not hypothetical: `read_diet_tree`
used to discard errors below the top level (`h2_blocks, _ = get_children(...)`), so a
transient read failure made a section look **empty** — which is exactly what makes Claude
decide to populate it, and `apply_updates` then replaced real content with content merged
against nothing. Nothing errored. See `implement_diet._children_of_many`.

**An error is never the same value as an empty result.** The corollary of the rule
above, and the one that cost the most. `proactive/briefing.py` used to write
`if err or not events: return None`, so a revoked calendar share and a free evening
produced identical output — the reminders stopped and nothing said so. The morning
half was worse: on an error it set `events = []`, and an empty list renders as
"nothing scheduled", so during an outage David stated the day was clear. A missing
message you eventually notice; a confident wrong answer you act on. Every proactive
builder now returns `(text, error)`, and `scheduler._run_job` is the single place
those three states — send / stay silent / report — are told apart.

**Everything sent with Markdown goes through `telegram_text`.** Legacy Markdown has
no literal asterisk, so one stray `*` in a Notion category, a Claude-written
heading, or a slice of an uploaded PDF made Telegram reject the whole message —
after the Notion write had already succeeded, so it looked like David ignoring you.
`escape_md` at the interpolation site (a sender cannot tell David's own `*bold*`
from data), plus a retry without `parse_mode` on `BadRequest` for whatever escape
gets forgotten. Enforced by an ast-based test.

**The three error reporters are exempt from that, deliberately.**
`david.notify_error`, `david.on_error` and `proactive.scheduler._report_error` send
plain text unconditionally. They interpolate an exception into a `code span`, where
Markdown v1 ignores backslash escapes — escaping cannot save them, so the reporters
used to fail on exactly the ugly Notion errors they existed to report. Do not
"fix" them back into Markdown; each carries a comment saying so.

**Notion database IDs are `{AREA}_ID` in the environment.** `get_area_db_id` derives the
name (`"Brain"` → `BRAIN_ID`), so adding an area means adding an env var, not code.

**All secrets come from environment variables.** `config.validate()` runs first in
`__main__` and raises `SystemExit` listing *every* problem at once — a misconfigured deploy
costs one fix, not one redeploy per variable. Add a var to `REQUIRED_ENV`/`OPTIONAL_ENV`
and the README table when you introduce one.

**One model name, one client.** `config.ANTHROPIC_MODEL` is the only place the model
is named, and `anthropic_client.complete_json` is the only way to reach the API. A
feature module owns its system prompt and its JSON Schema; it does not own retry,
truncation handling, or token accounting.

**Async discipline.** Every synchronous call — `requests`, Notion, Anthropic, PyPDF2,
Google Calendar — runs via `asyncio.to_thread`. PTB runs updates on one event loop, so a
blocking call in an `async def` freezes the whole bot: a 300-second Anthropic read took
David down for five minutes. Keep those functions **synchronous** so they stay directly
testable; the handler does the offloading. Long *reads* also get an `asyncio.wait_for` cap
from `config.py` — **writes do not**, because `wait_for` cancels the awaiting coroutine but
cannot cancel the worker thread, so timing out a write reports failure while it is still in
flight. Notion writes are bounded by `notion_request`'s timeout and bounded retries instead.

## Hard rules

### 1. Notion is the single source of truth

No local mirror, no local database, no cached copy treated as authoritative. Railway's
filesystem is ephemeral — anything written locally dies with the container. Caches for
performance are fine *if deleting them is harmless*: `month.py` caches the resolved page in
`.month_state.json`, and losing it just means asking Notion again.

### 2. Never delete before the replacement is committed

Notion has no transactions, so **ordering is the only safety mechanism available**. The
pattern, in this order, every time:

1. snapshot the existing block IDs
2. append the new content
3. on error, **return early** — nothing has been deleted, the old content is intact
4. only on success, delete the snapshotted IDs — from the *snapshot*, never from a re-read
   after appending, which would delete the new content too

Reference implementations: `implement.apply_section_updates` (Manual sections) and
`implement_diet.apply_updates` (Diet sections). Clear-then-append meant a 502 or a Railway
restart between the two left the page **permanently empty**, with no transaction to roll
back and no second copy anywhere. The page briefly showing old-then-new content is the
accepted cost of never showing neither. Locked down by `tests/test_safe_writes.py`.

### 3. Never send a section the source did not touch

Everything sent to the model can come back reworded. `handle_implement` used to send
the **whole** Manual and rebuild the page from the reply, so untouched sections drifted
a paraphrase at a time, and a Manual over 40k characters had its tail silently dropped
from the prompt (`manual_text[:40000]`) and therefore from the rebuilt page.

So both Implement paths are sectioned: index by heading, a cheap routing call over
section NAMES, a merge over only the affected sections, and a write back to only those.
**The only guarantee that a section is unchanged is that it was never sent.** Locked
down by `tests/test_implement_sections.py`.

### 4. A destructive command never guesses which row it meant

`U e` and `D e` are find-then-mutate over a `contains` filter, and they used to act
on `results[0]`. **Notion documents no ordering for query results**, so with two
Coffees on the page the row that was archived was arbitrary — and the reply said
"deleted successfully" either way. No exception, a 200 from Notion, a confident
confirmation, and a missing row discoverable only by opening Notion.

Three independent guards, and they are independent on purpose — each one alone
still leaves a way to hit the wrong row:

1. **`sorts=CREATED_DESC` on every lookup that reads `results[0]`**
   (`notion_client.CREATED_DESC`). This does not make "first" *correct*, it makes
   it *defined* — the same row on two identical calls. Applies to
   `find_expense_matches`, `find_Book_Page` and `search_page_in_db`.
2. **Expense lookups are scoped to the current month.** If the month cannot be
   resolved the lookup is REFUSED, never widened: falling back to an unscoped
   search restores exactly the reach the filter removes, at the moment David is
   least sure of its own state.
3. **More than one match writes nothing** and asks, via `expense_safety`. The
   pending list lives in `context.user_data` and expires after 2 minutes, so a
   stray `2` cannot answer a prompt from an hour ago.

Then **every destructive write records its own reversal** before reporting success,
and `undo` applies it. The update snapshot must come from the page object the
LOOKUP returned — re-reading after the PATCH records the new amount as the old one,
producing an undo that changes nothing and says it worked.

The lookup runs **inside** the expense lock, not just the write. Splitting
find-from-mutate into two functions made it possible to lock only the second half,
which reads as safe — no two writes overlap — while leaving both queries free to
resolve to the same row. Locked down by `tests/test_expense_safety.py` and
`tests/test_concurrency.py::test_no_two_expense_cycles_are_ever_in_flight_together`.

### 5. Concurrency is deliberately narrow

`concurrent_updates` is **off**. Do not enable it; `tests/test_async_io.py` fails if you do.
Responsiveness is bought per-command instead: only the long commands (`Learn`, `Implement`,
both PDF uploads) go through `david.run_detached`. Everything else runs inline, strictly
ordered — which is what makes `Add e Carrefour 5` followed by `B` always report the new
total. **Locks do not give that back**: they stop two cycles interleaving, they do not decide
which runs first. That is why the decision is per-command and not a global switch.

Before widening concurrency, every find-then-mutate cycle must be locked *and verified by a
test that drives two real handlers concurrently* (`tests/test_concurrency.py`). Current
locks — always keyed on a **database** ID, never a page ID, because a page ID is not known
until the lookup the lock has to cover:

| Cycle | Key | On contention |
| --- | --- | --- |
| Implement → area Manual | `area_db_id` | refused |
| Implement → Diet page | `DIET_ID` | refused |
| `U e` / `D e` (lookup **and** write) | `EXPENSES_ID` | queues |
| A number answering an ambiguous `U e` / `D e` | `EXPENSES_ID` | queues |
| `Remind` | `CALENDAR_ID` | queues |

The ambiguous path releases the lock while it waits for your number — holding it
across a reply would stall every expense write for as long as you took to answer,
and the selection writes by page ID, so it has no lookup left to protect.

`month.py` uses a `threading.RLock`, not `page_lock`: its cycle is reached from worker
threads, and an `asyncio.Lock` between two threads acquires without ever blocking.

## Testing

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

Fully offline: `tests/conftest.py` installs a fake environment at import time (before any
project module loads — several read `os.environ` at module scope) and `responses` intercepts
HTTP. Nothing reaches Notion, Telegram or Google.

`tests/test_router.py` is the pre-deploy gate: a table of `input → handler → parsed args`
driving the **real** `handle_message`. Which command wins depends on the order of the regex
checks and on `fullmatch` vs `match` — invisible when reading one branch, and exactly what
breaks when a command is added in the wrong place. Adding a command means adding a
`SPY_TARGETS` entry plus rows. Rows marked `known_bug` assert current *wrong* behaviour on
purpose; fixing one turns its row red, and the row is updated in the same commit.

**Every bug fix ships with a test that fails before it and passes after**, asserting against
the shipping code path — a test that rebuilds the logic only proves it agrees with itself.

## Deployment

Pushing to `main` triggers a Railway deploy. **Railway keeps the previous version running
when a deploy fails, so a broken deploy is silent** — you will not notice until a command
misbehaves. CI on the PR (`ruff check .` + `pytest`) is the real gate. Work on a branch, open
a PR, let CI go green, then merge. Never commit directly to `main`.

## Open questions

Found in the code, not resolved here — do not "fix" these by guessing intent:

- **Unreferenced code:** `implement.clear_page_blocks` (only the `_by_id` variant is
  called), `DATABASE_ID` in `david.py`. (`reminder.build_today_message` /
  `build_tomorrow_message` were on this list and have been deleted — they had zero
  callers and carried a copy of the error/empty collapse that made them look like
  the bug's home. The live copy was in `briefing.py`.)
- **The Learn-nudge job does not exist.** Both Implement paths tick an `Implemented`
  checkbox described as feeding it; `proactive/__init__.py` lists it as Step 6, with Step 5
  (takeaway of the week) and Step 7 (tasks). The checkbox is written and never read.
- ~~**`MONTH_ID` is in `REQUIRED_ENV` but `month.py` treats it as an optional seed.**~~
  Resolved: it is in `OPTIONAL_ENV` now, which is what the module reading it always
  meant. Unset, the first run resolves the month from Notion by title.
- **`(value, error)` is still not universal**, but the remaining gaps are narrower and
  named:
  - `budget.compute_budget()` returns `dict | None`, collapsing a Notion failure into
    "nothing to report" for `briefing._budget_line` and `budget_watch._should_warn`.
    This is the same class of bug the briefings had, one layer down. Asserted as a
    known gap in `tests/test_briefings.py`.
  - `calendar_client` returns `[], err` on failure — the failure value IS the
    legitimate empty value, which is the root cause every caller has to work around
    by checking `err` FIRST. Changing it ripples into `find_conflicts`,
    `reminder.handle_remind` and three test files.
  - `reminder.py:93` discards the `find_conflicts` error into `_`, so a calendar read
    failure is indistinguishable from "the slot is clear". Deliberate (a failed check
    degrades to no warning rather than blocking the reminder) but undocumented until
    now.
