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
| `calendar_client.py` | The **only** place that speaks to Google Calendar; per-thread service. `now_local()` is the project clock — never `datetime.now()` | Telegram, Notion |
| `page_lock.py` | Per-database asyncio locks (`page_lock`, `PageBusy`) | Anything else |
| `month.py` | Which page this month's expenses relate to: naming, find-or-create, cache, `Month` handler | Expense writes, budget maths |
| `budget.py` | Expense aggregation + recap text (`compute_budget`, `format_budget`, `budget`) | Notion HTTP, Telegram |
| `learn.py` | `Learn [type] [source]` — extract, Claude-summarise, write to Notion | Manual merging |
| `implement.py` | `Implement [Page] - [Area]` — merge a Learn page into an area Manual. Owns `get_area_db_id` | Diet (delegates to `implement_diet`) |
| `implement_diet.py` | The Diet page's H1>H2>H3 toggle tree: skeleton, breadth-first read, surgical updates | Generic Manual merging |
| `reminder.py` | `Remind …` — parse, conflict-check, create the calendar event | Calendar HTTP (that is `calendar_client`) |
| `notion_ids.py` | `Diag` / `Find` / `DBs` — read-only ID + schema diagnostics | Any write |
| `proactive/` | Scheduled push messages. One builder module per feature; `scheduler.py` does all JobQueue wiring and sending. Never imports `david.py` | Sending from a builder — builders return `str \| None` |

New features get a module. `david.py` routes to them; it does not absorb them.

## Conventions

**Fallible functions return `(value, error)`, never a bare `None` and never a raised
exception across a module boundary.** A lone `None` conflates "this failed" with "there is
nothing here", and the two need opposite handling. That is not hypothetical: `read_diet_tree`
used to discard errors below the top level (`h2_blocks, _ = get_children(...)`), so a
transient read failure made a section look **empty** — which is exactly what makes Claude
decide to populate it, and `apply_updates` then replaced real content with content merged
against nothing. Nothing errored. See `implement_diet._children_of_many`.

**Notion database IDs are `{AREA}_ID` in the environment.** `get_area_db_id` derives the
name (`"Brain"` → `BRAIN_ID`), so adding an area means adding an env var, not code.

**All secrets come from environment variables.** `config.validate()` runs first in
`__main__` and raises `SystemExit` listing *every* problem at once — a misconfigured deploy
costs one fix, not one redeploy per variable. Add a var to `REQUIRED_ENV`/`OPTIONAL_ENV`
and the README table when you introduce one.

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

Reference implementations: `implement.handle_implement` (the Manual) and
`implement_diet.apply_updates` (Diet sections). Clear-then-append meant a 502 or a Railway
restart between the two left the page **permanently empty**, with no transaction to roll
back and no second copy anywhere. The page briefly showing old-then-new content is the
accepted cost of never showing neither. Locked down by `tests/test_safe_writes.py`.

### 3. Concurrency is deliberately narrow

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
| `U e` / `D e` | `EXPENSES_ID` | queues |
| `Remind` | `CALENDAR_ID` | queues |

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

- **`pkm.py` is entirely unwired.** It implements `Get [Topic] - [Area]` (index build, fuzzy
  resolve, discovery mode) but nothing imports it: no route, no help entry, no test. Dead
  code, or pending wiring?
- **Other unreferenced code:** `reminder.build_today_message` / `build_tomorrow_message`
  (superseded by `proactive/briefing.py`), `implement.clear_page_blocks` (only the `_by_id`
  variant is called), `DATABASE_ID` in `david.py`.
- **The Learn-nudge job does not exist.** Both Implement paths tick an `Implemented`
  checkbox described as feeding it; `proactive/__init__.py` lists it as Step 6, with Step 5
  (takeaway of the week) and Step 7 (tasks). The checkbox is written and never read.
- **`MONTH_ID` is in `REQUIRED_ENV` but `month.py` treats it as an optional seed**, resolving
  the month from Notion without it. Required-by-contract, optional-in-practice.
- **`(value, error)` is not universal.** `briefing.py` and `reminder.py` still collapse error
  and empty into `None` (`if err or not events: return None`) — deliberate, since both mean
  "nothing to send", but it is the one place the rule above is knowingly not followed.
