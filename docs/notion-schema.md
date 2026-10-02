# Notion schema

Everything David reads from and writes to in Notion. The
[Notion template](https://protective-cost-810.notion.site/David-Notion-Template-3e853cc47bc281a28e30c6f0d37acbcf)
already has all of it — duplicate it and you can skip this page. Read on if you
are wiring David to databases you already have, or renaming something.

## How David finds things

- **Databases by ID.** Each one is an environment variable
  ([Configuration](configuration.md)). Their names in Notion do not matter.
- **Columns by name.** Every column below must exist with exactly this name and a
  compatible type. The names live in one place, the `NOTION SCHEMA` section of
  [`config.py`](../config.py), so renaming a column in Notion means renaming it
  there too ([Customization](customization.md#notion-column-names)).
- **The title column** is found automatically when David *searches* a database,
  but David *writes* it by name — so it must be called `Name` in every database
  David creates pages in (all of them except Months).
- **A few pages by title** — `Manual` in each area, `Diet` in the Diet database,
  and one page per month. David creates each of them the first time it needs it.

A write that names a column Notion does not have is rejected outright, and
nothing is saved. That is the most common setup error; see
[Troubleshooting](troubleshooting.md#a-command-fails-with-a-notion-400).

## Expenses — `EXPENSES_ID`

| Column | Type | Needed | Used by |
| --- | --- | --- | --- |
| `Name` | Title | Yes | `Add e` writes it; `U e` and `D e` search it |
| `Amount` | Number (€ in the template) | Yes | `Add e` and `U e` write it; `B` and the budget messages add it up |
| `Date` | Date | Yes | `Add e` writes today's date |
| `Category` | Multi-select | Yes | `Add e` and `U e`; `B` breaks the month down by it |
| `Account` | Relation → Months | Yes | every expense is related to this month's page; `B` sums the expenses related to it |

`Category` options: `Shopping`, `Food`, `Gift`, `Other` — typed as `s`, `f`, `g`,
`o`, with `Food` as the default. Changing them:
[Customization](customization.md#expense-categories-and-book-genres).

## Months — discovered, or `MONTHS_DB_ID`

David finds this database by following the Expenses `Account` relation, so it
needs no variable of its own.

| Column | Type | Needed | Used by |
| --- | --- | --- | --- |
| *(title, any name)* | Title | Yes | one page per month, titled like `October 2026` |

David creates the month's page on the 1st (or the first time an expense needs
it), and renames a page titled just `October` rather than making a second one.
The template also has an `Expenses` column: the other side of the `Account`
relation, which Notion adds by itself. David does not use it.

## Books — `LETTI_ID`

| Column | Type | Needed | Used by |
| --- | --- | --- | --- |
| `Name` | Title | Yes | `Add b`, `Learn book`; `Add q` finds the book by it |
| `Author` | Text | Yes | `Add b`, `Learn book` |
| `Genre` | Multi-select | Yes | `Add b` |
| `Area` | Relation → Areas | Yes | `Add b` relates every book to the `Literature` page |

`Genre` options: `Satira`, `History`, `Manga`, `Poetry`, `Adventure`,
`Philosophy` — typed as `s`, `h`, `m`, `p`, `a`, `ph`.

## Areas — no variable

David never queries this database. It holds one page David needs: **`Literature`**,
whose page ID is `LITERATURE_ID`.

## Learn — `LEARN_ID`

| Column | Type | Needed | Used by |
| --- | --- | --- | --- |
| `Name` | Title | Yes | every `Learn`; `Implement` finds the source page by it |
| `Author` | Text | Yes | `Learn article` writes the byline. Without the column, `Learn article` fails on anything with an author |
| `Source URL` | URL (or Text) | Recommended | stops the same link being summarised twice. Without it, David says so on every `Learn` and carries on without the check |
| `Implemented` | Checkbox | Recommended | `Implement` ticks it on the source page; the Saturday nudge lists pages saved over a week ago and still unticked. Without it, `Implement` still works and the nudge reports that it cannot run |

`Learn book` pages go to **Books**, not here: a book belongs with your books.

## Area databases — `BRAIN_ID`, `FINANCE_ID`, any `{AREA}_ID`

| Column | Type | Needed | Used by |
| --- | --- | --- | --- |
| `Name` | Title | Yes | `Implement` creates the area's `Manual` page; `Get` reads it |

The first `Implement … - Brain` creates a page called `Manual` and builds it
from that source; later runs merge into it. The page has a fixed outline:

1. an overview callout
2. `⚙️ Perfect Process` — the routine, as numbered steps
3. `🚀 Improvements & Optimizations`
4. `📖 Step-by-Step Breakdown`, one `→ step` sub-heading per step
5. `📚 Sources` — a log of every page merged in, with its date and whether it
   was read or recalled. David appends to it on every run, and nothing else may
   edit it.

## Diet — `DIET_ID`

| Column | Type | Needed | Used by |
| --- | --- | --- | --- |
| `Name` | Title | Yes | `Implement … - Diet` creates the `Diet` page; `Get … - Diet` reads it |

The Diet area is not a flat Manual. Its `Diet` page is a tree of toggle headings —
`Mediterranean Diet`, `Seasonality`, `Goals`, `Supplementation`, each with rows
inside and the evidence for each row (Question, Result, Limits, Practical
Conclusion) below that. `Implement` updates only the rows a source touches.

## Headings David looks for inside pages

| Heading | In | Used by |
| --- | --- | --- |
| `✅ Key Takeaways` | Learn pages (David writes it) | the Sunday takeaway picks one bullet from under it |
| `📚 Sources` | each `Manual` | David's log of what was merged |
| the red `UNVERIFIED — generated from model recollection` callout | `Learn book` pages | `Implement` warns when merging from one, and may only add to a Manual from it, never rewrite |
