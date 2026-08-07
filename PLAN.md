# Plan: router registry (F-32) + dead code sweep (F-34)
_Last updated: 2026-08-07_

Branch: `claude/router-cleanup-7s3raa`. Two commits, reviewable independently.
Baseline before any change: **620 passed**, `ruff check .` clean.

## Milestone 0: verification pass (done before writing code)

Every F-34 item checked against the code as it stands today:

| Item | State | Action |
| --- | --- | --- |
| `david.headers` dict | dead — defined at `david.py:91`, zero readers | remove (and `NOTION_KEY`, which exists only to build it) |
| `david.py` unused `json` import | **not present** — already gone | report only |
| `david.py` unused `logging` import | **in use** — `setup_logging()` + `getLogger("david")` | report only, keep |
| `learn.py` NOTION_BASE / notion_request / rich | **not imported** — already handled | report only |
| `implement_diet.py` NOTION_BASE / notion_request / `_paragraph` | **not imported** — already handled | report only |
| `implement._get_page_title_from_result` alias | live, one call site (`implement.py:631`) | inline `get_page_title`, drop alias |
| `implement.get_all_blocks` / `append_blocks_to_page` | live pass-throughs to `get_children` / `append_children` | drop wrappers, call the shared client directly, update 3 test files |
| `implement.build_manual_blocks(source_title)` | parameter genuinely unused in the body | drop the parameter |
| `learn.create_learn_page(metadata={})` | live mutable default | `metadata: dict | None = None` |
| `learn` newspaper branch | dead — `newspaper` is not in requirements.txt (removed there deliberately), so the ImportError is always swallowed | remove branch, BS4 becomes the only path |
| 3 block flatteners | `notion_client.blocks_to_text`, `implement_diet._content_to_text`, `_content_to_text_deep` | consolidate into `blocks_to_text(blocks, style=...)` |
| `update_Expense` / `delete_Expense` shared find logic | **already extracted** — both take a `page_id`; the lookup is `find_expense_matches`, split out by the expense-safety work | report only, no `find_expense` helper |
| `learn.TYPE_EMOJI` / `_get_db_id` maps | live in learn.py | move to config.py |

## Milestone 1: command registry (F-32) — commit 1

- [x] Add a `Command` dataclass to `david.py`: `name`, compiled `pattern`, `handler`, `help`, `destructive`.
- [x] Add a `Help` dataclass (label, usage lines, notes, inline vs block, group) plus
      `HelpGroup`, so the generated text keeps the current emoji + grouping style.
- [x] Extract each `if` branch of `handle_message` into a module-level
      `async def _cmd_x(update, context, args)`, body moved verbatim.
      Downstream calls stay module-global lookups so the existing test spies keep working.
- [x] Convert every pattern to NAMED groups (`?P<name>`, `?P<amount>`, `?P<category>`,
      `?P<author>`, `?P<genre>`, `?P<query>`, `?P<book>`, `?P<title>`, `?P<body>`);
      patterns whose handler consumes the raw text capture it as `?P<text>` instead of
      leaving the handler to re-derive it.
- [x] Declare `COMMANDS: list[Command]`. **Changed from the plan** — ordered for reading
      (which is also the help order) rather than transcribing the old control flow.
      See the Changelog entry below for why that is safe and what verifies it.
- [x] Replace the if/elif chain with a loop: `pattern.fullmatch(text)` → `handler(update, context, m.groupdict())`.
      The pending-selection guard stays AHEAD of the loop (it is conditional on live state, not on a pattern)
      and the "I didn't get that" fallback stays after it.
- [x] `build_help()` generates the help message from the registry. Learn's type list is
      read from `learn.SUPPORTED_TYPES` so `Learn recipe` cannot come back; the argument
      hints stay in the registry (help text is the registry's own).
- [x] Add the missing Diet note under IMPLEMENT, and fold the "quote from PDF" variant
      into the ADD QUOTE entry (the same pattern serves it as text).
- [x] `destructive=True` drives the shared expense guard note in the generated help, and is
      test-enforced to mean "goes through the `expense_safety` find-then-choose path".
      NOTE: it does not replace a hardcoded list — there was none. The guards are driven by
      the `expense_safety.UPDATE`/`DELETE` action constant passed into
      `_start_destructive_expense`, which is unchanged.
- [x] Extend `tests/test_router.py`: a per-command row test, pattern mutual exclusivity,
      the destructive-flag test, four help tests, and a `Learn podcast` row.
- [x] `ruff check .` + full suite green (660 passed, was 620) → commit 1.

## Milestone 2: dead code sweep (F-34) — commit 2

- [ ] `david.py`: drop the `headers` dict and the now-unused `NOTION_KEY`.
- [ ] `implement.py`: drop `_get_page_title_from_result`, `get_all_blocks`,
      `append_blocks_to_page`; call `get_page_title` / `get_children` / `append_children` directly.
- [ ] Update the three test files that monkeypatch the removed wrappers
      (`test_safe_writes.py`, `test_implement_sections.py`, `test_async_io.py`).
- [ ] `implement.build_manual_blocks`: drop the unused `source_title` parameter.
- [ ] `learn.py`: `metadata: dict | None = None`; remove the newspaper branch and correct
      the two comments that describe it (including `test_async_io.py:394`'s docstring).
- [ ] `notion_client.blocks_to_text(blocks, style="markdown"|"plain")` replaces all three
      flatteners; update `implement.py` and `implement_diet.py` call sites.
- [ ] Move `TYPE_EMOJI` + the `_get_db_id` mapping into `config.py` as one keyed map
      (emoji + db env var per type), so they cannot drift apart. `learn.py` keeps reading
      `os.environ` itself — config.py must not own feature IDs.
- [ ] `ruff check .` + full suite green → commit 2.
- [ ] Update CLAUDE.md's "Unreferenced code" open question (the `headers` entry is resolved).

## Open questions

- `implement.clear_page_blocks` and `david.DATABASE_ID` are on CLAUDE.md's
  "do not fix these by guessing intent" list and are NOT in the task scope. Left alone.
  (`clear_page_blocks` no longer exists in `implement.py`; only `clear_page_blocks_by_id`
  does, and it is live — the CLAUDE.md entry is stale, noted but not acted on.)
- Consolidating the flatteners changes what Claude sees for the Diet summary by a hair:
  dividers render as `---` and quotes/callouts gain their `> ` prefix, because the
  markdown style becomes the single one. No caller parses that text; it is prompt input only.

## Changelog

- 2026-08-07 — plan created after the verification pass in Milestone 0.
- 2026-08-07 — **COMMANDS is ordered for reading, not transcribed from the old control flow.**
  The plan said "current precedence order". That order (undo, help, B, Diag, DBs, Month, Find,
  Get, Remind, Add b, Add q, Learn, Implement, U e, D e, Add e) bears no relation to the order
  the help message presents things in, and the help is generated from this one list — so
  keeping it would have reshuffled the help into something markedly worse to read, with no
  way to fix it short of a second ordered list to drift against.
  Every pattern is anchored on a distinct literal prefix, so under `fullmatch` no input can
  satisfy two of them and the order carries no precedence today. That is asserted, not
  assumed: `test_no_input_can_be_claimed_by_two_commands` runs every table input and every
  complete help example through every pattern. A future command that does overlap turns it
  red and has to be positioned deliberately.
