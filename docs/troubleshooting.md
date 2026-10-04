# Troubleshooting

Organised by what you see. Most problems are a variable or a Notion connection;
`Diag` and `DBs` answer most Notion questions from inside the chat, and the
Railway deployment log answers the rest.

## The bot does not answer at all

In order of likelihood:

1. **`OWNER_ID` is not your ID.** David ignores everyone else *silently* — by
   design, so a stranger cannot tell the bot is alive. Check the number with
   [@userinfobot](https://t.me/userinfobot).
2. **David is not running.** Open the Railway deployment log. If it says
   `David cannot start`, see the next section.
3. **Two copies are running** — a local run and Railway, or two Railway
   replicas, with the same token. Telegram lets one poll at a time and the log
   shows `Conflict: terminated by other getUpdates request`. Stop one.
4. **The token is wrong** — the log shows an authorization error at startup.
   Copy it again from @BotFather.

## The log says "David cannot start"

The message lists every required variable that is missing or blank, with what
each one is for. Set them all, then redeploy (or re-run `dotenv run -- python
david.py`). See [Configuration](configuration.md).

If instead the log ends in a `ValueError` about converting a string to a number,
a numeric variable — `BUDGET_CEILING`, `ANTHROPIC_MAX_TOKENS` or
`ANTHROPIC_DAILY_BUDGET_USD` — holds something that is not a number. (Blank is
fine; `abc` or `300€` is not.)

## A command fails with a Notion 400

Notion rejects a write that names a column the database does not have, or gives
a column the wrong type of value — and nothing is saved. The error usually says
which column, e.g. `Author is not a property that exists`.

- Compare the database with [Notion schema](notion-schema.md): every column name
  is exact, capitals included.
- `Learn article` failing while `Learn video` works is almost always the missing
  `Author` column in Learn.
- If you renamed a column on purpose, rename its constant in `config.py` too
  ([Customization](customization.md#notion-column-names)).
- `Diag` checks the Expenses columns for you.

## Notion says a database does not exist

`Could not find database with ID …` (a 404) means one of two things:

- **The integration is not connected.** Open the template's top page → **•••** →
  **Connections**, and add it ([Setup § 3](setup.md#give-it-access)).
- **The ID is wrong**, or is a page's ID where a database's belongs (or the
  reverse for `LITERATURE_ID`). Send `DBs`: it lists every database David can see
  with its ID.

## Calendar commands say `GOOGLE_CALENDAR_ID` is not set

Set it to your calendar's ID ([Setup § 4](setup.md#4-google-calendar), step 7).
David refuses to run without it — and refuses `primary` — because for a service
account `primary` is its *own* calendar: reminders would be created where nobody
sees them, and `Agenda` would read that empty calendar and call a busy day free.

## Reminders are confirmed but never show up

`GOOGLE_CALENDAR_ID` names a calendar other than the one you are looking at.
Check it against **Settings → your calendar → Integrate calendar → Calendar ID**.

## Calendar commands fail

- **"Not found" or "forbidden"** — the calendar is not shared with the service
  account's email, or is shared without **Make changes to events**.
- **"API has not been used in project … or it is disabled"** — enable the Google
  Calendar API in the service account's project.
- **A JSON error on the first calendar command** —
  `GOOGLE_CREDENTIALS_JSON` was pasted incompletely. It must be the whole file,
  braces included; in a `.env` file, inside single quotes.

## `Learn video` fails

- **`SUPADATA_KEY not set`** — add the key ([Setup § 5](setup.md#5-anthropic-and-supadata)).
- **No transcript** — the video has no captions Supadata can read. Nothing to
  fix on your side.

## A summary or Manual comes back cut short

The reply says the answer was truncated and names `ANTHROPIC_MAX_TOKENS`. Raise
it (try `16000`) and run the command again.

If the reply instead warns that the *source* is too long — "Source is … characters;
summarising the first …" — the source was longer than David reads in one go. That is a
deliberate limit, and the reply is telling you the summary is partial.

## `Learn` or `Implement` is refused: daily budget reached

David's estimate of today's Anthropic spend passed `ANTHROPIC_DAILY_BUDGET_USD`
(default $5). Wait for midnight (Europe/Rome), or raise the variable.

## `Implement` says an update is already in progress

Two `Implement` runs into the same area are not allowed at once — the second is
refused rather than merged against a page that is changing under it. Wait for
the first to finish.

## Scheduled messages never arrive

- **`CHAT_ID`** is not the chat you are reading. For a private chat with the bot
  it equals `OWNER_ID`.
- **You never pressed Start** in the bot's chat. A bot cannot open a
  conversation.
- The Sunday 20:30 **heartbeat** always sends. If even that is missing, David is
  not running.

## Expenses go to last month, or `B` shows the wrong month

Send `Month`. It finds or creates this month's page and says which one it is
using. It also runs by itself every night at 00:05.

## I pushed a change and nothing happened

Send `v`. It answers with the commit David is actually running; if that is not
the commit you pushed, the deploy did not land. Railway keeps the previous
version running when a build fails — or is never started — and says nothing in
Telegram. Open the deployment list in Railway and check the latest one.

If `v` itself gets "I didn't get that", the running version is older than the
command.

## `v` says `unknown — … is not set`

David reads the build from variables Railway injects — see
[Configuration](configuration.md#set-by-railway-not-by-you). On a local run they
do not exist, and `unknown` is the correct answer there. On Railway, a missing
one most likely means the service was not deployed from a GitHub repository, or
that Railway has renamed the variable. Do not set it by hand.

## The automated PR review fails within seconds

For contributors with the review workflow enabled: a review that fails in a few
seconds, with no tokens spent, means the `CLAUDE_CODE_OAUTH_TOKEN` secret has
stopped working. Replace it — see
[Architecture](architecture.md#the-automated-review-on-a-fork).

## The automated PR review is green but left no comment

Then it has told you nothing yet — do not read it as a pass. Open the review job
and read its last step, **Show what the review concluded**: it prints the review's
final message and the tool calls it was refused.

- **"No execution file".** The pull request edits a file under
  `.github/workflows/`: the action skips it, and GitHub shows the skip as a pass
  within seconds. Expected; the tests are the only gate for that pull request.
- **A final message about waiting for background agents, after about 20
  seconds.** The workflow lost `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS`: the review
  started its agents in the background, and the run ended before they reported.
- **A final message that reports findings, and still no comment.** The workflow
  lost `--comment`, or the inline-comment tool in `--allowedTools`.

`tests/test_review_workflow.py` fails if the workflow loses any of the three.
Refused tool calls are normal — the first complete review listed 19 — so read the
final message, not the count.

**No review ran on your latest push?** Expected: the review runs once, when the
pull request is opened. Close and reopen the pull request to run it again.

More in [Architecture](architecture.md#the-automated-review-on-a-fork).
