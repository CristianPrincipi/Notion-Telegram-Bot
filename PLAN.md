# Plan: M1 — one truncation budget per Claude call (the Implement paths)

_Last updated: 2026-08-14_

Branch: `implement-one-budget`, off `main` at `cba66a0`.
Baseline before any change: **900 passed**, `ruff check .` clean.

`CLAUDE.md`: *"A budget belongs to whatever consumes it, and there is one of it."*
Learn obeys it. Implement does not, five times over, with three different numbers
and no comment at any of them:

| Site | Slice |
| --- | --- |
| `services/implement.py:331` — `route_sections` | `source_text[:60000]` |
| `services/implement.py:404` — `merge_sections` | `source_text[:60000]` |
| `services/implement.py:641` — `build_manual` | `source_text[:60000]` |
| `services/implement_diet.py:367` — routing | `summary_text[:50000]` |
| `services/implement_diet.py:432` — merge | `summary_text[:50000]` |

So a long Learn page is merged into a Manual from its first 60k characters (50k
on the Diet path) and the reply reads exactly like a full merge. Same shape as
the extractor/summariser bug that is already documented as the reason the rule
exists — you find out from a thin Manual entry months later, with no way to tell
which entries were affected.

## Decisions taken before writing code

**1. Two constants, one per path, each named for the input it bounds:
`MANUAL_SOURCE_CHARS = 60_000` and `DIET_SUMMARY_CHARS = 50_000`.**
Per-path rather than unified, and named for the input rather than for the
milestone, so a second budget on either path can be added beside its sibling
instead of overloading a shared number.

Both are **separate from `SUMMARY_INPUT_CHARS`** and stay separate: that budget
covers a prompt that is nothing but source text, these cover prompts that also
carry the sections being merged into.

**1b. `MANUAL_EXISTING_CHARS` / `DIET_TREE_CHARS` are NOT added, because there is
nothing to name.** The accumulating inputs were the right thing to look for —
they are the ones that grow without bound — but both caps were removed by the
sectioning work and survive only as prose about the code that was deleted:

| Reference | What it is |
| --- | --- |
| `services/implement.py:12` | module docstring, "It **used to** send the ENTIRE Manual… truncated at 40k on the way IN" |
| `services/implement_diet.py:296` | comment, "`decide_updates` **used to** send CURRENT_TREE… sliced at `[:30000]`" |
| `tests/test_implement_sections.py:12`, `:343` | test docstrings naming the old cap |

`grep -rn "\[:[0-9_]\{4,\}\]"` over the repo returns five live slices: the three
`source_text[:60000]` and two `summary_text[:50000]` this milestone removes, plus
`[:2000]` on Notion rich-text and title fields, which is a Notion API limit, not
a budget. `test_a_huge_manual_does_not_get_its_tail_dropped`
(`tests/test_implement_sections.py:342`) drives a 60,000-character section
through the real path and asserts it arrives whole — that test is the proof the
40k cap is gone, and it passes at HEAD.

**Adding those two caps back would be a regression, not a safeguard.** Existing
content is sent whole or not at all (Hard Rule 3), and the merge returns the FULL
merged content for each section, which *replaces* what is on the page. So a
section clipped on the way in comes back short on the way out, and the tail is
deleted from Notion — strictly worse than the source-side bug this milestone
fixes, because the source-side one only degrades a summary while this one
destroys the record. If a defensive ceiling is wanted there it has to refuse the
run, not clip the input, and that is its own decision. Left open below.

**1c. A truncation logs at WARNING with the pre-truncation length.** `run_learn`
logs its cut at INFO; INFO is the wrong level for "this answer was built from
part of the input", and the length before the cut is the only number that says
how much was lost. Both cut sites log
`WARNING … source is N chars, cut to M` and both say it in the reply as well —
the log is for afterwards, the reply is for now.

**2. One cut per run, at the top, before anything reads the text.**
`run_implement` (`services/implement.py:844`) and `run_implement_diet`
(`services/implement_diet.py:627`) each call `blocks_to_text` once. Cut there —
and specifically **before `is_unverified_source`**, so the predicate sees exactly
the string the model will see. The three prompt builders must then NOT re-slice,
for the reason `config.py` already gives for extractors: if both the caller and
the callee cap, a source of exactly the budget and one cut down to it are the
same string, and the warning becomes unreliable at its own boundary.

**3. The cut and its wording live in ONE function, used by both paths.**
`fit_to_budget(text, cap, *, label) -> (text, warning | None)` — synchronous and
pure, so it is directly testable, with the caller doing the `await notify(...)`
and the function doing the WARNING log. Defined in
`services/implement.py` and imported by `services/implement_diet.py` (safe: the
dependency already runs the other way and lazily, `implement.py:813`). Two copies
of a warning string is how one of them gets reworded and the other does not.

Explicitly NOT in scope: unifying it with `run_learn`'s copy of the same shape.
That is a third budget for a different consumer, and folding it in means editing
the one path this milestone exists to leave alone.

**4. The marker survives the cut — confirm, then assert.**
`config.UNVERIFIED_MARKER` is written as the FIRST block of a Learn page
(`services/learn.py:502`, above the TL;DR), so a head-slice always keeps it and
`is_unverified_source` still fires. Confirmed by reading; a test pins it, because
if it were ever moved to the bottom, truncation would silently disarm the
additions-only rule on exactly the long recollections that most need it.

## Milestone 1: the constants

- [x] `config.py`: add `MANUAL_SOURCE_CHARS` and `DIET_SUMMARY_CHARS` beside
      `SUMMARY_INPUT_CHARS`, with a comment saying why they are separate budgets
      (these prompts carry section contents as well as the source), why each is
      named for the input it bounds, and that this is the only place either
      number lives.

## Milestone 2: the Manual path — `services/implement.py`

- [x] `fit_to_budget(text, cap, *, label)` → `(text, warning | None)`, next to
      the other module helpers. Logs at WARNING with the pre-truncation length
      when it cuts — and the percentage that survived, which is the number that
      makes the length mean something at a glance.
- [x] `run_implement`: cut immediately after `blocks_to_text`, before
      `is_unverified_source`, and `await notify(warning)` when there is one.
      Plain `notify`, not `notify_md` — the string is David's own and
      interpolates only integers, so there is nothing to escape and no reason to
      risk a parse_mode rejection on the message that exists to warn you.
- [x] Delete `[:60000]` from `route_sections`, `merge_sections` and
      `build_manual`; one-line comment at each saying the caller owns the cap.
      The merge builders' comments also say the SECTION texts are deliberately
      uncapped, which is the mistake the next reader is likeliest to make.

## Milestone 3: the Diet path — `services/implement_diet.py`

- [x] Import `fit_to_budget` from `services.implement`.
- [x] `run_implement_diet`: cut immediately after `blocks_to_text`
      (`:627`), before the lock is taken and before `is_unverified_source` is
      evaluated at `:714`.
- [x] Delete `[:50000]` from both prompt builders, same comment.

## Milestone 4: tests

`tests/test_implement_sections.py` stubs `route_sections` / `merge_sections` at
module level, so what reached each call is readable off the fixture;
`tests/test_diet_routing.py` stubs `complete_json` instead, so the REAL prompt
builders run and the prompt text itself is what gets asserted. Both are needed:
the first proves the cut happened once, the second proves the builder did not cut
again.

- [x] A source longer than the budget reaches `route_sections` and
      `merge_sections` already at exactly the cap.
- [x] The real `route_sections` / `merge_sections` / `build_manual` pass their
      argument through WHOLE — drive them with a stubbed `complete_json` and an
      over-budget string, assert the prompt carries all of it. This is the
      assertion that the second cap is gone rather than merely lowered, and it
      turned out to be the ONLY one that can be — see `## Changelog`.
- [x] The reply says the source was truncated, and says it **only** when it was.
- [x] A source of exactly `MANUAL_SOURCE_CHARS` is not reported as truncated —
      the boundary. ~~and the assertion that proves the two-cap bug is gone~~ —
      it is not that, and its docstring now says so.
- [x] The cut logs at WARNING and the log line carries the pre-truncation length
      (`caplog`), because the reply is gone as soon as you scroll past it.
- [x] The unverified marker survives truncation of a long recollection page:
      `_hold_back_rewrites` still engages and the merge still gets
      `unverified=True`.
- [x] The same five for the Diet path in `tests/test_diet_routing.py`, minus
      `_hold_back_rewrites` (the Diet path has no additions-only rule; assert the
      plan message still names the source as unverified).
- [x] **Guard-revert pass**: five guards reverted one at a time — the `[:60000]`
      in `merge_sections`, the `[:50000]` in the Diet merge, the cut in
      `run_implement`, the cut in `run_implement_diet`, and the WARNING log
      demoted to INFO. Four turned a named test red. The fifth turned nothing
      red; what it found is in `## Changelog`.
- [x] Full suite green: **913 passed**, up from 900. `ruff check .` clean.

## Milestone 5: docs

- [x] `CLAUDE.md` — the budget paragraph names only the Learn path. Add the
      Implement paths and the two constants; keep the existing text about
      `run_learn` intact rather than rewriting it.
- [x] `ROADMAP.md` — tick M1's to-do and test boxes as they land, in the same
      commit as the work, per its own "How to work from this file". Its changelog
      records the two findings, since the next milestone is read from that file
      and not from this one.

## Milestone 6: ship

- [ ] Commit on `implement-one-budget`, push, open a PR, let CI go green before
      merging. Never straight to `main` — Railway keeps the old version running on
      a failed deploy, so a broken one is silent.

## Changelog

- **The four-constant scheme was cut to two, because two of the four sites do not
  exist.** `MANUAL_EXISTING_CHARS` / `DIET_TREE_CHARS` were to name the caps on
  the accumulating inputs — the right thing to look for, since those are the ones
  that grow without bound. Both were removed by the sectioning work and survive
  only as prose about deleted code (`services/implement.py:12`,
  `services/implement_diet.py:296`, plus two test docstrings);
  `test_a_huge_manual_does_not_get_its_tail_dropped` puts 60,000 characters
  through the real path and asserts they arrive whole. Adding those caps back
  would delete content from Notion rather than protect it, for the reason in
  Decision 1b. Recorded in `ROADMAP.md`'s changelog too, where the next
  milestone will be read from.
- **A guard-revert pass found a missing test rather than confirming a guard.**
  Putting `[:50000]` back into the Diet merge builder left every Diet test green.
  The reason generalises and is now written into `CLAUDE.md`: once the caller
  cuts to N, a builder cutting to N is a **no-op no end-to-end test can see** —
  including the boundary tests, which is why their docstrings now say what they
  do not prove. `test_the_prompt_builders_do_not_cut_again` drives the builders
  directly with text the caller never cut. Written with the slice still in place,
  watched go red, then the slice removed.
- **Two test-fixture measurements had to be probed rather than assumed.**
  `blocks_to_text` decorates each block (`- ` / `• `) and SKIPS an empty one, so
  a helper building "a source of exactly N characters" reads the decoration as
  zero if it measures against `""`. Both helpers probe with real text. Without
  it the boundary tests were off by two characters — passing or failing for a
  reason unrelated to what they exist for.

## Open questions

- **The accumulating half of each prompt is still unbounded, and capping it by
  clipping is not available.** Routed section contents (Manual) and routed leaf
  contents (Diet) are sent whole, per Hard Rule 3, and the merge's reply replaces
  what is on the page — so clipping the input deletes the tail from Notion rather
  than merely degrading an answer. A ceiling there would have to REFUSE the run
  and name the section, in the family of `title_property`'s refusal. Whether that
  is worth building is a decision this milestone does not take. What bounds it
  today: only routed sections are ever sent, so the size scales with the update
  rather than with the knowledge base.
- **`fit_to_budget` is the third copy of this shape**, after `run_learn`'s and —
  once this lands — nothing else. Folding `run_learn` into it is a one-function
  change and was left out deliberately: this milestone must not edit the one path
  that already obeys the rule.
- **The cap almost never fires in practice.** An Implement source is a Learn
  *summary*, typically a few thousand characters, not the original source. That
  is an argument for the cut being cheap, not for leaving five anonymous
  literals: the one time it fires is the time you cannot detect afterwards.
