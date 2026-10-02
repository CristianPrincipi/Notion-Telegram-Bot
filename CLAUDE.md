# CLAUDE.md

**David** — a single-user Telegram bot that writes to Notion, reads Google Calendar and
summarises with the Anthropic API; one polling worker on Railway (`Procfile`: `worker: python david.py`).
`OWNER_ID` gates every update through a PTB filter, so an unauthorized update never
reaches handler code.

This file is the rules. Almost every one is a bug that shipped; the stories are in
[docs/design-notes.md](docs/design-notes.md), under the same headings — read the note
before changing what a rule protects. The overview and the full module map are in
[docs/architecture.md](docs/architecture.md); user docs are in [docs/](docs/).

## Layers

- `bot/` → `services/` → `clients/`, never back. New features get a module; `david.py`
  routes to them and does not absorb them.
- Nothing under `services/` imports `telegram` or takes an `update` parameter
  (`tests/test_layering.py`).
- A service reports through `notify(text)` (plain, required) and `notify_md(text)`
  (Markdown, defaults to `notify`). `bot/notify.py` is the only place they are bound.

## One home each

| Thing | Lives only in |
| --- | --- |
| HTTP to Notion | `clients/notion_client.py` |
| Anthropic calls; the model name | `clients/anthropic_client.complete_json`; `config.ANTHROPIC_MODEL`. Each feature owns its prompt and schema, not retry or token accounting |
| Google Calendar | `clients/calendar_client.py` |
| The clock | `calendar_client.now_local()`. Never a clock read without a timezone (scanned) |
| Markdown to Telegram | `telegram_text` — `escape_md` at every interpolation site; `reply` / `send` are the only `parse_mode` senders |
| Notion column names, looked-up page titles | `config.py`'s NOTION SCHEMA, per database even where two agree (scanned) |
| Environment variables | `config.REQUIRED_ENV` / `OPTIONAL_ENV`; a default only through `config.env_or` (scanned) |
| Splitting a long reply | `bot/long_messages.py` |
| The pending list and the undo record | `pending_choice.py` — ONE of each across every destructive command |
| Cutting source text to a budget | `run_learn` (`SUMMARY_INPUT_CHARS`); `run_implement` / `run_implement_diet` via `fit_to_budget` |
| Sending scheduled messages | `proactive/scheduler.py`; builders return `(text, error)` and never send |
| The unverified-source marker | `config.UNVERIFIED_MARKER` + `is_unverified_source` |
| Rendering calendar events | `services/agenda.format_events_inline` |

## Conventions

- **Fallible functions return `(value, error)`** — never a bare `None`, never an exception across a module boundary.
- **An error is never an empty result.** Check `err` first; an error path never
  renders `[]`, which reads as "nothing scheduled".
- **Nor is an empty result an error.** `search_page_in_db` answers `(page, None)` /
  `(None, None)` / `(None, error)`; a create-if-missing caller refuses on an error and
  never reads it as missing.
- **The three error reporters send plain text on purpose** — `david.notify_error`,
  `david.on_error`, `proactive.scheduler._report_error`. Do not convert them to Markdown.
- **One budget, one number, one cut.** Extractors return text WHOLE; prompt builders
  never re-slice; the SECTION half of a merge prompt is never clipped (refuse instead);
  the reply says when source text was cut; the cut runs before `is_unverified_source`.
- **A partial write is a third outcome.** `append_children` returns `Written` (a list
  with `batches_done` / `batches_total`); callers print the tally and warn that a
  re-run duplicates. A test double returns `Written` too (`conftest.written_*`).
- **A recollection may add to a Manual, never rewrite it.** `Learn book` pages carry the
  marker as their FIRST block; `_hold_back_rewrites` refuses a section that loses or
  rewords any existing line, and a held-back section is its own outcome. Only that check
  is a guarantee — the prompt rule is a hint; never describe it as one.
- **`📚 Sources` is David's ledger.** Appended on every Implement; kept unwritable by
  two independent guards (`routable_sections`, `apply_section_updates`). Keep both.
- **Learn de-duplicates by URL, before the fetch.** Normalisation folds only what cannot
  change the bytes (query kept, sorted). A missing `Source URL` column skips the check
  and the write, out loud; a failed check is reported and the run continues.
- **Title columns are discovered for reads** (`title_property`: cached only on success,
  refused on failure, never guessed as `"Name"`), and named from `config` for writes.
- **An area is an environment variable:** `get_area_db_id` maps `Brain` → `BRAIN_ID`.
- **Article extraction is trafilatura → BS4** with `MIN_ARTICLE_CHARS`; `import
  trafilatura` stays unguarded; log which parser won. Re-run the manylinux dry-run in
  `requirements.txt` before bumping either pin.
- **The environment is validated first.** `config.validate()` lists every problem in one
  exit. A new variable gets `REQUIRED_ENV` / `OPTIONAL_ENV`, a row in
  `docs/configuration.md` and a line in `.env.example`. A blank variable is unset.
- **Optional features degrade in their own commands, out loud** — never by failing
  startup, never silently (e.g. the calendar refuses an unset `GOOGLE_CALENDAR_ID`).
- **Async:** every synchronous call (requests, Notion, Anthropic, PyPDF2, Google) runs
  via `asyncio.to_thread`, and those functions stay synchronous. Long reads get an
  `asyncio.wait_for` cap from `config`; writes never do.
- **`Remind` / `Agenda` dates** follow
  [.claude/rules/dates-and-times.md](.claude/rules/dates-and-times.md), loaded when you
  touch those files.

## Hard rules

1. **Notion is the single source of truth.** Nothing local is authoritative — Railway's
   disk dies with the container. A cache must be harmless to delete, preferably in
   memory; get the no-cache path right first.
2. **Never delete before the replacement is committed:** snapshot the block IDs → append
   → on error return → delete from the SNAPSHOT, never from a re-read
   (`apply_section_updates`, `apply_updates`; `tests/test_safe_writes.py`).
3. **Never send a section the source did not touch:** route on section names, merge
   only the affected sections, write only those (`tests/test_implement_sections.py`).
4. **A destructive command never guesses which row it meant.** Defined ordering
   (`CREATED_DESC`, `orderBy="startTime"`); a scope that is refused, never widened (this
   month; `CANCEL_SEARCH_DAYS`); more than one match writes nothing and asks; every write
   records its reversal — snapshotted from the object the LOOKUP returned — before
   reporting success. The lookup runs inside the lock. `Cancel`'s undo RE-CREATES (say
   "re-created", never "restored") and matches the event title only, never the API's `q`.
5. **Concurrency is deliberately narrow.** `concurrent_updates` stays off
   (`tests/test_async_io.py`); only `Learn`, `Implement` and the PDF uploads go through
   `bot.tasks.run_detached`. Locks key on a DATABASE id, never a page id. Before
   widening, every find-then-mutate cycle needs a lock and a test driving two real
   handlers concurrently (`tests/test_concurrency.py`). `services/month.py` uses a
   `threading.RLock`, because its cycle runs on worker threads.

| Cycle | Lock key | On contention |
| --- | --- | --- |
| Implement → area Manual | `area_db_id` | refused |
| Implement → Diet page | `DIET_ID` | refused |
| `U e` / `D e` (lookup **and** write), and the number answering them | `EXPENSES_ID` | queues |
| `Remind`; `Cancel` (lookup **and** delete), its number and its `undo` | `CALENDAR_ID` | queues |

## Testing

```bash
pip install -r requirements-dev.txt && ruff check . && pytest
```

- Offline: `tests/conftest.py` fakes the environment at import; `responses` intercepts HTTP.
- **Every bug fix ships with a test that fails before it and passes after**, against the
  shipping code path. Verify every guard by removing it and watching a named test go red.
- `tests/test_router.py` is the pre-deploy gate. A new command needs a `Command`, a
  `SPY_TARGETS` entry and rows; a `known_bug` row is updated in the commit that fixes it;
  a moved function moves its spy (`SPY_HOMES`); no input may match two commands.
- The help is generated from `david.COMMANDS` — never write it by hand.
- Source scans (layering, `parse_mode` senders, weekdays, lock keys, Notion names,
  environment reads, clock reads) walk the root and `bot/`, `clients/`, `services/`,
  `proactive/`. A new package joins all of them. Each scan has a can-it-fail test.
- A process-lifetime cache needs an autouse fixture clearing it (`_title_props`, `_db_schemas`).
- A fixture whose shape depends on the runner builds every shape by hand; bulk-text
  fixtures never repeat a paragraph (trafilatura drops duplicates).

## Deployment

A push to `main` deploys on Railway, and a failed deploy is silent — the previous
version keeps running. Branch → PR → CI green → merge; never commit to `main`. The PR
review action is pinned by SHA with an explicit model; if a review fails in seconds
with no tokens spent, replace `CLAUDE_CODE_OAUTH_TOKEN` before suspecting anything else.

## Documentation contract

| Change | Update in the same PR |
| --- | --- |
| A command added, changed or removed | the Commands table in `docs/features.md` (tested); examples there |
| An environment variable | `config.REQUIRED_ENV` / `OPTIONAL_ENV`, `docs/configuration.md`, `.env.example` |
| A Notion column, database or looked-up page | `config.py` NOTION SCHEMA, `docs/notion-schema.md`, and the Notion template (say so in the PR) |
| A scheduled job or its time | the scheduled-messages table in `docs/features.md` |
| A setup step or a new account/service | `docs/setup.md` |
| A new way for setup or a command to fail | `docs/troubleshooting.md` |
| A rule or convention | this file (the rule) and `docs/design-notes.md` (the why) |
| A module added or moved | `docs/architecture.md`'s module map; the "One home" table if it owns something |
| Anything a user would notice | `CHANGELOG.md`, under `[Unreleased]` |

## Open questions

Known and deliberately unresolved: [docs/design-notes.md#open-questions](docs/design-notes.md#open-questions).
Do not "fix" one by guessing intent. When a decision settles one, record it there.

## Implementation Plan Tracking

For any non-trivial task (multi-file changes, new features, refactors, migrations),
maintain a plan file at `PLAN.md` in the project root.

**`PLAN.md` is local-only.** It is in `.gitignore` (with `ROADMAP.md` and
`CLAUDE.local.md`) and is never committed — do not `git add -f` it. So nothing
committed may cite it: a code comment or doc that says "see PLAN.md" points a public
reader at a file they do not have. When a plan settles something the repo needs to
remember, write it where it lives — the code, `docs/design-notes.md`'s Open questions,
or `docs/`.

### Before writing any code
1. Create or update `PLAN.md` with the full list of steps required, grouped under
   milestones.
2. Each step is a checkbox: `- [ ] Step description`.
3. Show me the plan and wait for confirmation before starting.

### Format
~~~
# Plan: <task name>
_Last updated: YYYY-MM-DD_

## Milestone 1: <name>
- [x] Completed step
- [ ] Pending step — <one-line note on approach or blocker>

## Open questions
- ...
~~~

### Rules for keeping it current
- The moment a step is finished, mark it `[x]` before moving to the next one. Never
  batch updates at the end.
- If requirements change, rewrite the affected steps immediately and note what changed
  and why under a `## Changelog` section.
- If you discover a step that wasn't in the plan, add it rather than doing it silently.
- If a step turns out to be unnecessary, strike it through
  (`- [ ] ~~step~~ — dropped: reason`) instead of deleting it.
- Re-read `PLAN.md` at the start of every session so you resume from the correct state.
- Keep steps atomic: each one should be independently verifiable.
