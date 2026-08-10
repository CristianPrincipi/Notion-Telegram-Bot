# Plan: ingest hygiene — idempotency, unverified sources, honest partial writes

_Last updated: 2026-08-10_

> **Part 2 (this branch, second pass): what an unverified source may do to a Manual.**
> Milestones 5–7 below were added after the first three shipped, from the decisions
> taken on the open question at the bottom of this file. Options 1 (provenance) and
> 3 (the merge rule) were built; option 2 (a force gate on `Implement`) was declined.

Branch: `claude/ingest-hygiene-qkue5z`, off `main` at `b63b6fd`.
Baseline before any change: **786 passed**, `ruff check .` clean.
After: **853 passed**, `ruff check .` clean.

Three fixes to the Learn ingest path, before anything new is built on top of it. They
share a shape: each one is a place where David reports a state it is not in — a second
page presented as a first, a recollection presented as a reading, a partial write
presented as a failure.

## Milestone 1: Idempotency — the same URL is not summarised twice

- [x] `services/learn.normalise_source_url` — one canonical form for comparison:
      `http`→`https`, host lowercased and de-`www.`'d, default port dropped, `utm_*`
      and the named tracking params stripped, remaining params sorted, fragment
      dropped, trailing slash dropped. Non-http input is returned untouched.
- [x] A `Source URL` property on the Learn database, written on every URL-bearing
      Learn (`video`, `article`, `podcast`) and holding the NORMALISED url — the page
      body keeps the original in its `🔗 Source` link, so nothing is lost.
- [x] `notion_client.database_property_type(db_id, name)` — the schema question the
      check needs, cached per database exactly like `title_property` (populated only
      on success, read before any network call).
- [x] Before extracting — before the fetch and before Claude — query the Learn
      database for that normalised URL. A hit replies with the page title, when it was
      saved, and the exact command to force a re-summarisation. Nothing is fetched,
      nothing is paid for.
- [x] The property may not exist yet. Absent → the check is SKIPPED and said out loud,
      and the property is not written either, so this deploys against an untouched
      Notion database instead of failing every Learn with a 400.
- [x] A check that ERRORS is not a check that passed: the failure is reported and the
      run continues, because refusing to Learn at all is the worse failure here. The
      message names the risk (a possible duplicate).
- [x] Both `url` and `rich_text` property types are supported, so whichever one gets
      created by hand works.

## Milestone 2: Unverified sources are marked, and Implement can see it

- [x] `config.UNVERIFIED_MARKER` / `UNVERIFIED_NOTE` / `KNOWLEDGE_RECALL_TYPES` —
      the marker lives in config because two layers need it and neither owns it.
- [x] `Learn book` (the only type with no source text) stamps a red callout at the top
      of the page: unverified, generated from model recollection, no source text read.
- [x] The marker is a SENTENCE in the page body, not a property. That is deliberate:
      it survives `blocks_to_text`, so it is in the very text Implement sends to Claude,
      and it is visible in Notion without opening a properties panel.
- [x] `config.is_unverified_source(text)` — one predicate, so the write and the read
      cannot drift.
- [x] `Implement` surfaces it in the plan message (before the merge runs) and in the
      first-run build message, both prefixed with the same warning line.
- [ ] Anything beyond that is NOT implemented in this branch — see `## Open questions`,
      which is the recommendation asked for.

## Milestone 3: Partial writes report what actually landed

- [x] `notion_client.Written` — a `list` subclass carrying `batches_done`,
      `batches_total`, `blocks_total` and a `summary` string. A list, so every caller
      that already reads it as "the blocks that were created" is unaffected.
- [x] `append_children` returns it. Stops on the first failed batch, as before.
- [x] `add_Quote` stops hand-rolling its own batching loop (it was a copy of
      `append_children` minus the `after` anchor) and returns `(written, error)`.
      Both quote flows report three states: saved, saved partially with the tally and
      a duplication warning, or failed with nothing written.
- [x] `create_page` already returned `(page_id, error)` with BOTH set on a partial
      append; that state now says so in the error string instead of being ambiguous.
- [x] `create_learn_page` returns `(ok, result, incomplete)` and `run_learn` reports
      the partial case as its own outcome rather than as `✅ Saved to Notion!`.
- [x] `implement.apply_section_updates` returns `(applied, skipped, partial)`. A
      section whose append half-landed is no longer listed under "these are
      unchanged", which was false for exactly that section.
- [x] `implement._first_run` reports a Manual page created with incomplete content.

## Milestone 4: Tests

- [x] `tests/test_learn_idempotency.py` — the normalisation table (12 cases), the
      duplicate short-circuit (no fetch, no Claude call), the reply naming the date,
      the force token, the missing property, the failed check, both property types.
- [x] `tests/test_unverified_sources.py` — the callout is stamped on `book` and on
      nothing else, it survives the flattening Implement reads through, the plan
      message carries the warning, and a verified source's does not.
- [x] `tests/test_partial_writes.py` — a mid-run batch failure at every one of the
      four call sites, each asserting the MESSAGE, not just the return value.
- [x] Every existing test double for `append_children` / `add_Quote` /
      `create_learn_page` updated to return the type production returns, and
      `conftest.written_ok` / `written_nothing` / `written_half` added so the next
      one does not have to invent it.
- [x] Each guard verified by reverting it and watching its named test go red —
      all 19 of them, in two passes (see `## Changelog`).

## Milestone 5: The Sources ledger

- [x] `SOURCES_SECTION`, and `record_source` — one bullet per Implement run, naming
      the source, whether it was read or recalled, and the date. Creates its own
      `📚 Sources` heading when the Manual has none.
- [x] A pure append, which is why it can run after the section writes with none of
      Hard Rule 2's ordering care: there is no window in which the page holds
      neither the old content nor the new.
- [x] **Two independent guards** keep it unwritable by a merge, because either one
      alone is undoable by a reasonable-looking change: `routable_sections` keeps
      it out of what the router is SHOWN, and `apply_section_updates` refuses a
      Sources path however it was arrived at.
- [x] Still INDEXED — `record_source` has to find it, and `Get Sources - Brain` has
      to read it. The exclusion is about routing, not about the index.
- [x] Provenance goes here and NOT into the merged lines: an inline marker is sent
      back to the model as "current content" on the next run, reworded at its
      discretion, and never removed if a real source later confirms the claim.

## Milestone 6: The additions-only rule

- [x] `_survives` — a subsequence walk over whitespace-normalised lines. Order is
      significant (a numbered routine with two steps swapped has been changed);
      whitespace is not (a reflowed line is not a rewrite).
- [x] `_hold_back_rewrites` runs in `_sectioned_run` BEFORE the writer, and only
      when the source is unverified. Policy about what may be written lives next to
      the merge; `apply_section_updates` stays a writer and its three buckets keep
      describing what Notion did.
- [x] Held-back sections are a fourth outcome in the reply, naming the lines that
      would have been lost. Not folded into "skipped": both are unchanged, but one
      is Notion failing and one is David refusing.
- [x] `_UNVERIFIED_MERGE_RULE` appended to the merge prompt — the hint that makes
      the check pass often enough to be usable, never the guarantee.
- [x] ~~A force gate on `Implement`~~ — dropped: declined deliberately. A gate that
      fires on every book page is a gate you stop reading; the plan message is the
      checkpoint.

## Milestone 7: The Diet path

- [x] `run_implement` branches to Diet BEFORE it fetches the Learn page, so the
      unverified detection never ran for `Implement … - Diet` at all. Its plan
      message now carries the same warning, computed from the summary text it
      already reads. No ledger — the Diet page is a fixed toggle tree with nowhere
      to put one.

## Milestone 8: Tests for parts 5–7

- [x] `tests/test_implement_sections.py` — Sources absent from the routing paths
      (asserted against the REAL `_sectioned_run`, not against `routable_sections`
      in isolation, because the failure mode is someone rebuilding that list at the
      call site); still indexed; an H3 beneath it excluded too; a merge naming it
      refused at the write; and seven ledger tests driving the real `record_source`.
- [x] `tests/test_unverified_sources.py` — the rule bites on a reworded line and on
      a reordered one, tolerates whitespace, lets a pure addition through, leaves a
      new section alone, and does not apply to verified sources. Plus the merge
      prompt's text, asserted on the PROMPT rather than on the call — deleting the
      appended rule leaves a call-level assertion green.
- [x] All 12 new guards verified by reverting them. One (the merge prompt) was
      STILL GREEN on the first pass and got a second test; see `## Changelog`.

## What is enforced, and what is not

Copied into the PR body verbatim, because the difference is the whole value:

- **Enforced, deterministically.** No line already in a Manual is deleted or
  reworded by a merge whose source was a recollection. David holds both sides
  before it writes.
- **Prompt-only.** That the model cooperates, and the tone of what it adds.
- **Not checked by anything.** Whether an ADDED line is true. Nothing can check
  that; it is why the ledger exists.
- **Unmeasured.** How often the rule holds a section back. `_MERGE_SYSTEM` asks for
  "the FULL merged content" and models reword while reproducing.

## Changelog

- **The `Source URL` guard is two guards, and the first revert check only found
  one.** `run_learn` must pass an empty property type when the column is missing,
  AND `create_learn_page` must skip the property when it is given one. Reverting
  the second left the test green, because the test that covers the first stubs
  `create_learn_page` out entirely. Split into two tests, one per line. Recorded
  because the guard read as a single decision and is not.
- **A Notion block has two shapes, and only one of them reaches Implement.** The
  first version of `test_the_marker_survives_the_flattening_implement_reads_through`
  flattened the blocks `build_notion_blocks` returns and got `"---\n---"`: a block
  on the way IN carries `{"text": {"content": …}}`, and `extract_rich_text` reads
  only the `plain_text` a block carries on the way BACK. Asserting against the
  request shape would have passed on a builder whose output flattens to nothing.
  The test converts to the response shape by hand, for the same reason the
  RCDATA-title test builds its shape by hand.
- **A test asserting the merge call was TOLD does not guard what it was told.**
  The first version stubbed `merge_sections` and asserted `unverified is True`;
  deleting the rule text the real function appends left it green. The revert pass
  caught it. There are now two tests — one for the plumbing, one driving the real
  `merge_sections` with only `complete_json` replaced. Same shape as the
  `Source URL` guard that turned out to be two guards, and the second time this
  branch has been caught guarding the wrapper instead of the thing.
- **`test_a_huge_manual_does_not_get_its_tail_dropped` used `📚 Sources` as its
  tail section**, so it routed to and rewrote the section that is now David's
  ledger — it would have asserted the exact opposite of the new rule. Renamed to
  `📎 References`, which tests the same tail-not-dropped property. Worth recording
  because any Manual with real content under `Sources` is now equally frozen: that
  section can no longer be rewritten by a merge, by design.
- **The duplicate check is two more Notion calls before the fetch**, so
  `test_async_io`'s exact-sequence assertion for Learn grew two entries rather
  than being loosened. They were also making real network calls in the suite until
  they were stubbed — the offline guarantee catches this only if the stubs exist.

## Open questions

- ~~**How far should Implement go with an unverified source?**~~ — resolved in
  milestones 5–7. Option 1 was built as the **Sources ledger**, not as inline
  suffixes: the merge replaces a section's full content, so an inline marker goes
  back through the model next run and survives at its discretion, and nothing ever
  removes it if a real source later confirms the claim. Option 3 was built as an
  **enforced check** rather than the prompt-only hedging first proposed. Option 2
  (the force gate) was declined — see Milestone 6.
- **How often the additions-only rule holds a section back is unmeasured.** If it
  turns out to be most of them, the honest next move is to loosen it to "no
  existing line may be DROPPED, rewording allowed" — which cannot be checked
  deterministically and would have to be described as prompt-only, losing the one
  property that makes the current rule worth having.
- **The Diet page has no ledger.** Its structure is a fixed H1>H2>H3 toggle tree
  with nowhere to put one, and inventing a section there is a schema change to
  argue for separately. `Implement … - Diet` warns but does not record.
- **`Learn book` is still not deduplicated.** It has no URL, so this branch does not
  cover it; the equivalent would be a title match against `LETTI_ID`, which is a
  fuzzy-match decision of its own and was not asked for.
- **The `Source URL` property has to exist in Notion for the dedup to do anything.**
  It is not created by the code (an integration can add properties to a database, but
  doing it implicitly on a database David does not own the schema of is a bigger
  decision than this branch). Absent, David says so and carries on.
