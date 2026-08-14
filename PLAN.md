# Plan: M5 — split the last four `update`-taking modules

_Last updated: 2026-08-14_

Branch: `split-update-modules`, off `main` at the M4 merge (`f0209cc`).
Baseline before any change: **962 passed**, `ruff check .` clean.

`reminder.py`, `pkm.py`, `notion_ids.py` and `month.py` are still at the repo
root and still take `update`, replying through `telegram_text` themselves.
`CLAUDE.md` names them as deliberate follow-ups from the layering split, and
`bot/commands.py` exists only to hold their one-line adapters until this
happens. Splitting them makes them callable from a job, drivable from a test
with no fake `Update`, and — the part that lasts — puts them under
`tests/test_layering.py`, which today cannot see them at all.

**This is a pure refactor. Move code only.** Every defect noticed on the way is
recorded under `## Found while moving` and fixed in a later PR, because a fix
hidden inside a move is a fix nobody reviewed.

## Decisions taken before writing code

**1. One COMMIT per module, one PR for the milestone.** The roadmap asks for one
PR per module; `ROADMAP.md`'s own workflow section asks for one PR per
milestone. The two split the difference badly, so: one branch, four commits,
each of which leaves `ruff check .` clean and the full suite green on its own.
A reviewer reads them one at a time; CI gates them once.

**2. The seam is the existing `handle_*` adapter, and it keeps its name and its
signature.** `handle_remind(update, user_text)` moves from `reminder.py` to
`bot/reminder.py` and becomes two lines: bind `for_update(update)`, call
`services.reminder.run_remind(user_text, notify=…, notify_md=…)`. That is what
`bot/learn.py` already does, and it means `tests/test_router.py` needs only a
`SPY_HOMES` change — the spy names and their recorded argument names do not
move, so the router table keeps asserting the same thing about the same call.

**3. One `bot/` module per feature, and `bot/commands.py` goes away.** Its
docstring is entirely about a state of affairs this milestone ends. Its four
delegators become `bot/reminder.py`, `bot/pkm.py`, `bot/notion_ids.py` and
`bot/month.py`, matching `bot/books.py` / `bot/expenses.py` / `bot/learn.py`;
`cmd_budget` — the one handler in there that already reads right, because
`budget.py` is telegram-free — becomes `bot/budget.py`.

**4. `_send_long` becomes one implementation, and it is bound where `notify`
is.** `pkm.py:221` splits plain text; `notion_ids.py:271` splits Markdown. Same
splitter, two senders. It cannot move into `services/` (it is a Telegram limit)
and the service cannot import `bot/`, so the adapter wraps the channel that
needs it:

    notify, notify_md = for_update(update)
    await run_get(text, notify=partial(send_long, notify), notify_md=notify_md)

The service just calls `notify(long_text)`. Which channel splits stays exactly
where it is today — plain for `Get`, Markdown for the diagnostics — so no
message changes shape, and there is one splitter instead of two.

**5. `services/month.py` keeps its `threading.RLock` verbatim.** It is reached
from worker threads, and an `asyncio.Lock` between two threads acquires without
ever blocking. The comment saying so moves with it, unedited.

**6. `REMIND_PATTERN` stays with the service; what a token MEANS stays in
`clients/calendar_client.py`.** Not collapsing that split is the point of the
module: a shorthand resolved in the regex is a date rule nothing can unit-test.

## Milestone 1: `reminder.py`

- [x] `services/reminder.py` — `run_remind(user_text, *, notify, notify_md=None)`,
      carrying `REMIND_PATTERN`, `_format_conflict_warning` and the
      `page_lock(CALENDAR_ID)` acquisition unchanged.
- [x] `bot/reminder.py` — `cmd_remind` + `handle_remind(update, user_text)`.
- [x] `david.py` imports `cmd_remind` from `bot.reminder`.
- [x] Delete `reminder.py`.
- [x] `tests/test_reminder_dates.py`, `tests/test_async_io.py`,
      `tests/test_concurrency.py` — retarget imports; no assertion changes.
- [x] `tests/test_concurrency.py` `LOCKING_MODULES` — `services/reminder.py`.
      `test_every_locking_module_is_actually_checked` fails until it is updated.
- [x] `tests/test_router.py` `SPY_HOMES["handle_remind"] = bot.reminder`.
- [x] A test driving `run_remind` with a list's `append` and no Update at all.
- [x] Suite green, `ruff check .` clean.

## Milestone 2: `pkm.py`

- [x] `bot/long_messages.py` — `send_long(send, text)`, the one splitter.
- [x] `services/pkm.py` — `run_get(user_text, *, notify, notify_md=None)`;
      `_send_long` deleted from it.
- [x] `bot/pkm.py` — `cmd_get` + `handle_get`, binding `notify` through
      `partial(send_long, notify)`.
- [x] `david.py`, `tests/test_pkm.py`, `tests/test_async_io.py`,
      `tests/test_router.py` retargeted.
- [x] A test driving `run_get` with a list's `append` and no Update.
- [x] Suite green, `ruff check .` clean.

## Milestone 3: `notion_ids.py`

- [x] `services/notion_ids.py` — `run_diag`, `run_find(query)`, `run_dbs`, plus
      the already-pure `search_all` / `list_db_pages` / `build_diagnostic_report`
      and the `__main__` block.
- [x] `bot/notion_ids.py` — the three adapters, binding `notify_md` through
      `send_long` (the Markdown channel is the one that splits here).
- [x] `ruff.toml` per-file-ignore repointed to `services/notion_ids.py`.
- [x] `david.py`, `tests/test_router.py` retargeted.
- [x] A test driving `run_dbs` / `run_find` with a list's `append` and no Update.
- [x] Suite green, `ruff check .` clean.

## Milestone 4: `month.py`

- [ ] `services/month.py` — everything, `run_month(*, notify, notify_md=None)`
      replacing `handle_month`. `threading.RLock` and its comment move unchanged.
- [ ] `bot/month.py` — `cmd_month` + `handle_month`.
- [ ] Importers updated: `budget.py`, `services/expenses.py`,
      `proactive/heartbeat.py`, `proactive/month_rollover.py`,
      `services/notion_ids.py`.
- [ ] `bot/budget.py` created, `bot/commands.py` deleted, `david.py` retargeted.
- [ ] `tests/test_month.py`, `tests/test_async_io.py`, `tests/test_concurrency.py`,
      `tests/test_router.py`, `tests/test_config_validate.py` retargeted.
- [ ] A test driving `run_month` with a list's `append` and no Update.
- [ ] Suite green, `ruff check .` clean.

## Milestone 5: the guards

- [ ] `tests/test_layering.py` now covers four more modules — run it after each
      one, not once at the end.
- [ ] **Spy-retarget verification:** break each moved function deliberately and
      watch its router row go red. A stub left on the old module keeps the test
      green against nothing, which is the failure mode `SPY_HOMES` exists for.
- [ ] `tests/test_concurrency.py`'s lock-key scan still finds the `CALENDAR_ID`
      lock after the move.

## Milestone 6: docs

- [ ] `CLAUDE.md` — module map rows, the layer paragraph, and strike the "five
      modules never got the treatment" entry.
- [ ] `README.md` — the Layout section.
- [ ] `ROADMAP.md` — tick M5.

## Milestone 7: ship

- [ ] Commit per module, push, PR, CI green, merge.

## Found while moving

Recorded rather than fixed — see decision 0 above. Add to `ROADMAP.md`'s backlog
when this lands.

- **`services/reminder.py` discards the `find_conflicts` error into `_`**, so a
  calendar read failure is indistinguishable from "the slot is clear". Already
  named in `CLAUDE.md`'s open questions; the move does not change it, and the
  entry's file path needs updating there.
- **`bot/long_messages.py`'s splitter can cut between a `*` and its closing
  one**, leaving an unbalanced entity in each half. `notion_ids.py` carried that
  as a KNOWN LIMITATION comment; it moves with the code, unfixed.
- **`services/notion_ids.py` reads `NOTION_KEY` from the environment itself**
  even though `clients/notion_client.py` owns the header. It is used only as a
  "is anything configured at all" probe in the diagnostic. Left as found.

## Open questions

- **`_send_long`'s two copies differed by one character** — pkm's `.rstrip()`
  against notion_ids' `.rstrip("\n")`. The merged one keeps `.rstrip("\n")`, so
  a chunk ending in trailing spaces keeps them. Nothing asserts either, and
  Telegram renders both identically; recorded so the choice is visible rather
  than silent.

## Changelog

- Nothing yet.
