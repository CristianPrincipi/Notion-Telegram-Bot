# CLAUDE.md

## What this is

**David** — a single-user personal Telegram bot that writes to Notion, reads Google
Calendar, and summarises content with the Anthropic API. Runs as a polling worker on Railway
(`Procfile: worker: python david.py`). Single-user is a design constraint: every command
spends the owner's Notion/Anthropic quota, so `OWNER_ID` gates everything through a PTB
filter — an unauthorized update is dropped by the dispatcher and never reaches handler code.

## The layers

```
bot/        Telegram adapters — parse the update, call a service, send what comes back
services/   the work itself — no telegram, no update parameter, reports via callbacks
clients/    the wire: Notion, Google Calendar, Anthropic, Telegram file download
```

Arrows point one way: **bot → services → clients**. A service that imports a handler
is the same tangle `david.py` used to be, spread over more directories, so the
direction is asserted rather than assumed (`tests/test_layering.py`).

**THE RULE, and it is enforced by a test, not by discipline:**

> Nothing under `services/` may import `telegram`, and no service function may take
> `update` as a parameter.

`tests/test_layering.py` walks the ast of every file under `services/` and fails on an
`import telegram` (including `from telegram.ext …`, and one hidden inside a function),
a parameter named `update`, a direct `reply_text`/`send_message` on any object, an
import of `telegram_text.reply`/`send`, or an import pointing back up the stack. It
carries its own offender rows and a positive control against `david.py`, so it cannot
pass by scanning nothing.

**How progress gets out.** A service reports by calling back:

```python
async def run_something(..., *, notify, notify_md=None) -> None
```

`notify(text)` is plain and the only channel a caller must supply — `bot/` passes
`update.message.reply_text`, a test passes a list's `append`, a job passes something
that logs. `notify_md(text)` is the Markdown channel and **defaults to `notify`**. Two
channels because David already had two and the difference is load-bearing: David's own
`*bold*` goes out with `parse_mode` and every interpolated value escaped, while a raw
Notion error or a slice of an uploaded PDF goes out plain precisely because escaping
cannot save it inside a `code span`. `bot/notify.py` is the only place they are bound.

## Module map

| File | Owns | Must NOT own |
| --- | --- | --- |
| `david.py` | Entry point (`__main__`), the `COMMANDS` registry + its dispatch loop, the generated help (and `cmd_help`, which renders it), the owner filter and handler registration, job registration, `on_error` / `notify_error` | Any command's work, Notion, argument parsing beyond the patterns |
| `config.py` | Constants, schedule times, timeouts, shortcut maps, weekday constants, the unverified-source marker + `is_unverified_source` and `TAKEAWAYS_HEADING` (two layers need each, neither owns it), the env contract (`REQUIRED_ENV`/`OPTIONAL_ENV`) and `validate()` | Reading feature IDs — each module reads its own `os.environ` |
| `bot/notify.py` | `for_update(update) -> (notify, notify_md)` — the **only** place a service's callbacks are bound to a message | Anything a service could decide |
| `bot/tasks.py` | `run_detached` — the per-command decision to background a long one | Which commands are long (that is the registry) |
| `bot/expenses.py` | `Add e` / `U e` / `D e` / `undo` / a bare number: the `AMOUNT` grammar, `parse_amount`, `resolve_category` | Which row a command means, the lock, the undo |
| `bot/books.py` | `Add b` and `Add q` in their typed form | Notion, PyPDF2 |
| `bot/learn.py`, `bot/implement.py` | The `update`-taking wrappers, and (for Learn) the PDF upload, which needs a `context.bot` | Extraction, merging, routing |
| `bot/documents.py` | `handle_document` — the caption router for uploads | The work either caption triggers |
| `bot/commands.py` | The one-line delegators for commands whose feature module still takes `update` itself: `B`, `Month`, `Diag`, `DBs`, `Find`, `Get`, `Remind` | Anything more than the forward — see the open question |
| `services/expenses.py` | The expense writes, `find_expense_matches`, the `EXPENSES_ID` lock, and the find-choose-write cycle | Telegram, argument parsing |
| `services/books.py` | Book + quote writes, `extract_quote_from_pdf`, the quote-from-PDF flow (its download is INJECTED) | Fetching from Telegram |
| `services/learn.py` | `Learn [type] [source]` — extract, Claude-summarise, write to Notion. Owns the trafilatura→BS4 parser ladder, the one place source text is cut to fit, and URL identity (`normalise_source_url`, the `Source URL` property, the duplicate check and its ` !` override) | Manual merging; the unverified marker's TEXT (that is `config.py`) |
| `services/implement.py` | `Implement [Page] - [Area]` — index a Manual by heading, route, merge and rewrite **only** the affected sections. Owns `get_area_db_id`, the `📚 Sources` ledger (`record_source`, and the two guards that keep it unwritable by a merge) and the additions-only rule for unverified sources | Diet (delegates to `services/implement_diet.py`); the marker's TEXT (that is `config.py`) |
| `services/implement_diet.py` | The Diet page's H1>H2>H3 toggle tree: skeleton, breadth-first read, surgical updates | Generic Manual merging |
| `clients/notion_client.py` | The **only** place that speaks HTTP to Notion: headers, per-thread `Session`, retry/backoff, pagination, block builders | Any feature logic |
| `clients/anthropic_client.py` | The **only** place that speaks to Anthropic: `complete_json`, retry, `stop_reason` checks, token logging, the daily spend guard | Prompts — each feature owns its own system prompt and schema |
| `clients/calendar_client.py` | The **only** place that speaks to Google Calendar; per-thread service. `now_local()` is the project clock — never `datetime.now()` | Telegram, Notion |
| `clients/telegram_files.py` | Attachment validation and the bounded PDF download | What the bytes are for |
| `page_lock.py` | Per-database asyncio locks (`page_lock`, `PageBusy`) | Anything else |
| `telegram_text.py` | `escape_md`, and the **only** safe senders (`reply`, `send`) — the sole place `parse_mode` reaches Telegram | Feature logic, message wording |
| `observability.py` | `setup_logging`, the correlation-ID contextvar, the heartbeat counters | Telegram, Notion, any probe |
| `expense_safety.py` | The guards on `U e` / `D e`: the pending-choice state machine in `user_data`, its 2-minute expiry, the undo record, and every message either prints | Notion calls, Telegram sends — it decides and formats, `services/expenses.py` acts |
| `month.py` | Which page this month's expenses relate to: naming, find-or-create, cache, `Month` handler | Expense writes, budget maths |
| `budget.py` | Expense aggregation + recap text (`compute_budget`, `format_budget`, `budget` — the two fallible ones return `(value, error)`) | Notion HTTP, Telegram |
| `pkm.py` | `Get [Topic] - [Area]` — read a section back out of a Manual: index, fuzzy resolve, discovery. Read-only, no Claude call | Writing anything; knowing how Manuals are built |
| `reminder.py` | `Remind …` — the command pattern (which tokens a date and a time may be), conflict-check, create the calendar event | Calendar HTTP (that is `clients/calendar_client.py`), and what a token MEANS — `td` becoming a date, and `t` becoming a refusal, are the client's job |
| `notion_ids.py` | `Diag` / `Find` / `DBs` — read-only ID + schema diagnostics | Any write |
| `proactive/` | Scheduled push messages. One builder module per feature; `scheduler.py` does all JobQueue wiring and sending. Never imports `david.py` | Sending from a builder — builders return `(text, error)` |
| `proactive/heartbeat.py` | `build_heartbeat` — the weekly liveness proof; runs the Calendar/Notion/month probes | Sending (that is `scheduler.py`) |
| `proactive/learn_nudge.py` | `build_nudge` — the weekly list of Learn pages never merged into a Manual. Owns what "pending" means (one Notion filter) and the `Implemented` property name | Sending; un-ticking the checkbox (nothing does) |
| `proactive/takeaway.py` | `build_takeaway` — one takeaway bullet resurfaced weekly. Owns finding the takeaways section in a page (`takeaways_in`) and the bounded skip-and-retry over pages that have none | Sending; the heading's TEXT (that is `config.TAKEAWAYS_HEADING`) |

`month.py`, `budget.py`, `pkm.py`, `reminder.py` and `notion_ids.py` are still at the
root and four of them still take `update` — they were out of scope for the layering
work and are named as follow-ups under Open questions. Everything new goes in a layer.

New features get a module. `david.py` routes to them; it does not absorb them.

## Conventions

**Fallible functions return `(value, error)`, never a bare `None` and never a raised
exception across a module boundary.** A lone `None` conflates "this failed" with "there is
nothing here", and the two need opposite handling. That is not hypothetical: `read_diet_tree`
used to discard errors below the top level (`h2_blocks, _ = get_children(...)`), so a
transient read failure made a section look **empty** — which is exactly what makes Claude
decide to populate it, and `apply_updates` then replaced real content with content merged
against nothing. Nothing errored. See `services/implement_diet.py`'s `_children_of_many`.

**An error is never the same value as an empty result.** The corollary of the rule
above, and the one that cost the most. `proactive/briefing.py` used to write
`if err or not events: return None`, so a revoked calendar share and a free evening
produced identical output — the reminders stopped and nothing said so. The morning
half was worse: on an error it set `events = []`, and an empty list renders as
"nothing scheduled", so during an outage David stated the day was clear. A missing
message you eventually notice; a confident wrong answer you act on. Every proactive
builder now returns `(text, error)`, and `scheduler._run_job` is the single place
those three states — send / stay silent / report — are told apart.

**Everything sent with Markdown goes through `telegram_text`.** Legacy Markdown has
no literal asterisk, so one stray `*` in a Notion category, a Claude-written
heading, or a slice of an uploaded PDF made Telegram reject the whole message —
after the Notion write had already succeeded, so it looked like David ignoring you.
`escape_md` at the interpolation site (a sender cannot tell David's own `*bold*`
from data), plus a retry without `parse_mode` on `BadRequest` for whatever escape
gets forgotten. Enforced by an ast-based test.

**The three error reporters are exempt from that, deliberately.**
`david.notify_error`, `david.on_error` and `proactive.scheduler._report_error` send
plain text unconditionally. They interpolate an exception into a `code span`, where
Markdown v1 ignores backslash escapes — escaping cannot save them, so the reporters
used to fail on exactly the ugly Notion errors they existed to report. Do not
"fix" them back into Markdown; each carries a comment saying so.

**A budget belongs to whatever consumes it, and there is one of it.** The article
extractor returned `text[:12000]`; the summariser accepted `text[:100000]`. Two
independent numbers describing one budget, so every article over ~12k chars was
summarised from its first eighth — silently, and indistinguishably from a full
summary in both the reply and the Notion page. You find out from a thin Manual
entry months later, with no way to tell which entries were affected.

`config.SUMMARY_INPUT_CHARS` is now the only number, and `run_learn` is the only
place text is cut — **for every content type**, since a 3-hour transcript and a
400-page PDF reach the same cap an article does. Two consequences worth keeping:

- **Extractors must return the text WHOLE.** An extractor that pre-truncated to
  the budget would leave the truncation undetectable at its own boundary — a
  source of exactly the budget and one cut down to it are the same string.
- **It says so in the reply.** A partial summary you are told about is a
  different object from one you discover later. `summarize_with_claude` keeps the
  same cap as a backstop, and that copy is deliberately NOT the primary: if it
  ever fires, it fires silently.

**Implement had the same bug, five times over, and it is fixed the same way.**
`route_sections`, `merge_sections` and `build_manual` each sliced
`source_text[:60000]`, and the two Diet builders sliced `summary_text[:50000]` —
five anonymous literals, three numbers, no relation to any constant and nothing
in the reply. So a long Learn page merged into a Manual from its first 60k
characters and the run reported a clean success.

`config.MANUAL_SOURCE_CHARS` and `config.DIET_SUMMARY_CHARS` are now the only
numbers, `services.implement.fit_to_budget` is the only implementation of the cut
(the Diet path imports it rather than copying it, so there is one wording of the
warning), and `run_implement` / `run_implement_diet` are the only places it is
applied. Three things to keep:

- **The prompt builders must not re-slice.** Same reason extractors must not, and
  with a nastier consequence: a builder capping at the number the caller already
  cut to is a **no-op that no end-to-end test can see**. That was verified by
  putting `[:50000]` back into the Diet merge, watching the entire file stay
  green, and writing the test that catches it —
  `test_the_prompt_builders_do_not_cut_again`, in both test files, which drives
  the builders with text the caller never cut. It is the only test that can see a
  second cap; the boundary tests cannot, and their docstrings say so.
- **The SECTION half of those prompts is deliberately uncapped**, and clipping it
  would be worse than the bug this fixed. Hard Rule 3 sends a section whole or
  not at all, and the merge's reply *replaces* the section — so a section clipped
  on the way in comes back short on the way out, and the tail is deleted from
  Notion. A ceiling there would have to refuse the run, not clip the input.
- **The cut runs before `is_unverified_source`**, so the predicate reads exactly
  the string the model gets. That is safe in one direction only: the marker is
  the FIRST block Learn writes, so a head-slice always keeps it. Asserted in both
  test files, because it is a property of two modules agreeing — were the marker
  ever moved down the page, truncation would silently disarm the additions-only
  rule on precisely the long recollections that need it.

**A partial write is a third outcome, not a failure.** Notion caps an append at
100 blocks and has no transaction across the batches, so batch 3 of 5 failing
leaves batches 1 and 2 on the page — permanently, since deleting them would be a
second write that can fail the same way. Every caller used to report that as a
flat failure: `add_Quote` returned a bare `False` while two fifths of the quote
sat in the book, and `create_page` handed back the new page's id *together with*
the append error, which `if not page_id` cannot see — so a Learn page holding its
first 100 blocks came back as `✅ Saved to Notion!`.

The reason it matters is what you do next. Told it failed, you re-run, and the
part that already landed is appended a second time. So `append_children` returns
`Written` — a **list subclass**, because every caller already reads that value as
the blocks that were created, and a NamedTuple would have broken each of them at a
different call site — carrying `batches_done` / `batches_total`, and every caller
prints the tally plus the warning that a re-run duplicates. `implement`'s
`apply_section_updates` gained a third bucket for the same reason: a half-written
section is neither applied nor "unchanged", and it was being filed under the
latter, next to a heading that said so.

**A source with no source says so, in the page body.** `Learn book X` summarises
from Claude's recollection — nothing is fetched, nothing is read — and the result
was filed next to pages built from a real transcript with nothing to tell them
apart. It then flows into a Manual through `Implement`, where even the fact that
it came from a `Learn book` command is gone.

`config.UNVERIFIED_MARKER` is stamped as a red callout at the top of those pages,
and it is **a sentence in the body rather than a property** for three reasons that
are all the same reason: it is visible in Notion without opening a panel, it
survives `blocks_to_text`, and therefore it is already inside the text `Implement`
sends to the merge call. Detecting it costs no extra read. One predicate
(`config.is_unverified_source`) serves the writer and the reader, because two `in`
checks in two modules is how a marker gets reworded in one of them and silently
stops being detected in the other.

**The Manual keeps a ledger, and it is the one section a merge cannot touch.**
`📚 Sources` used to be written on the first run and never again, so a Manual
recorded nothing about what had been merged into it since. It is now appended to
on every run — title, whether the source was unverified, and the date — and it is
the only place an unverified source's provenance survives, because a marker inside
a section does NOT: the merge returns a section's full content, so anything inline
goes back through the model next run and is reworded or dropped at its discretion.

Kept unwritable by **two independent guards**, and they are independent because
each one alone is undoable by a change that looks reasonable on its own:
`routable_sections` keeps it out of what the router is SHOWN (an inline
`[s.path for s in sections]` at the call site puts it back), and
`apply_section_updates` refuses a Sources path however it was arrived at (a model
can name a section it was never offered). Both are asserted in
`tests/test_implement_sections.py`.

**An unverified source may add to a Manual; it may not rewrite one.**
`_hold_back_rewrites` compares the merge's output against the section's existing
lines and refuses the whole section unless every one of them survives verbatim
(whitespace-normalised, order significant — a numbered routine with two steps
swapped has been changed). A held-back section is a fourth outcome in the reply,
next to applied / skipped / partial, because "David refused" and "Notion failed"
need different next steps.

Be precise about what this buys, in the code and in any PR describing it:

- **Enforced:** no line already in the Manual is deleted or reworded on the
  authority of a recollection. Deterministic — David holds both sides before it
  writes.
- **Prompt-only:** `_UNVERIFIED_MERGE_RULE` asks the model to reproduce existing
  lines exactly. It is what makes the check pass often enough to be usable; it is
  not what makes it true.
- **Not checked by anything:** whether an ADDED line is true. That is the ledger's
  job, and the ledger is a trail, not a verdict.

**Learn asks whether it has seen this URL before, and asks Notion, not itself.**
The same link twice — which is what you send after a timeout — made a second page
and paid for a second summarisation, and the two near-identical titles are exactly
what `search_page_in_db`'s `contains` filter cannot choose between afterwards. The
check runs **before the fetch and before Claude**, so a duplicate costs one query.

Two properties of it are load-bearing:

- **Normalisation answers one question** — would these two strings have fetched
  the same bytes. So scheme, host case, `www.`, default ports, `utm_*` and the
  named trackers, the fragment and a trailing slash fold away, while the remaining
  query is **sorted rather than dropped**: `?v=abc` and `?v=def` are two videos.
  Anything a site might route on (`page`, `id`, YouTube's `t`) stays out of the
  tracking list, because collapsing two real sources into one is the worse error.
- **It degrades rather than blocks, out loud.** The `Source URL` property is not
  created by David and may not exist; absent, the check *and* the write are
  skipped and said, because writing a property Notion does not know is a 400 on
  the page CREATE — which would turn "you have not added the column" into "Learn
  does not work". A check that ERRORS is reported and the run continues, naming
  the duplicate it could not rule out. Refusing the command is the worse failure
  when the safeguard is optional; that asymmetry is the opposite of
  `title_property`'s, and it is deliberate — there, guessing produces a *wrong
  answer*, here, refusing costs you the command.

**Notion is asked what its columns are called.** `search_page_in_db` filtered on a
hard-coded `{"property": "Name"}`. Notion answers a 400 when the title column is
named anything else, and that 400 came back as `"No page found matching 'X'"` — an
error about your data, for a bug in this line, while `get_page_title` twenty lines
away had always resolved the title property properly. `notion_client.title_property`
now discovers it, cached per database.

That cache is **populated only on success and read before any network call**, which
is the whole reason refusing is safe: a database read once keeps working through a
later outage, and only one never read successfully fails. On failure the lookup is
REFUSED and names the schema read — never widened back to `"Name"`, because guessing
would restore exactly the misleading error it removes, *intermittently*, which is
harder to diagnose than a bug that happens every time.

**Notion database IDs are `{AREA}_ID` in the environment.** `get_area_db_id` derives the
name (`"Brain"` → `BRAIN_ID`), so adding an area means adding an env var, not code.

**Article extraction is a ladder, and the rungs are declared dependencies.**
trafilatura scores the DOM for content density, so navigation, cookie banners and
footers do not reach the summariser; BS4's `get_text()` cannot do that, because to
it every string in the document is equally text. BS4 remains the fallback, and
`MIN_ARTICLE_CHARS` guards the case `is None` cannot: trafilatura's failure mode on
an odd layout is a *fragment* — a caption, a standfirst — that looks like success.

`import trafilatura` is at module scope and **unguarded, on purpose**. This path
used to open with `from newspaper import Article` inside a `try/except ImportError`
while newspaper was not in `requirements.txt`, so the import failed on every call,
the bare except swallowed it, and BS4 did the work every time — a branch documenting
extraction quality nothing was delivering. A guarded import converts a build failure
(loud, at deploy, fixable) into a permanent silent downgrade. Which parser won is
logged for the same reason.

lxml was the original objection and no longer is: it ships manylinux wheels. Before
touching either pin, re-run the check in `requirements.txt`'s comment —
`--only-binary=:all:` against `manylinux_2_17_x86_64` fails if anything in the tree
needs a compiler, which is the Railway build failing on your laptop instead of on
Railway.

**All secrets come from environment variables.** `config.validate()` runs first in
`__main__` and raises `SystemExit` listing *every* problem at once — a misconfigured deploy
costs one fix, not one redeploy per variable. Add a var to `REQUIRED_ENV`/`OPTIONAL_ENV`
and the README table when you introduce one.

**One model name, one client.** `config.ANTHROPIC_MODEL` is the only place the model
is named, and `clients.anthropic_client.complete_json` is the only way to reach the API. A
feature module owns its system prompt and its JSON Schema; it does not own retry,
truncation handling, or token accounting.

**Async discipline.** Every synchronous call — `requests`, Notion, Anthropic, PyPDF2,
Google Calendar — runs via `asyncio.to_thread`. PTB runs updates on one event loop, so a
blocking call in an `async def` freezes the whole bot: a 300-second Anthropic read took
David down for five minutes. Keep those functions **synchronous** so they stay directly
testable; the handler does the offloading. Long *reads* also get an `asyncio.wait_for` cap
from `config.py` — **writes do not**, because `wait_for` cancels the awaiting coroutine but
cannot cancel the worker thread, so timing out a write reports failure while it is still in
flight. Notion writes are bounded by `notion_request`'s timeout and bounded retries instead.

**`Remind` never guesses which moment you meant.** The command takes a date and a
time, and the accepted forms are:

| Date | |
| --- | --- |
| `12.06` | that day this year, subject to the rollover rule below |
| `12.06.2027` | that year, taken at face value — past or not |
| `td` / `today` | today, provided the time has not already passed |
| `tr` / `tomorrow` | the day after today |

| Time | |
| --- | --- |
| `14.30` | 24-hour |
| `10` | a bare hour, meaning o'clock |

The ` - ` between them is optional, so `Remind Dentist tr 10` and
`Remind Dentist 12.06 - 14.30` are both whole commands. `reminder.REMIND_PATTERN`
decides which TOKENS are legal; `clients.calendar_client.parse_date_time` decides what they
MEAN. Keep that split — a shorthand resolved in the regex is a date rule nothing
can unit-test.

**The day shorthands are a matched PAIR, and they are two letters on purpose.**
There used to be one, `t`, and it meant tomorrow: a single letter for one of two
adjacent days, with no room for today beside it and nothing in the letter to say
which of the two it had picked. `td` and `tr` cost one keystroke and remove the
class. Neither is a prefix of the other, and the long forms stay because a
shorthand nobody remembers is worse than none.

Six things are refused rather than resolved, and each one is a bug that already
shipped or nearly did:

- **A bare `DD.MM` inside the last 24 hours** (`PAST_GRACE`). The old rule rolled
  ANY past datetime to next year, so at 10:00 a reminder for 09:00 today was
  booked for next August and confirmed as though it were fine. Beyond a day past,
  `12.06` in December still obviously means next June, so that still rolls.
- **`td` naming a time already gone.** The one thing the pair can do that `t`
  could not — tomorrow is a future day by construction. A past event pings
  nothing: Google's alerts are 1 day and 1 hour before, both gone, and the morning
  poll ran at 07:30, so booking it is a silent no-op confirmed as "Reminder set!".
  `PAST_GRACE` deliberately does NOT reach this path — it exists because a bare
  `DD.MM` is ambiguous between this year and next, and `td` answers that outright.
  The refusal offers both readings (`tr` for tomorrow, or the date spelled out
  with its year to record something that happened) rather than picking one.
- **A bare `t`.** It meant tomorrow and now names neither day, so it is refused BY
  NAME rather than dropped from the grammar — dropping it gives the generic usage
  message, which does not say what changed. What it must never do is keep working:
  `t` sits one letter from both replacements, so any silent reading is a coin flip
  between two adjacent days. The refusal names BOTH, because naming only `tr`
  ("it used to mean tomorrow") nudges toward one of them at the exact moment David
  has no idea which was meant.
- **Local times that are not one real instant.** `localize()` defaults to
  `is_dst=False`, which silently shifted the hour skipped at the start of summer
  time and silently picked one of the two at the end. `is_dst=None` raises, and
  both are reported with the hour to avoid. On the spring-forward day `td 02.30`
  is both nonexistent and past; the DST check runs FIRST, because that message
  says the hour cannot be booked on any day while "already past" would send you to
  try the same time tomorrow, where it exists.
- **A shorthand running into the next token.** It otherwise matches the leading
  letters of any word starting the same way and the rest becomes the TIME:
  `Remind Bus td4 to town 10` parsed as "Bus", today, 04:00 — wrong name AND wrong
  time. A lookahead requires the token to end where the word does, which costs
  `td10` and is worth it.
- **A run-together time.** `1030` would match its first two digits and book 10:00.

Every one of those guards was verified by REMOVING it and watching a named test go
red. A lookahead that guards nothing is worse than none, because it reads like
protection.

**`REMIND_PATTERN`'s alternation order is legibility, not protection, and it says
so in the file.** It lists the tokens longest-first, which reads like the thing
keeping `t` from claiming the head of `today` — and it is not. Reversing it leaves
every token resolving to the same day, because the lookahead after the date group
makes the order irrelevant. It was written as a guard, failed the revert check,
and is now labelled. Do not re-promote it to one.

The confirmation is the backstop for all of it: it names the weekday and the full
date (`Friday 07 August 2026 at 10:00`), plus a line of its own when the year is
not the current one. A terse shorthand is safe to type precisely because the reply
is not terse.

## Hard rules

### 1. Notion is the single source of truth

No local mirror, no local database, no cached copy treated as authoritative. Railway's
filesystem is ephemeral — anything written locally dies with the container. Caches for
performance are fine *if deleting them is harmless*, and the safest place for one is
**memory**: `month.py` holds the resolved page for the life of the process and a fresh
container just asks Notion again, at a cost of two API calls.

`month.py` used to persist that cache to `.month_state.json`, and removing it is
instructive. The saving was negligible on a platform that deletes the file every deploy —
but the file had quietly become load-bearing, because while it existed the fallback path
behind it was never reached, and that path handed back a stale `MONTH_ID` **without asking
Notion at all**. A cache whose absence changes behaviour is not a cache. If you add one,
the no-cache path is the one to get right first.

### 2. Never delete before the replacement is committed

Notion has no transactions, so **ordering is the only safety mechanism available**. The
pattern, in this order, every time:

1. snapshot the existing block IDs
2. append the new content
3. on error, **return early** — nothing has been deleted, the old content is intact
4. only on success, delete the snapshotted IDs — from the *snapshot*, never from a re-read
   after appending, which would delete the new content too

Reference implementations: `services/implement.py`'s `apply_section_updates` (Manual
sections) and `services/implement_diet.py`'s `apply_updates` (Diet sections). Clear-then-append meant a 502 or a Railway
restart between the two left the page **permanently empty**, with no transaction to roll
back and no second copy anywhere. The page briefly showing old-then-new content is the
accepted cost of never showing neither. Locked down by `tests/test_safe_writes.py`.

### 3. Never send a section the source did not touch

Everything sent to the model can come back reworded. Implement used to send
the **whole** Manual and rebuild the page from the reply, so untouched sections drifted
a paraphrase at a time, and a Manual over 40k characters had its tail silently dropped
from the prompt (`manual_text[:40000]`) and therefore from the rebuilt page.

So both Implement paths are sectioned: index by heading, a cheap routing call over
section NAMES, a merge over only the affected sections, and a write back to only those.
**The only guarantee that a section is unchanged is that it was never sent.** Locked
down by `tests/test_implement_sections.py`.

### 4. A destructive command never guesses which row it meant

`U e` and `D e` are find-then-mutate over a `contains` filter, and they used to act
on `results[0]`. **Notion documents no ordering for query results**, so with two
Coffees on the page the row that was archived was arbitrary — and the reply said
"deleted successfully" either way. No exception, a 200 from Notion, a confident
confirmation, and a missing row discoverable only by opening Notion.

Three independent guards, and they are independent on purpose — each one alone
still leaves a way to hit the wrong row:

1. **`sorts=CREATED_DESC` on every lookup that reads `results[0]`**
   (`clients/notion_client.py`'s `CREATED_DESC`). This does not make "first" *correct*, it makes
   it *defined* — the same row on two identical calls. Applies to
   `find_expense_matches`, `find_Book_Page` and `search_page_in_db`.
2. **Expense lookups are scoped to the current month.** If the month cannot be
   resolved the lookup is REFUSED, never widened: falling back to an unscoped
   search restores exactly the reach the filter removes, at the moment David is
   least sure of its own state.
3. **More than one match writes nothing** and asks, via `expense_safety`. The
   pending list lives in `context.user_data` and expires after 2 minutes, so a
   stray `2` cannot answer a prompt from an hour ago.

Then **every destructive write records its own reversal** before reporting success,
and `undo` applies it. The update snapshot must come from the page object the
LOOKUP returned — re-reading after the PATCH records the new amount as the old one,
producing an undo that changes nothing and says it worked.

The lookup runs **inside** the expense lock, not just the write. Splitting
find-from-mutate into two functions made it possible to lock only the second half,
which reads as safe — no two writes overlap — while leaving both queries free to
resolve to the same row. Locked down by `tests/test_expense_safety.py` and
`tests/test_concurrency.py::test_no_two_expense_cycles_are_ever_in_flight_together`.

### 5. Concurrency is deliberately narrow

`concurrent_updates` is **off**. Do not enable it; `tests/test_async_io.py` fails if you do.
Responsiveness is bought per-command instead: only the long commands (`Learn`, `Implement`,
both PDF uploads) go through `bot.tasks.run_detached`. Everything else runs inline, strictly
ordered — which is what makes `Add e Carrefour 5` followed by `B` always report the new
total. **Locks do not give that back**: they stop two cycles interleaving, they do not decide
which runs first. That is why the decision is per-command and not a global switch.

Before widening concurrency, every find-then-mutate cycle must be locked *and verified by a
test that drives two real handlers concurrently* (`tests/test_concurrency.py`). Current
locks — always keyed on a **database** ID, never a page ID, because a page ID is not known
until the lookup the lock has to cover:

| Cycle | Key | On contention |
| --- | --- | --- |
| Implement → area Manual | `area_db_id` | refused |
| Implement → Diet page | `DIET_ID` | refused |
| `U e` / `D e` (lookup **and** write) | `EXPENSES_ID` | queues |
| A number answering an ambiguous `U e` / `D e` | `EXPENSES_ID` | queues |
| `Remind` | `CALENDAR_ID` | queues |

The ambiguous path releases the lock while it waits for your number — holding it
across a reply would stall every expense write for as long as you took to answer,
and the selection writes by page ID, so it has no lookup left to protect.

`month.py` uses a `threading.RLock`, not `page_lock`: its cycle is reached from worker
threads, and an `asyncio.Lock` between two threads acquires without ever blocking.

## Testing

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

Fully offline: `tests/conftest.py` installs a fake environment at import time (before any
project module loads — several read `os.environ` at module scope) and `responses` intercepts
HTTP. Nothing reaches Notion, Telegram or Google.

`tests/test_router.py` is the pre-deploy gate: a table of `input → handler → parsed args`
driving the **real** `handle_message`. Which command wins depends on the order of
`david.COMMANDS` and on `fullmatch` vs `match` — invisible when reading one entry, and
exactly what breaks when a command is added in the wrong place. Adding a command means
adding a `Command`, a `SPY_TARGETS` entry, and rows; the registry tests fail until the
table covers it. Rows marked `known_bug` assert current *wrong* behaviour on purpose;
fixing one turns its row red, and the row is updated in the same commit.

`SPY_HOMES` next to that table is not decoration: a spy only works if it is installed on
the module whose namespace the caller resolves the name through AT CALL TIME. A stub on
`david.add_Expenses` stopped doing anything the moment the write moved to
`services/expenses.py`, and would have left the test passing against nothing had the
attribute still existed. When you move a function, move its spy.

**Two tests drive a service without a bot at all**, which is the point of the split:
`conftest.with_update(update)` builds the `notify` pair by calling `bot.notify.for_update`
itself, so a test cannot pass against a binding production does not use — and a test that
does not need an update passes a list's `append` instead.

**A command declares itself once.** Pattern, handler, help entry and the `destructive`
flag live on one `Command`; the help message is GENERATED from that list. The
hand-written help had already drifted — it advertised `Learn recipe`, which
`run_learn` rejects as an unknown type, omitted `Learn podcast`, which works, and
never mentioned that `Implement … - Diet` merges into a toggle tree instead of a flat
Manual. Nothing catches that class of error, because nothing runs the help.

Registry order is still precedence — dispatch takes the first pattern that fullmatches —
but it is arranged for reading, since every pattern is anchored on a distinct literal
prefix and no input can satisfy two. That is asserted, not assumed
(`test_no_input_can_be_claimed_by_two_commands`): a new command that overlaps an existing
one turns it red and has to be positioned deliberately.

`tests/test_layering.py` is the architectural gate, in the same family as
`test_telegram_text`'s parse_mode walk, `test_data_integrity`'s weekday scan and
`test_concurrency`'s lock-key scan. All four read the SOURCE, because no runtime
assertion can tell a database id from a page id, or a service from a handler. All four
carry a can-this-guard-actually-fail test, because a guard that cannot go red reads like
protection and is not.

**The source scans walk `bot/`, `clients/`, `services/` and `proactive/`, not just the
repo root.** They used to stop at the root, which meant a file that moved into a package
dropped silently out of the guard while it kept passing green. If you add a package, add
it to those lists.

**A process-lifetime cache needs an autouse fixture that clears it.**
`notion_client._title_props` lives for the life of the process, which is right in
production and poisonous in a suite: whichever test ran first would satisfy every
later test's schema lookup, so a test asserting the schema IS fetched passes
without it being fetched, and the refusal tests silently depend on file order.
`test_notion_client.forget_title_properties` clears it on the way in *and* out.
`_db_schemas` is the second one, with `test_learn_idempotency.forget_database_schemas`
doing the same job — two caches because merging them would rewrite the most
carefully-reasoned function in `notion_client` to save one GET per process.

**A test double must return the type production returns.** `append_children`
hands back `Written`, and a double returning a bare list looks correct and drops
the batch tally — so the caller under test can no longer tell a write that landed
nothing from one that landed half, and the assertions pass either way.
`conftest.written_ok` / `written_nothing` / `written_half` build the real type;
`test_partial_writes` covers each one against the real function too, so the
doubles cannot drift into describing a shape nothing produces.

**A test that only exercises the shape YOUR machine produces guards half a bug.**
`<title>Post <em>name</em></title>` reaches `_title_from_html` as a tag with
children on CPython 3.12.3 and as one RCDATA string still containing
`<em>name</em>` on 3.12.13 — same beautifulsoup4, different `html.parser`. The
first raises `AttributeError`; the second raises nothing and puts raw markup in
a Notion page title. A fixture parsed from markup tests whichever shape the
runner produces and silently leaves the other unguarded, which is exactly how
that reached CI green from a laptop. `test_the_rcdata_title_shape_is_covered_on_every_machine`
builds the other shape by hand.

**Fixtures that build bulk text must not repeat themselves.** The first version of
`test_article_extraction.paragraphs` made a long body by repeating one paragraph
twenty times, and trafilatura — which drops duplicate blocks — extracted 54
characters from it. The "no truncation" assertions failed for a reason that had
nothing to do with truncation, against correct code. Each paragraph is indexed now.

**Every bug fix ships with a test that fails before it and passes after**, asserting against
the shipping code path — a test that rebuilds the logic only proves it agrees with itself.
The six guards in `article-extraction` were each verified by reverting them and watching a
named test go red: `.string.strip()`, the 12k extraction cap, the truncation warning, the
collapsed `except`, the hard-coded `"Name"`, and caching a failed schema read.

## Deployment

Pushing to `main` triggers a Railway deploy. **Railway keeps the previous version running
when a deploy fails, so a broken deploy is silent** — you will not notice until a command
misbehaves. CI on the PR (`ruff check .` + `pytest`) is the real gate. Work on a branch, open
a PR, let CI go green, then merge. Never commit directly to `main`.

## Open questions

Found in the code, not resolved here — do not "fix" these by guessing intent:

- **Unreferenced code:** `DATABASE_ID` in `david.py` — still the only one left, and still
  unexplained, so it stays. (`implement.clear_page_blocks` was on this list and no longer
  exists; only `clear_page_blocks_by_id` does, and it is live. `reminder.build_today_message` /
  `build_tomorrow_message` were here too and have been deleted — they had zero
  callers and carried a copy of the error/empty collapse that made them look like
  the bug's home. The live copy was in `briefing.py`. `david.headers` — a second
  Notion header dict, superseded by `clients/notion_client.py` and read by nothing — has been
  deleted along with the `NOTION_KEY` that existed only to build it.)
- **What an unverified source may do to a Manual is now bounded, and the bound is
  narrower than it looks.** `_hold_back_rewrites` refuses to write a section unless
  every existing line survives verbatim, so a recollection can ADD but cannot
  delete or reword. That is deterministic and it is the only part that is
  enforced. Nothing checks whether an added line is TRUE — nothing can — which is
  what the Sources ledger is for. `_UNVERIFIED_MERGE_RULE` in the prompt is a hint
  that helps the model comply, never the guarantee; do not describe it as one.
  How often the rule holds a section back is still unmeasured: `_MERGE_SYSTEM`
  asks for "the FULL merged content" and models reword while reproducing.
- **The force gate on `Implement` was considered and declined.** A prompt-level
  refusal (`Implement … - Brain !`) was the obvious symmetry with Learn's token
  and was rejected deliberately: a gate that fires on every book page is a gate
  you stop reading. The plan message is the checkpoint.
- **`Learn book` is not de-duplicated.** It has no URL. The equivalent would be a
  title match against `LETTI_ID`, which is a fuzzy-match decision of its own.
- ~~**The Learn-nudge job does not exist.** Both Implement paths tick an `Implemented`
  checkbox described as feeding it; the checkbox is written and never read.~~
  Resolved: `proactive/learn_nudge.py` reads it weekly (Saturday 10:00) and names the
  pages you saved over `LEARN_NUDGE_STALE_DAYS` ago and never merged. Step 5
  (takeaway of the week) and Step 7 (tasks) are still unbuilt. Two things about it
  are worth keeping straight:
  - **"Pending" is decided by Notion, in one filter**, not fetched and sieved in
    Python. So the boundary is Notion's `before`, which is strict — a page exactly
    N days old is not yet named — and no test can assert that from the builder's
    output. `test_the_staleness_cutoff_is_now_minus_the_threshold` asserts the
    REQUEST instead, which is where the behaviour lives.
  - **A missing `Implemented` column refuses**, which is the OPPOSITE of the
    `Source URL` asymmetry two paragraphs down, and deliberately so. There the
    safeguard is optional and refusing costs you the command; here the checkbox
    is the whole feature, so there is nothing to degrade to — listing everything
    is noise and listing nothing is indistinguishable from "you are caught up".
- ~~**`MONTH_ID` is in `REQUIRED_ENV` but `month.py` treats it as an optional seed.**~~
  Resolved: it is in `OPTIONAL_ENV` now, which is what the module reading it always
  meant. Unset, the first run resolves the month from Notion by title.
- **`(value, error)` is still not universal**, but the remaining gaps are narrower and
  named:
  - ~~`budget.compute_budget()` returns `dict | None`, collapsing a Notion failure
    into "nothing to report" for `briefing._budget_line` and
    `budget_watch._should_warn`.~~ Resolved: `compute_budget` and `budget` both
    return `(value, error)` and neither returns a bare `None`. There is no third
    state to design around — an empty month is a dict whose total is `0.0`, which
    is exactly what stops the quiet month and the failed read from being the same
    value. All four readers report: the `B` command and the Sunday recap
    interpolate the reason instead of one generic sentence, the pacing warning
    returns `(None, error)` where it used to go silent, and the morning briefing
    says "I could not read your budget" beside the calendar half's equivalent
    line and returns both errors joined. `test_pacing_is_silent_when_notion_is_down`
    was the `known_bug` row asserting the old behaviour and was rewritten in the
    same commit, per the convention above.
  - `calendar_client` returns `[], err` on failure — the failure value IS the
    legitimate empty value, which is the root cause every caller has to work around
    by checking `err` FIRST. Changing it ripples into `find_conflicts`,
    `reminder.handle_remind` and three test files.
  - `reminder.handle_remind` discards the `find_conflicts` error into `_`, so a
    calendar read failure is indistinguishable from "the slot is clear". Deliberate
    (a failed check degrades to no warning rather than blocking the reminder) but
    undocumented until now. Named by FUNCTION, not by line: this entry said
    `reminder.py:93` and the line had since moved to 132, which is what a line
    number in a document nobody recompiles is always eventually worth.

### Left by the layering split, deliberately

Each of these was seen while moving code and NOT changed, because that PR was a pure
refactor and a fix hidden inside a move is a fix nobody reviewed.

- **Five modules never got the treatment.** `month.py`, `budget.py`, `pkm.py`,
  `reminder.py` and `notion_ids.py` are still at the root, and all but `budget.py` still
  take `update` and reply through `telegram_text` themselves — `pkm.handle_get`,
  `reminder.handle_remind`, `month.handle_month` and the three `notion_ids` handlers.
  They are the same welding the split removed everywhere else: none of them can be run
  from a scheduled job or driven from a test without a fake Update. `bot/commands.py`
  exists to hold their one-line adapters until each is split into a service and a
  handler, and the layering guard cannot see them because they are not under `services/`.
- **`escape_md` drags python-telegram-bot into `services/`.** A service formats its own
  Markdown, which is correct — escaping belongs at the interpolation site — but the
  function lives in `telegram_text.py`, which imports `telegram.error.BadRequest` for the
  senders' fallback. So `services/` transitively needs PTB installed for a regex. The
  guard permits it explicitly. Splitting `escape_md` into a telegram-free module would
  close it and would touch every call site.
- **The PDF parse cap is named after the download.** `services/books.py` bounds
  `extract_quote_from_pdf` with `clients.telegram_files.DOWNLOAD_TIMEOUT_SECONDS`, read
  live off the module so the two stay one value, as they were in `david.py`. It is the
  right duration and the wrong name.
- **`LEARN_ID`, `DIET_ID`, `BRAIN_ID` and `FINANCE_ID` in `david.py` have no reader.**
  They predate the split (each feature module reads its own), and they are left alongside
  `DATABASE_ID` rather than swept up in a refactor that was supposed to move code, not
  delete it.
- **`test_async_io.test_a_slow_command_no_longer_freezes_the_bot` can hang the suite
  rather than fail it.** Its watcher coroutine spins on `while not in_flight.is_set()`
  with no timeout, so if the stall it waits for never starts — which is exactly what a
  mis-targeted `monkeypatch` produces — pytest never returns. It cost a debugging round
  during the split. A bound there would turn that into a normal red.
- **`tests/test_anthropic_client.py` fails when run alone.**
  `test_every_call_logs_its_token_counts` passes in the full suite and fails as a
  single file, at HEAD and before the split alike — an order dependency, probably the
  daily-spend state. Pre-existing, unrelated to layering, and worth its own look.

## Implementation Plan Tracking

For any non-trivial task (multi-file changes, new features,
refactors, migrations), maintain a plan file at `PLAN.md` in
the project root.

### Before writing any code
1. Create or update `PLAN.md` with the full list of steps
   required, grouped under milestones.
2. Each step is a checkbox: `- [ ] Step description`.
3. Show me the plan and wait for confirmation before starting.

### Format
~~~
# Plan: <task name>
_Last updated: YYYY-MM-DD_

## Milestone 1: <name>
- [x] Completed step
- [ ] Pending step — <one-line note on approach or blocker>

## Open questions
- ...
~~~

### Rules for keeping it current
- The moment a step is finished, mark it `[x]` before moving
  to the next one. Never batch updates at the end.
- If requirements change, rewrite the affected steps
  immediately and note what changed and why under a
  `## Changelog` section.
- If you discover a step that wasn't in the plan, add it
  rather than doing it silently.
- If a step turns out to be unnecessary, strike it through
  (`- [ ] ~~step~~ — dropped: reason`) instead of deleting it.
- Re-read `PLAN.md` at the start of every session so you
  resume from the correct state.
- Keep steps atomic: each one should be independently
  verifiable.