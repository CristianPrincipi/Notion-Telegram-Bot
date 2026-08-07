# Plan: Diet routing/merge split (F-24) + reminder year rollover (F-17)
_Last updated: 2026-08-07_

Branch: `diet-routing`, off `main` at `54f65e3`. Two commits.
Baseline before any change: **668 passed**, `ruff check .` clean.

## Decision: routing stays on `config.ANTHROPIC_MODEL`

Measured, not assumed. Routing input is ~1,375 tokens (the 66 paths are ~525 of it;
the summary is the other ~850), so it is not the few-hundred-token call the cheap-model
idea assumes. Haiku would save ~$0.004/run against a split that already saves ~$0.010/run.
Against that: a routing miss is SILENT (the section is never fetched, never merged, never
written, and the confirmation still reports success), the spend guard bills every model at
flat Sonnet rates so the saving would not even register, and `implement.route_sections`
routes on the default model — diverging only for Diet means two answers, no measurement.
`complete_json` already takes `model=`, so this is reversible behind a recall benchmark.

## Milestone 1: split routing from merging (F-24) — commit 1

### The shape change

- [x] `read_diet_tree` → `read_diet_structure(page_id) -> (tree, block_map, error)`.
      Renamed because it no longer reads content, and a function named "tree" that
      returns no content is the kind of name that misleads the next reader.
- [x] Normalise the tree to ONE node shape at every level: every node is a dict of its
      children, a leaf is `{}`. No more `str` for a leaf H2 and `dict` for an H2 with H3s.
- [x] Structure read covers levels 1-3 only. Level 4 (H3 leaf content, ~45 of the ~67
      requests) becomes lazy — fetched after routing, for affected paths only.
- [x] Content is NEVER carried in the structure. A `""` that might mean "empty" or might
      mean "not fetched yet" is the empty-vs-unknown collapse this module already paid for
      once (`_children_of_many`). The structure does not claim to know content at all.
- [x] `read_section_contents(paths, block_map) -> ({path: content}, error)` — explicit,
      for a known set of paths, errors propagate (any failure fails the whole fetch).

### The two calls

- [x] `route_sections(paths, summary_text, summary_title)` — the taxonomy (content-bearing
      paths only: H3s and leaf H2s, NOT H1 containers or H2s that hold H3s, which cannot
      take content) plus the summary. Returns the affected paths.
- [x] `merge_sections(targets, summary_text, summary_title)` — only the routed sections'
      content plus the summary. Returns the same `updates` shape `apply_updates` already
      takes, so the write path is untouched.
- [x] Both through `anthropic_client.complete_json`, both under `ANTHROPIC_TIMEOUT` via
      `asyncio.to_thread`, matching every other Claude call in the codebase.
- [x] `apply_updates` UNCHANGED: append-then-delete ordering and the merge/replace
      unification from the safe-writes work are not touched. `tests/test_safe_writes.py`
      is the proof and must stay green without edits.

### No silent drops

- [x] Every stage that can lose a path REPORTS it: a routed path not in `block_map`
      (hallucinated), a path whose content read failed, a path routing named that merge
      declined to return. None of these may vanish into a success message.
- [x] Fix the skipped-paths report: currently `skipped[:8]` with no total, so beyond eight
      the rest are silently invisible. Show the total and an explicit "+N more".
- [x] TEST: a summary that legitimately touches several sections — assert every routed
      path survives into the merge payload and into the write.
- [x] TEST: the routing payload contains the paths and NO section content.
- [x] Report before/after token counts — re-measured through the SHIPPED prompt builders:
      23,346 -> 9,893 chars (**58% fewer**), ~5,836 -> ~2,473 est. tokens, $0.0175 -> $0.0074
      input per run. Notion reads for the page: 67 -> 25.

## Milestone 2: reminder year rollover (F-17) — commit 2

- [x] `parse_date_time`: roll to year+1 ONLY when the parsed datetime is more than
      `PAST_GRACE` (24h) in the past. Inside that window, return an error asking for
      confirmation instead of guessing — the 10:00-for-a-09:00-meeting case that currently
      books August 2027 and looks normal.
- [x] Make the year prominent in the confirmation (`reminder.handle_remind`).
- [x] `TIMEZONE.localize(..., is_dst=None)` so a nonexistent (spring-forward) or ambiguous
      (fall-back) local time raises instead of being silently shifted. Handle
      `NonExistentTimeError` and `AmbiguousTimeError` separately, each with a message that
      says what to send instead. Applies to the year+1 branch too.
- [x] Comment on the pytz → zoneinfo migration. **Written accurately**: zoneinfo does NOT
      raise on these times, it resolves them via `fold`. What it removes is the
      `localize()` footgun itself (tzinfo attaches at construction, arithmetic is
      DST-aware). Detecting nonexistent/ambiguous times there still needs an explicit
      fold-offset comparison. Saying "handles it natively" without that caveat would
      mislead whoever does the migration.
- [x] Tests per case: inside the grace window, outside it, the boundary either side,
      explicit year (future, past, two-digit), nonexistent time, ambiguous time, an
      ordinary time unaffected, the rollover DST-checked, 29.02 into a non-leap year,
      and the confirmation's year. Mutation-checked: reverting PAST_GRACE turns 3 red,
      reverting is_dst=None turns 3 red.
- [x] ADDED, not in the ticket: an optional year in the command (`DD.MM.YYYY`). Without
      it the refusal is a dead end — "confirm rather than guess" needs a way to answer,
      and re-sending the same bare date just hits the same refusal. Documented in the
      README row and the usage message.

## Open questions

- `implement.apply_section_updates` has the SAME `skipped[:8]` truncation. Out of scope
  for F-24 (this ticket names implement_diet), so it is flagged here, not changed.
- The summary is sent twice after the split — once to route, once to merge. That is why
  the saving is ~56% and not ~90%. Unavoidable without a summary-caching scheme that is
  not worth its complexity at this volume.
- The `[:30000]` tree slice disappears with the split rather than being raised. On the
  measured page the tree JSON is already 19,921 chars; ~1.5x more content per section
  would have started silently truncating the tail of the page out of the prompt.

## Changelog

- 2026-08-07 — plan created. Two deviations from the ticket as written, both flagged
  above and both reversible: (1) content fetching goes lazy, which the ticket implies
  ("fetch content for ONLY the returned paths") but which also moves ~45 Notion reads off
  the critical path; (2) the normalised tree carries no content, because the ticket's
  premise — that the tree is "the JSON handed to the model" — stops being true once the
  split lands, and a content field nobody sends is a field that goes stale.
