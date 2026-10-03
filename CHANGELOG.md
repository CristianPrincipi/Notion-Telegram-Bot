# Changelog

Notable changes to David. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/CristianPrincipi/Notion-Telegram-Bot/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/CristianPrincipi/Notion-Telegram-Bot/releases/tag/v1.0.0
