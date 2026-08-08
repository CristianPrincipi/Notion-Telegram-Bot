# Plan: separate business logic from the Telegram transport layer

_Last updated: 2026-08-08_

Branch: `layering`, off `main` at `56ae2bf`.
Baseline before any change: **727 passed**, `ruff check .` clean.

**This is a pure refactor. Zero behaviour change.** Anything that looks like a bug
gets written down under `## Noticed, not fixed` and shipped in a separate PR.

## Target structure

```
bot/        thin Telegram adapters — parse input, call a service, format the reply
services/   business logic — pure, testable, NO telegram imports
clients/    notion, calendar, anthropic, telegram file download
config.py   constants + validation (stays where it is)
david.py    stays at the repo root — Procfile runs `python david.py`
```

## The decisive rule

> Nothing under `services/` may import `telegram`, and no service function may take
> `update` as a parameter.

Enforced mechanically by `tests/test_layering.py` (added in Stage 1, before there is
anything under `services/` to break it) — an ast walk over every file in `services/`
that fails on `import telegram`, `from telegram…`, any parameter named `update`, and
any attribute chain reaching `message.reply_text`. It carries its own
can-this-guard-actually-fail test, the way `test_telegram_text` and
`test_data_integrity` already do — a guard that cannot go red is not a guard.

## Decisions taken before writing code

**D1 — the clients keep their filenames.** `git mv notion_client.py
clients/notion_client.py`, not `clients/notion.py`. A rename inside a move makes the
diff a delete-plus-add for the reader even when git records it, and `notion_client`
also happens to be a real PyPI package name — moving it into a package removes a
shadowing hazard rather than creating a naming question.

**D2 — the notify contract carries two channels, not one.** The stated signature is
`notify: Callable[[str], Awaitable[None]]`. The services being extracted send BOTH
plain messages (`update.message.reply_text`) and Markdown ones
(`telegram_text.reply`, which escapes at the interpolation site and retries plain on
`BadRequest`). Collapsing those into one callback changes behaviour in one direction
or the other, so services take:

```python
async def run_x(..., *, notify, notify_md=None)   # notify_md defaults to notify
```

`notify` is the plain channel and the only required one — a test passes `list.append`,
a job passes `logger.info`, and everything arrives as text. `notify_md` is what the bot
layer binds to `telegram_text.reply` so today's Markdown messages stay Markdown. See
the question raised for you below.

**D3 — services receive `user_data`, not `context`.** The expense state machine reads
`context.user_data` and nothing else off the PTB context. `expense_safety`'s functions
change parameter `context` → `user_data` (mechanical; it already imports no telegram),
and the bot layer passes `context.user_data`. `context.application.create_task`
(`run_detached`) stays in the bot layer, where it belongs.

**D4 — `handle_message` stays in `david.py`.** It IS the dispatch loop, and the
registry is staying per the brief. The `_cmd_*` bodies move to `bot/`.

## Milestone 1: Stage 1 — directory structure + clients

- [x] `clients/__init__.py`, `services/__init__.py`, `bot/__init__.py`
- [x] `git mv` notion_client.py / calendar_client.py / anthropic_client.py into `clients/`
- [x] Update every import site (david, budget, month, learn, implement, implement_diet,
      pkm, reminder, notion_ids, expense_safety, proactive/*) and the test imports —
      24 lines, no other change
- [x] `tests/test_layering.py` — the mechanical guard: `import telegram`, a parameter
      named `update`, a direct `reply_text`/`send_message`, and the layer direction.
      Mutation-checked by dropping a real offender into `services/` and watching
      `test_no_service_touches_telegram` name all three offences, then go green again.
      Carries its own offender test (6 rows) and a positive control against david.py.
- [x] Verify the entry point: `python david.py` reaches "🤖 David online!" under a fake
      environment, so the Procfile is unchanged and correct
- [x] `ruff check .` + `pytest` green (739 passed), commit

## Milestone 2: Stage 2 — services/expenses.py + services/books.py

- [ ] `services/expenses.py` — `add_Expenses`, `find_expense_matches`, `update_Expense`,
      `delete_Expense`, and the async find-choose-write cycle converted to `notify`
- [ ] `services/books.py` — `add_New_Book`, `find_Book_Page`, `add_Quote`, `chunk_text`,
      `extract_quote_from_pdf`, and the quote-from-PDF flow
- [ ] `clients/telegram_files.py` — `validate_pdf_attachment` + `download_pdf_attachment`
      (the one place that stays Telegram-aware, by definition — it downloads from Telegram)
- [ ] `expense_safety`: `context` → `user_data` (D3)
- [ ] `david.py` handlers become thin wrappers that build `notify` and call the service
- [ ] Preserved verbatim: the lookup INSIDE the expense lock, `sorts=CREATED_DESC`, the
      month-scoped refusal, more-than-one-match-writes-nothing, the undo snapshot taken
      from the LOOKUP's page object, `PageBusy` → `BUSY_EXPENSE_MESSAGE`
- [ ] green, commit, show the diff

## Milestone 3: Stage 3 — learn / implement / implement_diet become services

- [ ] `git mv learn.py services/learn.py`; every `update.message.reply_text` → `notify`,
      every `reply(update, …)` → `notify_md`
- [ ] `git mv implement.py services/implement.py`, same conversion
- [ ] `git mv implement_diet.py services/implement_diet.py`, same conversion
- [ ] `bot/learn.py`, `bot/implement.py` — the `update`-taking wrappers
- [ ] Preserved verbatim: append-then-delete ordering, the page locks and their refusal
      messages, `asyncio.wait_for` on reads only, the section-routing split
- [ ] green, commit, show the diff

## Milestone 4: Stage 4 — reduce david.py

- [ ] `bot/` gains the command handlers, `handle_document`'s caption router, `run_detached`
- [ ] `david.py` keeps: `__main__` bootstrap, `COMMANDS` + `handle_message`, `build_help`,
      `register_jobs`, `register_handlers`, the owner filter, `on_error` / `notify_error`
- [ ] `concurrent_updates` still absent (`test_async_io` inspects david's source)
- [ ] green, commit, show the diff

## Milestone 5: documentation

- [ ] CLAUDE.md module map rewritten for the new structure, with the decisive rule and
      the module that enforces it
- [ ] README updated where it names a moved file
- [ ] `## Noticed, not fixed` written up

## Test edits this refactor forces

"Every existing test must pass unmodified except for import paths" holds for most of
the suite. Four places need more than an import line, and none of them is an assertion
change — each is the test's ADDRESS for something that moved:

1. **`monkeypatch` / spy targets.** `tests/test_router.py::SPY_TARGETS` patches
   `david.add_Expenses` etc., and works today only because david's handlers resolve
   those names through david's own namespace. Once a handler lives in `bot/expenses.py`
   the patch has to name `services.expenses.add_Expenses`. Same for `test_async_io`'s
   `stub_module(monkeypatch, david, …)`.
2. **Source-scanning globs.** `test_telegram_text`, `test_data_integrity` (twice) and
   `test_concurrency` scan `root.glob("*.py") + (root/"proactive").glob("*.py")`. Moved
   files silently drop OUT of those scans — the guards would keep passing while covering
   less. They get widened to the new packages, which strengthens them.
3. **`test_concurrency.LOCKING_MODULES`.** A hardcoded list of filenames that the test
   below it asserts is exhaustive. The filenames change.
4. **Handlers called directly.** `test_async_io` calls `learn.handle_learn(update, text)`.
   After Stage 3 that call goes to the bot wrapper, which passes
   `update.message.reply_text` as `notify` — so every `replied_with(...)` assertion in
   those tests stays true, unedited.

## Answered before Stage 1 started

- **Q1 — how far does `services/` go?** → **Exactly the four stages.** `month.py`,
  `budget.py`, `pkm.py`, `reminder.py`, `notion_ids.py`, `expense_safety.py`,
  `telegram_text.py`, `page_lock.py`, `observability.py` and `proactive/` stay at the
  repo root. Four of them (`month`, `pkm`, `reminder`, `notion_ids`) still take
  `update` and reply directly; that is follow-up work, recorded under
  `## Noticed, not fixed`, not silently left out.
- **Q2 — the notify contract.** → **D2 as written**: `notify` plain and required,
  `notify_md` optional and defaulting to it.
- **Q3 — the test edits.** → **Mechanical only.** Addresses and glob roots may move;
  an assertion or an expected value may not. A test whose expectations would have to
  change is reported, not edited.

## Noticed, not fixed

_(filled in as I go; nothing here is touched in this PR)_

## Changelog

- 2026-08-08 — plan created.
