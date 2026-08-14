# Plan: M4 — takeaway of the week (proactive Step 5)

_Last updated: 2026-08-14_

Branch: `takeaway-of-the-week`, off `main` at the M3 merge.
Baseline before any change: **939 passed**, `ruff check .` clean.

A weekly message resurfacing one `✅ Key Takeaways` bullet from a random Learn
page. Listed as Step 5 in `proactive/__init__.py` and never built. Cheapest item
on the roadmap: pure Notion read, **no Anthropic call**, no new dependency. The
value is that a personal knowledge base you only ever write to is not a knowledge
base.

## Decisions taken before writing code

**1. The heading becomes one constant, and that is the first commit.**
`services/learn.py:529` writes `_heading2("✅ Key Takeaways")` and this reader
will look for it. Two string literals in two modules is exactly how
`UNVERIFIED_MARKER` was nearly reworded in one place and silently undetected in
the other. `config.TAKEAWAYS_HEADING`, used by both, plus a test that drives the
WRITER and then the READER so the two cannot drift.

**2. The chooser is a parameter, not a seed.** `build_takeaway(*, choose=random.choice)`.
Seeding is global state that another test can disturb, and it pins the algorithm
rather than the decision. A parameter lets a test pass `lambda seq: seq[0]` and
assert on an exact page — and it is used for BOTH choices (which page, which
bullet), so there is one source of randomness to control.

**3. Sampling is without replacement, and attempts are bounded.** A page picked
and found to have no takeaways is removed from the pool, so the bound counts
distinct pages rather than dice rolls — otherwise a database of 3 pages could
burn 5 attempts on the same one. Exhausting the bound is `(None, None)`: a
database with no takeaways anywhere is not an error, it is a quiet week.

**4. A page read that FAILS is not the same as a page with no takeaways**, and
this is the trap the whole milestone sits over. If a failed `get_children` just
moved on to the next page, then a Notion outage — where every read fails —
exhausts the attempts and returns `(None, None)`: silence, meaning "nothing to
say". That is the bug M2 spent a milestone removing.

So the first read error is remembered, and the outcome follows
`build_morning_briefing`'s shape rather than choosing between the two:

    (text, None)   found a takeaway, nothing went wrong
    (text, error)  found one, but a page could not be read — send AND report
    (None, error)  found none, and at least one read failed
    (None, None)   found none, everything was readable — a genuinely quiet week

**5. An unverified source stays labelled when it is resurfaced.** Not in the
roadmap, added deliberately and cheaply: `Learn book X` pages carry
`config.UNVERIFIED_MARKER` in their body, and a takeaway lifted out of one and
sent on its own is a recollection presented as a fact with its provenance
stripped. The blocks are already in hand, `config.is_unverified_source` already
exists, and the line costs one `if`. This is the same reasoning that put the
marker in the page body rather than in a property.

**6. Bullets are collected from the heading to the NEXT HEADING**, not to the end
of the page. On a David-written page the takeaways are last, so the two rules
agree today — which is exactly why the stricter one has to be written now, while
nothing depends on the accident.

## Milestone 1: the shared constant

- [x] `config.TAKEAWAYS_HEADING = "✅ Key Takeaways"`, with the two-modules
      reasoning.
- [x] `services/learn.py:529` uses it.

## Milestone 2: config

- [x] Day/hour/minute for the job, named weekday constant, in a free slot (every
      existing one is taken and `test_no_two_jobs_share_a_slot` asserts that).
- [x] `TAKEAWAY_MAX_ATTEMPTS`.

## Milestone 3: the builder

- [x] New `proactive/takeaway.py`, `build_takeaway(*, choose=random.choice) -> (text, error)`.
- [x] Query `LEARN_ID` (`query_database` paginates), choose a page, read its
      children, find the heading, collect the bullets under it.
- [x] Skip-and-retry without replacement, bounded; the four outcomes above.
- [x] Name the source page, and mark it when the source is unverified.

## Milestone 4: registration

- [x] `proactive/scheduler.py` — `_takeaway_job`, `run_daily` with `days=`,
      `chat_id=`, `name="takeaway"`. Plain text: bullets and titles are user data.

## Milestone 5: tests

- [x] New `tests/test_takeaway.py`:
  - [x] A page with takeaways produces a message naming the page.
  - [x] Bullets are collected only up to the next heading — not the whole rest
        of the page.
  - [x] A page with no takeaways section is skipped, not reported as an error.
  - [x] A database where NO page has takeaways → `(None, None)` after a bounded
        number of attempts (assert it terminates AND that it stopped at the
        bound).
  - [x] The same page is never tried twice.
  - [x] A Notion QUERY failure → `(None, error)`.
  - [x] A page READ failure with no takeaway found anywhere → `(None, error)`,
        not silence (decision 4).
  - [x] A page read failure with a takeaway found elsewhere → `(text, error)`.
  - [x] The chooser is injectable, so the test is deterministic.
  - [x] A takeaway from an unverified page says so.
- [x] The writer/reader constant test: build a page via the REAL
      `services/learn.build_notion_blocks`, hand those blocks to the takeaway
      reader, assert it finds them. Two `in` checks in two modules is how a
      string gets reworded in one of them.
- [x] `tests/test_scheduler.py` — registration, slot, day, no collision.
- [x] `tests/test_async_io.py` — `PROACTIVE_JOBS` gains the job (it is what
      asserts the builder runs off the event loop).
- [x] **Guard-revert pass**: three guards reverted one at a time. The read
      failure collapsed to a skip and the without-replacement removal each turned
      named tests red immediately. The third — letting collection run past the
      next heading — turned NOTHING red, and that is in `## Changelog`.
- [x] Full suite green: **962 passed**, up from 939. `ruff check .` clean.

## Milestone 6: docs

- [x] `README.md` scheduled-messages table.
- [x] `proactive/__init__.py` — mark Step 5 implemented.
- [x] `CLAUDE.md` — module map row.
- [x] `ROADMAP.md` — tick M4.

## Milestone 7: ship

- [ ] Commit, push, PR, CI green, merge.

## Open questions

- **Every page is fetched to choose one.** `query_database` returns the whole
  Learn database (paginated) and the choice happens in Python, because "has a
  takeaways section" is in the page BODY and Notion cannot filter on it. Fine at
  this size and for a weekly job; recorded so it is not mistaken for an
  oversight.
- **Nothing remembers which takeaways have been sent**, so the same bullet can
  come round twice. Storing that would need state, and Notion is the only
  durable store David has — a "last resurfaced" property is a schema change on a
  database the user owns. Out of scope, and repetition is not obviously wrong for
  a resurfacing job.


## Changelog

- **The heading-boundary test was passing against nothing, and the revert pass is
  what found it.** `test_the_bullets_stop_at_the_next_heading` built the whole
  message and asserted the stray line was absent from it. With the boundary
  removed it stayed green: the deterministic chooser takes the FIRST bullet, so a
  wrongly-collected extra one at the end never reached the text being asserted
  on. Rewritten to assert on `takeaways_in`'s output — the thing the guard
  actually produces — with the end-to-end version kept alongside it and a chooser
  that picks the LAST bullet, which is where the bug lands. Both go red on the
  revert now.
- **Notion's write shape is not its read shape.** `notion_client.rich()` emits
  `{"text": {"content": …}}`; the API's response carries `{"plain_text": …}`,
  which is what `extract_rich_text` reads. So the writer/reader cross-check could
  not hand `build_notion_blocks`' output straight to the reader — it would be
  testing a shape production never sees. The test converts
  (`as_notion_returns_it`, documented as the round trip) rather than the reader
  learning to accept both: widening production code so a fixture passes is the
  wrong direction, and it would have hidden exactly this asymmetry from the next
  person.
- **Decision 5 (the unverified label) survived contact with the code cheaply**,
  as expected: the blocks are already in hand for the takeaways scan, so
  `is_unverified_source(blocks_to_text(blocks))` costs no extra read.
