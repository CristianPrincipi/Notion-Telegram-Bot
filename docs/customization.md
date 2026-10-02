# Customization

Recipes for the changes people make most. Each says what to change, where, and
which tests will tell you if you missed something. After any change, run the
suite before you push — a push to `main` deploys:

```bash
ruff check .
pytest
```

Most settings are code constants in [`config.py`](../config.py), not environment
variables, so changing them means a commit on your fork. The few that are
variables are listed in [Configuration](configuration.md).

## Expense categories and book genres

- **Where:** `config.py` — `CATEGORY_MAP` (shortcut → Notion option) and
  `DEFAULT_CATEGORY` for expenses; `GENRE_MAP` for books.
- **Change:** edit the maps. Add the same options to the Notion `Category` /
  `Genre` column so the names match exactly.
- **Free:** the in-chat help is generated from the maps, so it updates itself.
- **Tests:** `tests/test_router.py`, `tests/test_budget.py`,
  `tests/test_expense_safety.py` and `tests/test_data_integrity.py` use today's
  names (`Shopping`, `Satira`, …) in their fixtures — update those rows.

## Timezone and schedule times

- **Where:**
  - `config.py` — `PROACTIVE_TIMEZONE`, and each job's `*_HOUR` / `*_MINUTE`
    (`MORNING_BRIEFING_HOUR`, `HEARTBEAT_DAY`, …).
  - `clients/calendar_client.py` — `TIMEZONE_NAME`, the clock every date in
    David is read on (`now_local()`).
  - `david.py` — `register_jobs`: the Sunday `budget_recap` time is written there
    (09:30), not in `config.py`.
- **Change:** a different timezone means changing **both** timezone constants
  together; a different time is one constant.
- **Tests:** `tests/test_agenda.py`, `tests/test_reminder_dates.py`,
  `tests/test_data_integrity.py` and `tests/test_router.py` pin Europe/Rome —
  including its summer-time dates — so a timezone change needs their fixtures
  moved too. Update the table in [Features](features.md#scheduled-messages).

## Adding an area

No code at all.

1. In Notion, create a database with a title column called `Name` (anywhere
   under the page your integration is connected to, or connect it directly).
2. Add `{AREA}_ID` = its ID: the area's name in capitals, spaces as
   underscores — `Sleep` → `SLEEP_ID`, `Side Projects` → `SIDE_PROJECTS_ID`.
3. Redeploy. `Implement [Page] - Sleep` creates the area's `Manual` on its first
   run, and `Get … - Sleep` reads it.

## Notion column names

- **Where:** the `NOTION SCHEMA` section of `config.py` — one constant per
  column, per database (`EXPENSE_AMOUNT_PROPERTY`, `BOOK_GENRE_PROPERTY`, …),
  plus the page titles `MANUAL_PAGE_TITLE` and `DIET_PAGE_TITLE`.
- **Change:** rename the column in Notion and the constant in `config.py`
  together; between the two, every write naming it is rejected.
- **Tests:** `tests/test_notion_schema.py` fails if a column name is written
  anywhere outside `config.py`, so a rename cannot be done halfway in code.

## Claude model and prompts

- **Model:** `config.ANTHROPIC_MODEL` is the only place it is named. If the new
  model is priced differently, update `ANTHROPIC_INPUT_COST_PER_MTOK` and
  `ANTHROPIC_OUTPUT_COST_PER_MTOK` next to it, or the daily budget guard will
  count wrong. Test: `tests/test_anthropic_client.py`.
- **Prompts:** each feature owns its own.

  | Prompt | Where |
  | --- | --- |
  | `Learn` summaries | `_SYSTEM` in `services/learn.py` |
  | `Implement` routing, merging, first build | `_ROUTE_SYSTEM`, `_MERGE_SYSTEM`, `_BUILD_SYSTEM` in `services/implement.py` |
  | `Implement … - Diet` | `_ROUTE_SYSTEM`, `_MERGE_SYSTEM` in `services/implement_diet.py` |

  Each prompt is paired with a JSON Schema the answer must match; change both
  together.

## Budget limits

| Setting | Where |
| --- | --- |
| Monthly budget | `BUDGET_CEILING` variable (default 300) |
| When the overspend warning starts | `config.BUDGET_PACING_MIN_DAY` (default: the 5th) |
| How far over before it warns | `config.BUDGET_PACING_THRESHOLD_PCT` (default 5%) |
| Anthropic spend per day | `ANTHROPIC_DAILY_BUDGET_USD` variable (default $5) |

## Learn types

- **Where:** `config.LEARN_TYPES` (name → emoji and database) and the extraction
  branch in `services/learn.py`'s `run_learn`, which fetches the text for each
  type.
- **Change:** add the entry and its branch. A type whose text is fetched like a
  web page can join the `("article", "podcast")` branch.
- **Free:** the help and `SUPPORTED_TYPES` follow `LEARN_TYPES`.
- **Tests:** `tests/test_router.py` checks the `Learn` row in
  [Features](features.md#commands) lists exactly the supported types — add the
  new one there. `tests/test_notion_schema.py` checks every type's database has
  known column names.

## Diet structure

- **Where:** `DIET_STRUCTURE` (the categories and the rows each starts with) and
  `EVIDENCE_FIELDS` in `services/implement_diet.py`.
- **Change:** edit them before the first `Implement … - Diet`: the skeleton is
  built from them once, when the `Diet` page is created. On an existing page,
  add the new heading in Notion as well.
- **Tests:** `tests/test_diet_tree.py`, `tests/test_diet_routing.py`.

## Adding a command

1. **The work** goes in a new module under `services/`. It must not import
   `telegram`; it reports through a `notify` callback.
2. **The adapter** goes under `bot/`: it reads the update, calls the service,
   and binds `notify` with `bot.notify.for_update`.
3. **Register it** in `david.COMMANDS` — pattern, handler, and a `Help` entry
   (the in-chat help is generated from it). Every pattern starts with its own
   literal word, and a test fails if two commands can match the same message.
4. **Tests:** `tests/test_router.py` needs a `SPY_TARGETS` entry and routing
   rows for the new command, and fails until it has them. Add a row to the
   [Features](features.md#commands) table too — a test checks every command is
   there.

If it changes or deletes something that already exists, mark it
`destructive=True` and give it the same three guards as `U e` and `Cancel`
([Architecture](architecture.md#destructive-commands)).

## Adding a scheduled job

1. **A builder** in a new `proactive/` module returns `(text, error)`: `text`
   to send, `None` to stay silent, or an error to report. It never sends.
2. **The schedule:** add the time to `config.py` and a `run_daily` call with a
   `name=` in `proactive/scheduler.py`'s `register_all`, wrapping the builder in
   `_run_job` — the one place that decides between sending, staying silent and
   reporting.
3. **Docs:** add it to the [scheduled messages](features.md#scheduled-messages)
   table.
4. **Tests:** see `tests/test_scheduler.py` for how a job is driven without a
   bot.
