# Notion-Telegram-Bot

**David** — a single-user personal Telegram bot that writes to Notion, reads Google
Calendar, and summarises content with the Anthropic API. Runs as a polling worker
on Railway (`worker: python david.py`).

## Access control

David is single-user by design: every command spends the owner's Notion and
Anthropic quota, and several write to or delete from Notion databases. Only the
Telegram account whose numeric ID matches `OWNER_ID` can use the bot.

Authorization is enforced with a python-telegram-bot filter, not a check inside
the handlers, so an unauthorized update is dropped by the dispatcher and never
reaches handler code — a newly added command cannot forget to check. Messages
from anyone else are logged at `WARNING` and answered with silence (a reply would
confirm to whoever probed the bot that it is live).

To find your ID, send `/start` to [@userinfobot](https://t.me/userinfobot).

## Environment variables

David validates all of these at startup (`config.validate()`). If any **required**
variable is missing or blank, the process exits immediately and lists *every*
problem at once, so a misconfigured deploy takes one fix rather than one redeploy
per variable. Missing **optional** variables only log a warning — the bot starts
fine without them and loses the feature named below.

| Variable | Required | Purpose |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | **Required** | Telegram bot token from [@BotFather](https://t.me/botfather). |
| `OWNER_ID` | **Required** | Numeric Telegram user ID allowed to use the bot. Everyone else is ignored. Must be numeric. |
| `CHAT_ID` | **Required** | Telegram chat that receives scheduled briefings and error reports. |
| `NOTION_KEY` | **Required** | Notion internal integration secret. |
| `EXPENSES_ID` | **Required** | Notion Expenses database ID. |
| `MONTH_ID` | **Required** | Notion page ID of the current month; expenses relate to it. **Update this each month.** |
| `LETTI_ID` | **Required** | Notion Books ("Letti") database ID. |
| `LITERATURE_ID` | **Required** | Notion page ID of the Literature area; books relate to it. |
| `LEARN_ID` | **Required** | Notion Learn database ID for videos, articles, podcasts and PDFs. |
| `ANTHROPIC_API_KEY` | **Required** | Anthropic API key used to summarise Learn and Implement content. |
| `SUPADATA_KEY` | Optional | Supadata API key for YouTube transcripts. Without it, `Learn video` fails. |
| `GOOGLE_CREDENTIALS_JSON` | Optional | Service-account JSON for Google Calendar. Without it, reminders fail. |
| `GOOGLE_CALENDAR_ID` | Optional | Target calendar. Defaults to `primary`. |
| `DIET_ID` | Optional | Notion Diet area database ID. Needed by `Implement [Page] - Diet`. |
| `BRAIN_ID` | Optional | Notion Brain area database ID. Needed by `Implement [Page] - Brain`. |
| `FINANCE_ID` | Optional | Notion Finance area database ID. Needed by `Implement [Page] - Finance`. |
| `BUDGET_CEILING` | Optional | Monthly budget ceiling in euros. Defaults to `300`. |
| `DATABASE_ID` | Unused | Read in `david.py` but never referenced anywhere. Left in place; safe to drop. |

`Implement [Page] - [Area]` resolves its target from `{AREA}_ID`
(see `get_area_db_id` in `implement.py`), so adding a new area means adding the
matching environment variable.

## Commands

Send `h`, `help` or `aiuto` to the bot for the in-chat version.

| Command | What it does |
| --- | --- |
| `Add b [Name] - [Author] - [Genre]` | Add a book. Genres: `s` `h` `m` `p` `a` `ph` |
| `Add q [Book] - [Title] - [Quote]` | Add a quote to a book |
| `Add q [Book] - [Title] - [Begin] / [End]` | Extract a quote from an attached PDF (send as the file's caption) |
| `Add e [Name] [Amount] [Category]` | Add an expense. Categories: `s` `f` `g` `o` (default `f`) |
| `U e [Name] [Amount] [Category]` | Update an expense |
| `D e [Name]` | Delete (archive) an expense |
| `B` | Monthly budget recap |
| `Remind [Name] [DD.MM] - [HH.MM]` | Create a Google Calendar event |
| `Learn video\|article\|podcast\|book\|pdf [source]` | Summarise into the Learn database |
| `Implement [Page] - [Area]` | Merge a Learn page into an Area manual |
| `Diag` / `Find [name]` / `DBs` | Notion ID diagnostics |

### File uploads

PDFs are accepted on the `Learn pdf` and `Add q ... / ...` captions. Both paths
reject non-PDF MIME types and files over **15 MB**, and both cap the download at
30 s per request / 2 minutes overall — Telegram's own download helper has no
timeout and can hang a worker indefinitely.

## Development

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The test suite runs fully offline: `tests/conftest.py` installs a fake
environment and every HTTP call is intercepted, so nothing reaches Notion,
Telegram or Google. CI (`.github/workflows/ci.yml`) runs the same two commands on
every push.

`tests/test_router.py` is the pre-deploy gate — a table of
`input → handler → parsed args` covering every command. Rows marked `known_bug`
assert current, wrong behaviour on purpose; fixing one of those bugs is expected
to turn its row red, and the row should be updated in the same commit.
