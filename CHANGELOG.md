# Changelog

Notable changes to David. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **Kindle quotes.** With the KOReader plugin `citazioni`, a highlight on the
  Kindle is saved to its book in Letti and announced here, without going through
  Telegram or anyone else's server. A book not yet in Letti is created with no
  genre. Off unless `KINDLE_TOKEN` is set; needs a public domain on Railway.
  See [Kindle quotes](docs/features.md#kindle-quotes).

### Changed

- **`Learn` and `Implement` run on Claude Sonnet 5.5.** Claude Sonnet 4.5 retires
  on 30 November 2026, and after that both commands would have failed on every
  call. Per token the new model costs a third less. It thinks briefly before it
  answers, and that thinking is billed. David keeps it moderate, and lowest for
  picking which Manual sections a source touches. Each call's token use is in the
  Railway log, as before.
- **`ANTHROPIC_MAX_TOKENS` now defaults to 16000** (it was 8192), because the cap
  now counts Claude's thinking as well as its answer. If you set it on Railway,
  raise it to at least 16000 or delete it.
- **When Claude declines a request, the reply says which kind of check stopped
  it** (for example `general_harms`), where the API gives one.
- **`Learn book` pages no longer include quotations.** Nothing is read for a book,
  so any "quote" was Claude's recollection, and could be a sentence the author
  never wrote. Pages built from an article, video, podcast or PDF keep theirs.
- **`Implement` edits a Manual, and the Diet page, line by line** instead of
  rewriting each section it touches. Lines it does not change keep their
  formatting, and the reply lists what changed in each section: lines added,
  sharpened or removed, with every sharpened or removed line quoted next to
  Claude's reason. The Diet reply's ⚖️ conflict lines are gone: each change now
  carries its own reason.
- **A `Learn book` source merged into a Manual keeps its additions** even when it
  also asked to change an existing line. Only that change is held back, and the
  reply names the line. Before, the whole section was held back.
- **A `Learn book` source can only add to the Diet page**, as it already could
  only add to a Manual. Before, the Diet page warned about it and then let it
  rewrite anything.

- **PDFs are read with pypdf**, the maintained successor of PyPDF2, which is no
  longer updated. `Learn pdf` and `Add q` from a PDF work as before.
- **The `Learn book` reply no longer says to treat its quotes as unverified:**
  those pages have no quotes. It says claims, figures and dates.
- **`Add q` separates its parts with ` / ` instead of ` - `:**
  `Add q Dune / Fear / I must not fear.`, and as a PDF caption
  `Add q Dune / Fear / I must not fear | it will be gone` — the first and last
  words of the passage are separated by ` | `. A book or a quote title can now
  contain a dash, and a typed quote a slash. The old form is no longer
  understood. `Add b` is unchanged.

### Fixed

- **The bot token is no longer written to the Railway log.** Every request to
  Telegram was logged with its full address, and the address contains the
  token, so anyone who could read the log could take over the bot. A failed PDF
  download no longer shows the token in its error message either. If your logs
  were ever shared or kept, get a new token from BotFather (`/revoke`) and
  update `TELEGRAM_TOKEN` on Railway.
- **`Implement` no longer deletes lines nested under a Manual or Diet line.**
  Every line of a section it merged into was deleted and written back, so
  anything nested under one of them was lost, along with bold labels on lines it
  kept. A line with nested lines is now never rewritten or removed.
- **`Implement … - Diet` no longer rewrites a section Claude was never shown.**
  If the merge named a section outside the ones it was sent, that section's
  content was replaced with lines written without seeing it. Only the sections
  Claude was shown can be changed now.
- **`Implement` says when it could not remove a line.** If Notion refused to
  delete a line being replaced or removed, the reply still counted it as
  changed while the old line stayed on the page. The section is now listed as
  half-written, quoting the line to delete by hand.
- **`Implement` no longer applies two edits to one line** when Claude names the
  same section twice in its answer. The line could be written twice, a line added
  after one being removed could end up anchored on nothing, and a new step could
  get two headings.
- **`Implement … - Diet` says when a section was only half-written**, instead
  of listing it as unchanged.
- **A long quote is saved as one quote.** `Add q` used to start a new quote block
  every 1800 characters, so a pasted quote of 2500 characters showed up in Notion
  as two. One quote block now holds up to 180,000 characters; a passage cut out of
  a PDF that is longer than that is refused, saying so, and nothing is saved.
- **A quote goes to the book whose title you typed, and the reply names the book
  it went to.** With "Dune" and a later "Dune Messiah" in Letti, `Add q Dune /
  …` and a Kindle highlight from Dune were saved in Dune Messiah, and the reply
  said "Dune". A title that matches exactly now wins over a longer one, a name of
  three letters or fewer must match the whole title, and when the book found is
  titled differently the reply says which one it was.
- **`Add b` says why a book could not be added**, with Notion's own reason,
  instead of "Check your API keys" for every failure. A Kindle highlight whose
  book could not be created says why too, and stays queued.

## [1.1.0] - 2026-10-04

### Added

- **`v` says which build is running** — the commit, branch, commit subject,
  Railway deployment and uptime of the bot that is answering you. A failed deploy
  is silent, so this is how you check that a push landed. Where a value is not
  available it says `unknown` and names it, instead of guessing.

## [1.0.0] - 2026-10-03

The first public release.

### Added

- **Expenses:** `Add e`, `U e`, `D e` and `B`, filed against an automatically
  managed page per month. Updates and deletes search this month only, never
  guess between two matches, and can be reversed with `undo`.
- **Books and quotes:** `Add b`, `Add q`, including cutting a quote out of an
  attached PDF.
- **Learn:** summaries of YouTube videos, articles, podcast pages, PDFs and books
  into Notion, with duplicate-link detection and a visible warning on summaries
  written from recollection.
- **Implement and Get:** merge a Learn page into an area's Manual, rewriting only
  the sections it touches and logging every source; a structured Diet page; read
  any section back with `Get`.
- **Calendar:** `Remind`, `Agenda` and `Cancel` on Google Calendar, with
  confirmations that name the full date, and `undo` for a cancelled event.
- **Scheduled messages:** morning and evening briefings, budget pacing and a
  weekly recap, a weekly heartbeat, a Saturday list of unmerged Learn pages and a
  Sunday takeaway.
- **Notion template**, and documentation for setting David up from scratch:
  setup, configuration, Notion schema, features, customization, architecture and
  troubleshooting.

### Changed

- **`GOOGLE_CALENDAR_ID` no longer defaults to `primary`.** To a service account
  that is its own calendar, which nobody can see, so reminders were created and
  confirmed there and `Agenda` reported busy days as free. The calendar commands
  now refuse, with a message saying what to set, until it names your calendar.
  If you deployed before this change, set it before updating.

### Fixed

- **`Add q` no longer says a book is missing when Notion is down.** A failed
  library search used to read as "I didn't find 'Dune' in the library"; it now
  says it could not search, and passes on Notion's error.

[Unreleased]: https://github.com/CristianPrincipi/Notion-Telegram-Bot/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/CristianPrincipi/Notion-Telegram-Bot/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/CristianPrincipi/Notion-Telegram-Bot/releases/tag/v1.0.0
