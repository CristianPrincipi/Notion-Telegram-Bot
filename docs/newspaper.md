# The daily newspaper

Every morning David builds `Giornale_AAAA-MM-GG.epub` from RSS feeds and sends it
to the chat as a file, to open on an e-reader (a Boox, a Kobo, anything that
reads EPUB). Send `Giornale` or `/giornale` for one now: David answers "Sto
preparando il giornale…", and the file follows, usually within a minute.

It costs nothing: no model reads or chooses anything, and no API is paid for.
The feeds are read with `feedparser`, the articles with `trafilatura`, the top
stories are counted with `rapidfuzz`, and the book is made with `ebooklib`.

## What is in it

1. **A text cover** — the title (`title` in the file), the date in Italian, and
   how many articles.
2. **Le 3 notizie del giorno** — the three stories the most sources ran: title,
   the opening paragraph, the other sources that covered it (with links), and a
   link to the full article in its section.
3. **Six sections, always in this order:** Finanza, Politica Italiana,
   Tecnologia, Geopolitica, Scienze, Ambiente. A section with nothing today says
   so.
4. **A table of contents** with every section and, under it, every article.

Each article has its title, source, date and time (Europe/Rome), the full text,
and a link to the original at the end. English articles stay in English. Where a
page is behind a paywall or a consent wall, or gives under 300 characters of
text, the article is the feed's summary with "Articolo completo sul sito".
Images are left out.

The message that carries the file lists the date, how many articles each
section has, how many are summary-only, and any source that could not be read.

## How the articles are chosen

1. **Collected:** each feed in `news_sources.yaml`, items published in the last
   24 hours only. A feed that fails is logged and named in the message; the rest
   of the paper is built anyway.
2. **Sorted into a section:** a source with a fixed `section` puts every item
   there. A source with `section: auto` tries the item's `<category>` tags against
   `category_map`, then the `keywords` on its title and summary; with neither, the
   item is dropped. Then at most `max` items per source, newest first.
3. **De-duplicated:** the same link (query string ignored), or two titles in the
   same section at 90% similarity or more — the longer article is kept.
4. **Full text** from each page.
5. **The three stories:** every item of the day is grouped by title similarity
   (lower case, no punctuation, no stopwords; `cluster_threshold`, 75 by
   default). A group scores one point per distinct source, plus one if ANSA's top
   news feed carries it. A tie goes to the group with more distinct sources, then
   to the newest. An article used there stays in its section too.

## Changing it: `news_sources.yaml`

The file is at the root of the repository. Edit it, push, and the next build
uses it — the sources, map and keywords are read on every build; **the time is
read at startup**, so a new `schedule` takes effect on the next deploy. A mistake
in the file is reported in the chat with every problem listed, and nothing is
sent.

### Sources

```yaml
sources:
  - {name: ANSA Economia, url: "https://www.ansa.it/sito/notizie/economia/economia_rss.xml", section: Finanza, max: 6}
  - {name: Il Post, url: "https://www.ilpost.it/feed", section: auto, max: 10}
```

- `name` is what the paper and the message call it.
- `section` is one of the six names above, exactly, or `auto`.
- `max` is the most items it contributes per day.

Before adding a feed, check it:

```bash
python scripts/check_feeds.py news_sources.yaml
```

It prints, for every feed, whether it answers, how many items it has, the date of
the newest, and every `<category>` value it uses — which is what `category_map`
needs. A feed whose newest item is over 7 days old is flagged **STALE**.

`top_news_feed` is not a source: its titles only give a story its bonus point.

### The category map and the keywords

For `auto` sources only.

```yaml
category_map:
  Economia: Finanza
  Mondo: Geopolitica

keywords:
  Ambiente: [clima, cop31, emissioni, siccità]
```

- `category_map` maps a feed's category (left, as `check_feeds.py` prints it,
  case ignored) to a section. An item with several categories takes the first one
  listed in the map. A category not in the map is ignored, so the item falls to
  the keywords — this is how Il Post's "Italia" (crime news as much as politics)
  is filtered through the Politica Italiana keywords, and how "Sport" or "Cultura"
  are dropped.
- `keywords` are matched as whole words (`ai` does not match "mai"), case
  ignored, on the title and the summary. The section with the most matches wins;
  a tie goes to the earlier section.

### The time

```yaml
schedule: "06:30"
```

`HH:MM`, Europe/Rome — Railway runs in UTC; the job does not care. If the file
cannot be read at startup the job still runs, at 06:30, and reports the file.

### The title

```yaml
title: "Il Giornale di Cristian"
```

## Building one locally

No Telegram, no environment variables:

```bash
python scripts/build_newspaper.py /tmp
```

writes `/tmp/Giornale_AAAA-MM-GG.epub` from the real feeds and prints the message
the bot would send. `--sources other.yaml` tries a different file.
