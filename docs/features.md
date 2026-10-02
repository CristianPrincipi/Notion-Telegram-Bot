# Features

Everything David does, from your side of the chat. For how to set it up, see
[Setup](setup.md); for why it is built the way it is, see
[Architecture](architecture.md).

## How to talk to David

A message runs a command only when the **whole message** is that command —
mentioning `B` in the middle of a sentence does nothing. Capitals do not matter,
and neither does whitespace around the message. Send `h` at any time for the
in-chat list.

Editing a message you already sent does **not** run it again, so fixing a typo
never creates a second entry.

## The idea: Learn → Implement → Get

Most of David is bookkeeping — expenses, books, reminders. The part that is not
is a small knowledge system built from three commands:

1. **`Learn`** reads something — a YouTube video, an article, a podcast page, a
   PDF, or a book you name — and saves a structured summary as a page in your
   **Learn** database. Learn is the inbox: everything lands there first.
2. **`Implement`** takes one of those pages and merges what it teaches into the
   **Manual** of an **Area** — Brain, Finance, or any area you add. A Manual is
   one living page per area: a `⚙️ Perfect Process`, its improvements, a
   step-by-step breakdown, and a `📚 Sources` log of everything merged into it.
   Only the sections the new source touches are rewritten; the rest of the page
   is left exactly as it was.
3. **`Get`** reads a section back out of a Manual into the chat, when you need it.

Every Saturday David lists the Learn pages you saved over a week ago and never
implemented, and every Sunday it resurfaces one key takeaway from a random Learn
page, so the inbox does not quietly become an archive.

**Diet** is the one area that works differently: instead of a flat Manual it
keeps a structured page — categories, rows inside them, and the evidence behind
each row — and `Implement … - Diet` updates only the rows a source is about.

## Commands

| Command | What it does |
| --- | --- |
| `Add e [Name] [Amount] [Category]` | Add an expense. Categories: `s` Shopping, `f` Food, `g` Gift, `o` Other (default `f`) |
| `U e [Name] [Amount] [Category]` | Change an expense's amount (and category). **This month only**; several matches → numbered list, reply with a number |
| `D e [Name]` | Delete an expense. Same matching as `U e` |
| `undo` | Reverse the last delete, update or cancelled reminder |
| `B` | This month's budget: spent per category, total, and what is left |
| `Month` | Make sure this month's page exists, and show its ID. Happens by itself on the 1st |
| `Add b [Name] - [Author] - [Genre]` | Add a book. Genres: `s` `h` `m` `p` `a` `ph` |
| `Add q [Book] - [Title] - [Quote]` | Add a quote to a book |
| `Add q [Book] - [Title] - [Begin] / [End]` | Cut a quote out of an attached PDF — send the command as the file's caption |
| `Learn video\|article\|podcast\|book\|pdf [source]` | Summarise into Notion. A URL already saved is reported instead of summarised again; end the command with ` !` to re-summarise anyway |
| `Implement [Page] - [Area]` | Merge a Learn page into that area's Manual |
| `Get [Topic] - [Area]` | Read a section of that area's Manual. `Get ? - [Area]` lists every topic |
| `Remind [Name] [Date] - [Time]` | Create a Google Calendar event — see [dates and times](#dates-and-times) |
| `Agenda [Day]` | What is on the calendar. Today if no day is given |
| `Cancel [Name]` | Delete a calendar event in the next 30 days. Several matches → numbered list with their times. `undo` re-creates it |
| `Diag` / `Find [name]` / `DBs` | Notion diagnostics: check the Expenses setup, find any page's ID, list every database David can see |
| `h` | The in-chat command list. `help` and `aiuto` work too |

### Expenses

```
Add e Coffee 2,50
Add e Groceries 43.10 f
Add e Birthday present 30 g
U e Coffee 3
D e Coffee
B
```

Either decimal separator works (`2,50` or `2.50`), and the amount must be more
than zero. Leave the category out and it is Food; give one David does not know
and it says so instead of guessing. Dates follow Europe/Rome, not the server's
clock, so an expense logged just after midnight lands on the right day.

### Books and quotes

```
Add b Dune - Frank Herbert - a
Add q Dune - Fear - I must not fear. Fear is the mind-killer.
```

For a long quote in a PDF, attach the PDF and write the command as its caption,
giving the first and last few words: `Add q Dune - Fear - I must not fear / it
will be gone`. David finds the passage and saves everything between them.

### Learn

```
Learn video https://youtu.be/…
Learn article https://…
Learn podcast https://…
Learn book Thinking, Fast and Slow
Learn pdf                         ← as the caption of an attached PDF
```

- **video** — a YouTube link; David fetches the transcript (needs
  `SUPADATA_KEY`).
- **article** — any web page; David extracts the article text and skips the
  menus, cookie banners and footers around it.
- **podcast** — the episode's **web page**: David reads the text on the page
  (show notes, or a transcript if the page has one). It does not listen to
  audio.
- **book** — just a title. Nothing is read: the summary is Claude's recollection
  of the book, so the page opens with a red **UNVERIFIED** warning and is saved
  in **Books**, not Learn.
- **pdf** — attach the file (up to 15 MB) with `Learn pdf` as the caption.

Very long sources are summarised from their first ~100,000 characters, and the
reply says so when that happens.

### Implement and Get

```
Implement Atomic Habits - Brain
Implement Protein, Fiber and the Balanced Plate - Diet
Get ? - Brain
Get Perfect Process - Brain
```

`Implement` finds the Learn page by its title — any distinctive part of it is
enough — and the area by its name. The first run in an area builds the Manual from scratch; later
runs show you which sections they are changing, then change only those.

`Get` matches the topic loosely — `Get perfect - Brain` finds `⚙️ Perfect
Process`.

### Calendar

```
Remind Dentist 12.06 - 14.30
Remind Dentist tr 10
Agenda
Agenda tr
Agenda 12.06
Cancel Dentist
```

Every confirmation names the **weekday and full date** — `Friday 12 June 2026 at
14:30` — so a date read differently from what you meant is visible in the reply,
not discovered later.

### Diagnostics

`Diag` checks the Expenses database, its columns and this month's page, and
names whatever is missing. `Find Literature` searches every page and database
David can see and replies with their IDs. `DBs` lists every database. All three
are read-only.

## Dates and times

`Remind` and `Agenda` take the same day words.

| Day | Means |
| --- | --- |
| `12.06` | 12 June this year (for `Remind`, see the rule below) |
| `12.06.2027` | that exact date |
| `td` or `today` | today |
| `tr` or `tomorrow` | tomorrow |

| Time (`Remind` only) | Means |
| --- | --- |
| `14.30` | 14:30 |
| `10` | 10:00 |

The ` - ` between date and time is optional.

`Remind` refuses rather than guesses when:

- a date without a year has **just passed** (within the last day) — did you mean
  today, or next year? Add the year. A date further in the past means next year;
- `td` names a time that has **already gone** today — a reminder in the past
  never alerts you. Use `tr`, or a full date if you are recording something;
- you type a bare **`t`** — it used to mean tomorrow, and now names neither day;
- the time **does not exist or happens twice** because the clocks change that
  night.

`Agenda` reads a date without a year as this year, past or not — "what did I have
on 12.06?" is a normal question.

## When David is not sure which one you meant

`U e`, `D e` and `Cancel` change or delete something that already exists, so they
never guess between two matches.

- **One match** — done, and the reply says which.
- **Several** — nothing is changed. David lists them (amount, date and category
  for expenses; day and time for events) and waits for you to reply with a
  number. The list lapses after **2 minutes**, so a stray `2` an hour later does
  nothing. A number outside the list keeps it open.
- **None** — David says so.

Expenses are searched **this month only**; events, **the next 30 days**. If David
cannot work out which month it is in Notion, it refuses rather than searching
everything.

**`undo`** reverses the last delete, update or cancelled reminder — whichever
was most recent — and works once:

- a deleted expense comes back as it was;
- an updated expense gets its old amount and category back;
- a cancelled event is **re-created** as a new event, with the same title, time,
  description, location, recurrence and alerts. Anything Google tied to the
  original — guest replies above all — does not come back.

Only one numbered list and one `undo` exist at a time, across all three
commands: a number always answers the most recent list.

## Scheduled messages

All times Europe/Rome. They go to `CHAT_ID`.

| Time | Job | What it sends |
| --- | --- | --- |
| 00:05 daily | `month_rollover` | The new month's expense page. Silent unless something changed |
| 07:30 daily | `morning_briefing` | Today's calendar and a one-line budget pace |
| 13:00 daily | `budget_pacing` | A warning, **only** when the month is on course to overspend by 5% or more (from the 5th onwards) |
| 20:00 daily | `evening_briefing` | Tomorrow's events. Silent when tomorrow is empty |
| 09:30 Sunday | `budget_recap` | The full `B` recap |
| 11:00 Sunday | `takeaway` | One key takeaway from a random Learn page, and where it came from |
| 20:30 Sunday | `heartbeat` | Calendar and Notion checks plus activity counts. **Always sends** |
| 10:00 Saturday | `learn_nudge` | Learn pages saved over a week ago and never implemented. Silent when there are none |

**A message is silent only when there is genuinely nothing to say.** If a job
cannot read your calendar or Notion, it tells you so, rather than going quiet in
a way that looks like a free day.

**The heartbeat always sends, and that is its job.** A missing Sunday heartbeat
means David is not running.

## Learn in more detail

### The same link twice

David checks the Learn database for a URL before fetching anything, so sending a
link again after a timeout costs nothing. The check ignores differences that do
not change the content — `http`/`https`, `www.`, tracking parameters like
`utm_source`, the `#fragment`, a trailing slash — while keeping the ones that do,
so `?v=abc` and `?v=def` stay two videos. A match is reported with the date it
was saved; end the command with ` !` to summarise it again anyway.

This needs the `Source URL` column in Learn
([Notion schema](notion-schema.md#learn--learn_id)). Without it, David says so
and saves without checking.

`Learn book` has no URL and is never de-duplicated.

### What a `Learn book` page can do to a Manual

A `Learn book` summary is a recollection, not a reading, so `Implement` treats it
with suspicion:

- **It can add to a Manual, but not rewrite it.** David compares the merged
  section with what was there; if any existing line was removed or reworded, the
  whole section is held back and the reply names the lines that would have
  changed.
- **Nothing can check that what it adds is true.** That is why every merge is
  logged in the Manual's `📚 Sources` section, marked as read or recalled — the
  trail, so you can find what came from where.

### Half-written pages

Notion accepts at most 100 blocks per write, so a long summary or quote is saved
in several batches. If one fails part-way, the reply says how many landed ("2 of
5 batches written") instead of a plain failure — because running the command
again would add a second copy of the part that is already there.

### Files

PDFs are accepted with the `Learn pdf` and `Add q … / …` captions. Other file
types and files over **15 MB** are refused, and a download that takes over two
minutes is abandoned rather than left hanging.
