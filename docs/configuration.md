# Configuration

David reads everything it needs from environment variables. On Railway they are
the service's **Variables**; on a local run they live in a `.env` file (start
from [`.env.example`](../.env.example)).

## What happens at startup

`config.validate()` runs before anything else. If a **required** variable is
missing or blank, David exits immediately and lists *every* problem in one
message, so a misconfigured deploy takes one fix rather than one redeploy per
variable. A missing **optional** variable only logs a warning: the bot starts and
loses the feature named below.

A variable that exists but is blank (`BUDGET_CEILING=` in a `.env` file, or a
Railway variable created without a value) counts as **unset**, exactly as if the
line were not there.

## Required

| Variable | What it is |
| --- | --- |
| `TELEGRAM_TOKEN` | Your bot's token, from [@BotFather](https://t.me/botfather). |
| `OWNER_ID` | Your numeric Telegram user ID. Only this account can use the bot; everyone else is ignored. Send `/start` to [@userinfobot](https://t.me/userinfobot) to find it. |
| `CHAT_ID` | The chat that receives scheduled messages and error reports. For a private chat with the bot, this is the same number as `OWNER_ID`. |
| `NOTION_KEY` | Your Notion internal integration secret. |
| `EXPENSES_ID` | The Expenses database ID. |
| `LETTI_ID` | The Books database ID (*letti* is Italian for "read" — the name stayed). |
| `LITERATURE_ID` | The ID of the `Literature` **page** inside the Areas database. Every book is related to it. |
| `LEARN_ID` | The Learn database ID — where `Learn video`, `article`, `podcast` and `pdf` save. |
| `ANTHROPIC_API_KEY` | Your Anthropic API key, used by `Learn` and `Implement`. |

## Optional

| Variable | Default | What it is |
| --- | --- | --- |
| `GOOGLE_CREDENTIALS_JSON` | — | The service-account JSON key for Google Calendar, pasted whole. Without it, `Remind`, `Agenda`, `Cancel` and the briefings' calendar half fail. |
| `GOOGLE_CALENDAR_ID` | — | The calendar David reads and writes: your calendar's ID (for your main Google calendar, your Gmail address). **Needed whenever you set `GOOGLE_CREDENTIALS_JSON`** — without it the calendar commands refuse, and so does `primary`, which for a service account is its *own* calendar, one nobody can see. |
| `SUPADATA_KEY` | — | [Supadata](https://supadata.ai) API key for YouTube transcripts. Without it, `Learn video` fails; everything else works. |
| `BRAIN_ID` | — | The Brain area database ID, for `Implement … - Brain` and `Get … - Brain`. |
| `FINANCE_ID` | — | The Finance area database ID, for `Implement … - Finance` and `Get … - Finance`. |
| `DIET_ID` | — | The Diet area database ID, for `Implement … - Diet` and `Get … - Diet`. |
| `{AREA}_ID` | — | Any other area: the area's name in capitals plus `_ID` (`Sleep` → `SLEEP_ID`). See [Adding an area](customization.md#adding-an-area). |
| `BUDGET_CEILING` | `300` | Your monthly budget in euros. Drives `B`, the morning pace line and the overspend warning. |
| `MONTHS_DB_ID` | discovered | The database the month pages live in. Unset, David finds it by following the Expenses `Account` relation, which is what you want. |
| `MONTH_ID` | — | **Outage fallback only.** If Notion cannot be reached the first time David looks up this month's page, it uses this page ID rather than nothing. David finds the real page itself; you never need to update this. |
| `ANTHROPIC_MAX_TOKENS` | `8192` | The longest answer one Claude call may return. Raise it if a long source comes back as a truncated page — that error message names this variable. |
| `ANTHROPIC_DAILY_BUDGET_USD` | `5` | Estimated Anthropic spend allowed per day. Once reached, `Learn` and `Implement` are refused until midnight, Europe/Rome. |
| `ANTHROPIC_SPEND_FILE` | `.anthropic_spend.json` | Where the day's running spend is kept. On Railway the disk is wiped on every deploy, so a redeploy resets the day's count — a bounded over-spend, not a broken bot. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. An unrecognised value logs a warning and falls back to `INFO`. |

## Where each feature gets its IDs

| Feature | Variables |
| --- | --- |
| Expenses, `B`, budget messages | `EXPENSES_ID` (+ `MONTHS_DB_ID`, `MONTH_ID`, `BUDGET_CEILING`) |
| Books and quotes | `LETTI_ID`, `LITERATURE_ID` |
| `Learn` | `LEARN_ID` (`Learn book` saves to `LETTI_ID`), `ANTHROPIC_API_KEY`, `SUPADATA_KEY` for videos |
| `Implement`, `Get` | `{AREA}_ID` for the area you name, `LEARN_ID` for the source page, `ANTHROPIC_API_KEY` |
| `Remind`, `Agenda`, `Cancel`, briefings | `GOOGLE_CREDENTIALS_JSON`, `GOOGLE_CALENDAR_ID` |

Finding each Notion ID is covered in [Setup § Notion](setup.md#3-notion).

## Set by Railway, not by you

`v` reports which build is running from four variables Railway puts into the
container it starts:

`RAILWAY_GIT_COMMIT_SHA`, `RAILWAY_GIT_BRANCH`, `RAILWAY_GIT_COMMIT_MESSAGE`,
`RAILWAY_DEPLOYMENT_ID`.

Do not set them yourself, and do not add them to `.env`. They are deliberately
absent from the tables above, which list what *you* configure: a value you typed
would make `v` describe a build that is not the one running. Where one is missing
— a local run, or a platform that names it differently — `v` says `unknown` and
names the variable instead of guessing.
