# David

**A personal Telegram bot that keeps your life admin in Notion** — expenses,
books, reading notes and calendar reminders — and turns the videos, articles and
PDFs you send it into notes, then into living manuals you can query from the
chat.

David is **self-hosted and single-user**. You run it on your own accounts —
your Telegram bot, your Notion, your Anthropic key — and it answers you and
nobody else.

## What it does

- **Expenses and budget.** `Add e Coffee 2,50` files an expense against this
  month's page; `B` shows where the month stands. Updates and deletes never guess
  between two matches, and `undo` reverses the last one.
- **Books and quotes.** Add a book, save a quote, or cut a quote out of a PDF.
- **Learn.** Send a YouTube link, an article, a podcast page, a PDF or a book
  title; get a structured summary page in Notion. The same link twice is caught
  before it costs anything.
- **Implement and Get.** Merge what you learned into a per-area **Manual** —
  only the sections it touches are rewritten, and every source is logged — then
  read any section back with `Get`.
- **Calendar.** `Remind Dentist tr 10`, `Agenda`, `Cancel` — on your Google
  Calendar, with replies that name the full date so a misread is visible.
- **It speaks first.** Morning and evening briefings, a budget warning when the
  month is running hot, a weekly recap, and a Sunday heartbeat that proves it is
  alive.

The full list, with examples: [Features](docs/features.md).

## Quick start

1. **Telegram:** create a bot with [@BotFather](https://t.me/botfather) and find
   your user ID with [@userinfobot](https://t.me/userinfobot).
2. **Notion:** duplicate the
   [**Notion template**](https://protective-cost-810.notion.site/David-Notion-Template-3e853cc47bc281a28e30c6f0d37acbcf),
   create an internal integration and connect it to the template's top page.
3. **Keys:** an [Anthropic API](https://console.anthropic.com) key; optionally a
   Google service account for the calendar and a [Supadata](https://supadata.ai)
   key for YouTube.
4. **Deploy:** fork this repo, create a [Railway](https://railway.com) project
   from the fork, and paste your variables
   ([`.env.example`](.env.example) lists them all).
5. **Say hello:** send `h` to your bot, then work through the
   [first-run checklist](docs/setup.md#8-first-run-checklist).

Every step in detail: **[Setup](docs/setup.md)** — about an hour from nothing.

## Documentation

| Page | For |
| --- | --- |
| [Setup](docs/setup.md) | Getting your own David running, step by step |
| [Configuration](docs/configuration.md) | Every environment variable |
| [Notion schema](docs/notion-schema.md) | The databases and columns David expects |
| [Features](docs/features.md) | Every command, with examples, and the scheduled messages |
| [Customization](docs/customization.md) | Categories, schedules, areas, prompts, new commands |
| [Troubleshooting](docs/troubleshooting.md) | What to check when something does not work |
| [Architecture](docs/architecture.md) | How it is built and why — for changing the code |
| [Design notes](docs/design-notes.md) | The bug behind each rule the code keeps |
| [Changelog](CHANGELOG.md) | What changed, release by release |

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The suite runs fully offline. [Architecture](docs/architecture.md) explains the
layers and the rules the tests enforce; [`CLAUDE.md`](CLAUDE.md) states them for
contributors (and for Claude Code).

## License

[MIT](LICENSE).
