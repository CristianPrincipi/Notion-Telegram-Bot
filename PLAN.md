# Plan: ingest hygiene — idempotency, unverified sources, honest partial writes

_Last updated: 2026-08-10_

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
- **The duplicate check is two more Notion calls before the fetch**, so
  `test_async_io`'s exact-sequence assertion for Learn grew two entries rather
  than being loosened. They were also making real network calls in the suite until
  they were stubbed — the offline guarantee catches this only if the stubs exist.

## Open questions

- **How far should Implement go with an unverified source?** Recommended, in order,
  none of it built here:
  1. Carry the marker INTO the Manual — prefix each line merged from an unverified
     source with `(unverified)` or write the Sources bullet as
     `Title — unverified, model recollection`. Today the Manual is the one place the
     provenance is lost, and it is the place the content is read from years later.
  2. Refuse by default, force with `Implement … - Brain !`, mirroring the Learn force
     token from Milestone 1. Symmetric, and it puts the decision at the moment the
     content leaves the quarantine of its own page.
  3. Tell the merge prompt about it, so unverified claims merge as hedged rather than
     as fact.
  (1) is the one worth doing; (2) is worth doing if the Manual is ever shared or acted
  on without re-reading; (3) is cheap but the weakest — it asks the model to be careful
  with its own recollection.
- **`Learn book` is still not deduplicated.** It has no URL, so this branch does not
  cover it; the equivalent would be a title match against `LETTI_ID`, which is a
  fuzzy-match decision of its own and was not asked for.
- **The `Source URL` property has to exist in Notion for the dedup to do anything.**
  It is not created by the code (an integration can add properties to a database, but
  doing it implicitly on a database David does not own the schema of is a bigger
  decision than this branch). Absent, David says so and carries on.
