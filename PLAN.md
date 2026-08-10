# Plan: article extraction quality, and one Notion read-path bug

_Last updated: 2026-08-10_

Branch: `article-extraction`, off `main` at `7024892`.
Baseline before any change: **743 passed**, `ruff check .` clean.
After: **770 passed**, `ruff check .` clean.

Learn's article output feeds Implement, which feeds the Manual. Extraction quality is
not a leaf concern — it propagates, and it surfaces months later as a thin Manual entry
with nothing pointing back at the extractor. All four defects below were silent in
exactly that way, which is why every fix here ships with the reply or the log saying
something it did not say before.

## Milestone 1: One truncation budget, and it announces itself

- [x] `config.SUMMARY_INPUT_CHARS = 100_000` — one number, owned by what CONSUMES the
      text. The extractor capped at 12,000 while the summariser accepted 100,000, so
      every article over ~12k chars was summarised from its first eighth.
- [x] `extract_article` stops truncating entirely, returning the body WHOLE. This is
      what makes truncation detectable: an extractor that pre-capped at the budget
      would leave `run_learn` unable to tell a source of exactly the budget from one
      already cut down to it.
- [x] `summarize_with_claude` uses the constant as a silent backstop, not as the cut.
- [x] `run_learn` truncates once, for **all five** content types, and says so in the
      reply. A 3-hour transcript and a 400-page PDF hit the same cap an article does.

## Milestone 2: The nested-`<title>` crash, and an except that stops lying

- [x] `.string.strip()` → `" ".join(soup.title.get_text().split())`. `.string` is None
      whenever `<title>` has more than one child, and the AttributeError was swallowed
      into "could not extract content" — a message pointing at the website.
- [x] **Not `get_text(strip=True)`**, which was the planned spelling and is wrong: it
      strips each string *before* joining, so `Post <em>name</em>` comes back as
      `Postname`. Found by the test, not by reading. See `## Changelog`.
- [x] Empty `<title>` falls back to the URL rather than to `""`.
- [x] `except requests.RequestException` (their fault, named as a fetch problem) split
      from `except Exception` (ours — names the exception type, and `logger.exception`
      puts the traceback in the Railway log). The broad catch stays: a service may not
      raise across a module boundary. What changed is that it no longer wears the fetch
      failure's clothes.
- [x] `logger` added to `services/learn.py`.

## Milestone 3: trafilatura primary, BS4 fallback

- [x] **Verified installable before committing to it**, which was the condition. lxml
      was rejected in the past because it built from C source and Railway's image lacks
      `libxml2-dev`/`libxslt-dev`; it ships manylinux wheels now (6.1.1). Proof:
      `pip install --dry-run --only-binary=:all: --platform manylinux_2_17_x86_64` over
      the whole of `requirements.txt` resolves clean at cp311/312/313 — that flag fails
      if *any* package in the tree lacks a wheel. Then installed for real and imported.
      Cost: 11 new transitive dependencies, all wheels, none compiled.
- [x] `extract_article` split into one fetch (`_fetch`) and two parsers
      (`_extract_trafilatura`, `_extract_bs4`). Our own `requests.get` is kept rather
      than `trafilatura.fetch_url`, so the network call stays inside the timeout story
      `SOURCE_FETCH_TIMEOUT` is built on.
- [x] `MIN_ARTICLE_CHARS = 250` — trafilatura's failure mode on an odd layout is a
      *fragment* (a caption, a standfirst), not an empty result, so `is None` alone
      cannot catch it.
- [x] Which parser won is logged, so a permanent silent downgrade to the fallback is
      visible rather than inferred from summaries slowly getting worse.
- [x] `import trafilatura` at module scope, unguarded — a `try/except ImportError`
      here is the exact construction that produced the dead newspaper branch.
- [x] `author` is now populated from metadata; it was hardcoded `""`.

## Milestone 4: `search_page_in_db` stops hardcoding `"Name"`

- [x] `notion_client.title_property(db_id)` — finds the property with `type == "title"`,
      cached per database under a `threading.RLock` (not `asyncio.Lock`: these run in
      `to_thread` workers, where an asyncio lock acquires without ever blocking).
- [x] **Cache populated only on success, read before any network call.** A database read
      once keeps working through a later Notion outage; only one never read
      successfully can fail. That asymmetry is what makes refusing safe.
- [x] On an unreadable schema it REFUSES and names the schema read — never widens back
      to `"Name"`, which would restore the misleading "No page found" intermittently.
- [x] A schema that reads fine but has no title property gets its own distinct error.

## Milestone 5: Tests

- [x] The title falls back independently of the body — trafilatura metadata, then the
      page's own `<title>`, then the URL. trafilatura can score the article correctly and
      still return no title, and naming the Notion page after its address is a worse
      outcome than one extra parse on a path that rarely runs.
- [x] `tests/test_article_extraction.py` — 16 tests: nested `<title>` (twice: through
      `extract_article` and against `_extract_bs4` directly, so the fix cannot be masked
      by trafilatura answering first), empty title, no 12k cut, no pre-truncation to the
      budget, the partial-summary warning, its mirror (a warning that always fires is not
      a warning), all-content-types coverage, internal-vs-fetch error, boilerplate
      exclusion, BS4 fallback, short-fragment rejection, author, parser logging.
- [x] `tests/test_notion_client.py` — a database whose title column is `"Título"`; the
      schema read once per database not once per lookup; the cache surviving a 502; a
      failed read NOT being cached; refusal naming the real cause; a non-database ID.
- [x] `forget_title_properties` autouse fixture clears the module-level cache around
      every test — without it whichever test ran first would satisfy every later test's
      schema lookup and the assertions would pass without the lookup happening.
- [x] **Every guard verified by reverting it and watching its named test go red**, per
      CLAUDE.md. All seven: `.string.strip()`, the 12k cap, the truncation warning, the
      collapsed except, hardcoded `"Name"`, caching failures, and the title fallback.

## Changelog

- **`get_text(strip=True)` was specified and is wrong.** It fixes the crash but welds
  words together (`Post <em>name</em>` → `Postname`), because `strip=True` strips each
  string before joining. Replaced with `" ".join(get_text().split())`, which keeps the
  word boundary the markup implies and still collapses titles laid out over several
  lines. Caught by `test_a_title_with_a_nested_tag_does_not_crash_extraction`.
- **The nested `<title>` arrives in TWO shapes, and the first fix only covered one.**
  CI went red on a test that was green locally: same beautifulsoup4 (4.15.0), different
  CPython patch release. `html.parser` treats `<title>` as RCDATA — the HTML5-spec
  behaviour — as of 3.12.13, so the element arrives as ONE string still containing
  `<em>name</em>`; on 3.12.3 it arrives as a tag with children. The first shape raises
  `AttributeError` on `.string.strip()`; the second raises nothing and silently makes
  the raw markup the Notion page's name. `_TAG_RE` now flattens the second, and a test
  builds that shape by hand so it is covered whatever the runner is running. Recorded
  because "it passes locally" was actively misleading here.
- **The first test fixture measured trafilatura's deduplicator, not the code.** Building
  a long body by repeating one paragraph 20 times produced 54 characters of extracted
  text, because trafilatura drops duplicate blocks — so the "no truncation" assertions
  failed for a reason unrelated to truncation. `paragraphs()` now indexes each one.
  Recorded because the fixture looked obviously correct and was not.

## Noticed, not fixed

- `extract_pdf` and `extract_youtube` have no size bound of their own; the only limit is
  `SUMMARY_INPUT_CHARS` at the summarisation step, which is now at least reported. A
  hostile or enormous source is still fully materialised in memory first.
- `_fetch` does not bound the response body either. `clients/telegram_files.py` has the
  bounded-download pattern if that is ever worth mirroring here.
