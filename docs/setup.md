# Setup

From nothing to your own David in about an hour. You will create a Telegram bot,
copy the Notion template, give David keys to Notion, Anthropic and (optionally)
Google Calendar, and deploy it on Railway.

David is **single-user and self-hosted**: it runs on your accounts, writes to
your Notion and answers only you. Nothing is shared with anyone else.

1. [Prerequisites](#1-prerequisites)
2. [Telegram](#2-telegram)
3. [Notion](#3-notion)
4. [Google Calendar](#4-google-calendar) (optional)
5. [Anthropic and Supadata](#5-anthropic-and-supadata)
6. [Deploy on Railway](#6-deploy-on-railway)
7. [Run locally](#7-run-locally) (optional)
8. [First-run checklist](#8-first-run-checklist)

Every variable mentioned here is described in [Configuration](configuration.md).
Keep a text file open as you go and collect them in `NAME=value` form — that is
exactly what Railway's raw variable editor and a local `.env` both accept.

## 1. Prerequisites

| You need | For | Cost |
| --- | --- | --- |
| A Telegram account | talking to David | free |
| A Notion account | where everything is saved | the free plan works |
| An [Anthropic API](https://console.anthropic.com) account with credit | `Learn` and `Implement` | pay per use — see below |
| A [Railway](https://railway.com) account | running David around the clock | a paid plan; see their pricing |
| A Google account + a Google Cloud project | `Remind`, `Agenda`, `Cancel`, the briefings | free *(optional)* |
| A [Supadata](https://supadata.ai) key | YouTube transcripts for `Learn video` | see their pricing *(optional)* |
| A GitHub account | forking the repo so Railway can deploy it | free |

Running locally as well needs **Python 3.12** (what CI tests on) and `git`.

**What the AI part costs.** Only `Learn` and `Implement` call Anthropic. Most
runs cost a few cents; a very long source (a two-hour transcript, a long PDF)
can reach about $0.20. David keeps an estimate of the day's spend and refuses
`Learn` and `Implement` once it passes `ANTHROPIC_DAILY_BUDGET_USD` (default
$5), so a runaway day is bounded.

## 2. Telegram

1. Open [@BotFather](https://t.me/botfather), send `/newbot`, and follow the
   prompts. It replies with a token like `123456789:AA…`.
   → `TELEGRAM_TOKEN`
2. Send `/start` to [@userinfobot](https://t.me/userinfobot). It replies with your
   numeric ID. → `OWNER_ID`
3. Scheduled messages and error reports go to `CHAT_ID`. For a private chat with
   your bot — the usual case — it is the **same number** as `OWNER_ID`.
   → `CHAT_ID`
4. Open a chat with your new bot and press **Start**. A bot cannot message
   someone who has never messaged it, so without this the scheduled messages
   have nowhere to go.

David answers `OWNER_ID` and nobody else. Messages from anyone else are dropped
before any command runs, and get no reply at all.

## 3. Notion

### Copy the template

Open the
[**David — Notion Template**](https://protective-cost-810.notion.site/David-Notion-Template-3e853cc47bc281a28e30c6f0d37acbcf)
and press **Duplicate** (top right). It lands in your workspace with eight empty
databases — Expenses, Months, Books, Areas, Learn, Brain, Finance and Diet —
each with exactly the columns David uses ([Notion schema](notion-schema.md)).

### Create an integration

1. Go to [notion.so/profile/integrations](https://www.notion.so/profile/integrations)
   and create a **new integration**: type *Internal*, your workspace.
2. Under **Capabilities**, it needs *Read content*, *Update content* and
   *Insert content*.
3. Copy the **Internal Integration Secret**. → `NOTION_KEY`

### Give it access

Open your copy of the template's **top page**, then **•••** → **Connections** →
add your integration. Everything inside the page inherits the connection, so
this one step covers all eight databases.

Forgetting this is the most common setup error: Notion answers as if the
databases did not exist.

### Copy the IDs

Open each database as a full page. Its ID is the 32 characters in the URL just
before `?v=`:

```
https://www.notion.so/yourworkspace/3e45b71dd7d34c909281b3bcf5508815?v=…
                                    └────────── the ID ──────────┘
```

| Open | Copy into |
| --- | --- |
| Expenses | `EXPENSES_ID` |
| Books | `LETTI_ID` |
| Learn | `LEARN_ID` |
| Brain | `BRAIN_ID` |
| Finance | `FINANCE_ID` |
| Diet | `DIET_ID` |
| the **`Literature` page** inside Areas (a page, not a database: the ID is the last 32 characters of its URL) | `LITERATURE_ID` |

Months and Areas need no variable. Brain, Finance and Diet are optional: leave
any area you do not want unset, and `Implement`/`Get` will tell you it is not
configured if you name it.

Once David is running, `DBs` lists every database it can see with its ID, and
`Diag` checks the Expenses setup end to end — a quick way to confirm you copied
the right ones.

## 4. Google Calendar

Optional: skip this section and everything except `Remind`, `Agenda`, `Cancel`
and the calendar half of the briefings works.

David uses a **service account** — a robot Google account with a key file —
because it never expires and needs no browser login, which is what a server
needs.

1. In the [Google Cloud console](https://console.cloud.google.com), create a
   project (any name).
2. **APIs & Services → Library** → search *Google Calendar API* → **Enable**.
3. **IAM & Admin → Service Accounts → Create service account.** Any name; it
   needs no roles.
4. Open it → **Keys → Add key → Create new key → JSON**. A `.json` file
   downloads. Its whole content, braces included, is
   → `GOOGLE_CREDENTIALS_JSON`
   (In a `.env` file, put it on one line inside single quotes. In Railway, paste
   it as is.)
5. Note the service account's email — the `client_email` in that file, ending in
   `iam.gserviceaccount.com`.
6. In [Google Calendar](https://calendar.google.com) → **Settings** → your
   calendar → **Share with specific people** → add that email with
   **Make changes to events**.
7. On the same settings page, under **Integrate calendar**, copy the
   **Calendar ID**. For your main calendar it is your Gmail address.
   → `GOOGLE_CALENDAR_ID`

**Do not skip step 7.** Without `GOOGLE_CALENDAR_ID` the calendar commands
refuse, and say so. (`primary` is refused too: for a service account it means
*its own* calendar, which nobody can see.)

## 5. Anthropic and Supadata

**Anthropic** (required):

1. In the [Anthropic console](https://console.anthropic.com), add credit under
   **Billing**.
2. **API keys → Create key.** → `ANTHROPIC_API_KEY`
3. Optionally set a daily ceiling in dollars, `ANTHROPIC_DAILY_BUDGET_USD`
   (default `5`).

**Supadata** (optional, for `Learn video`): create an account at
[supadata.ai](https://supadata.ai) and copy your API key. → `SUPADATA_KEY`

## 6. Deploy on Railway

1. **Fork** this repository on GitHub.
2. In Railway: **New Project → Deploy from GitHub repo** → pick your fork.
3. Open the service → **Variables → Raw Editor**, paste every `NAME=value` you
   collected, and save. Railway redeploys.
4. Open **Settings → Deploy** and make sure the start command is
   `python david.py`. (The repo's `Procfile` says so too; setting it explicitly
   removes any doubt about which file the builder reads.)
5. David polls Telegram — it needs **no public domain and no port**. Leave
   networking alone.
6. Keep **one replica**. Two copies of the bot polling with the same token make
   Telegram reject both.

**Reading the logs:** open the latest deployment → **View logs**. A good start
logs the scheduled jobs being registered and then goes quiet. If a variable is
missing, the log shows `David cannot start` followed by the complete list.

**Updates:** every push to your fork's `main` redeploys. If a build fails,
Railway keeps the previous version running and says nothing in Telegram — check
the deployment status after you push.

## 7. Run locally

Useful for trying David before deploying, or for working on it. Stop the
Railway deployment first if one is running with the same bot token — only one
copy can poll Telegram at a time.

```bash
git clone https://github.com/<you>/Notion-Telegram-Bot.git
cd Notion-Telegram-Bot
python3.12 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt "python-dotenv[cli]"
cp .env.example .env              # then fill it in
dotenv run -- python david.py
```

`.env` is gitignored. `dotenv run` loads it into the environment and starts
David exactly as Railway would; there is nothing in the code that reads the
file.

## 8. First-run checklist

Send these in order. Each one checks a different piece of the setup.

| Send | Expect | If not |
| --- | --- | --- |
| `h` | the command list | no reply at all → `OWNER_ID` is wrong, or the bot is not running |
| `Month` | this month's page (e.g. `October 2026`), created in Months, with its ID | Notion access — see [Troubleshooting](troubleshooting.md) |
| `Diag` | the Expenses database, its columns, and the month page — all found | it names what is missing |
| `Add e Test 1` | the expense saved, in category Food | a Notion error naming a column → [schema](notion-schema.md) |
| `B` | this month's recap, with the 1.00 you just added in its total | — |
| `D e Test` | the expense deleted | — |
| `undo` | the expense restored | — |
| `D e Test` | deleted again — this is clean-up | — |
| `Remind Test tr 10` | a confirmation naming tomorrow's full date at 10:00 | calendar access — see [Troubleshooting](troubleshooting.md) |
| *(open Google Calendar)* | **the Test event, tomorrow at 10:00** | it is not there → `GOOGLE_CALENDAR_ID` names a different calendar ([§4 step 7](#4-google-calendar)) |
| `Agenda tr` | tomorrow, named in full, with the Test event | — |
| `Cancel Test` | the event deleted | — |
| `Learn article <any article URL>` | a summary page in Learn (costs a few cents) | the reply says why |

Then wait for the scheduled messages: the morning briefing at 07:30 and, on
Sunday at 20:30, the **heartbeat**, which always sends. A missing heartbeat
means David is down. All times are Europe/Rome
([Customization](customization.md#timezone-and-schedule-times) to change it).
