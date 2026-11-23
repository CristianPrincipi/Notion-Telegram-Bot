"""Check every feed the daily newspaper would read, before trusting it.

    python scripts/check_feeds.py [path/to/news_sources.yaml]

For each source it prints: valid or not, the item count, the date of the newest
item (and how many days ago that was), and every <category> value seen, with
counts. A feed whose newest item is older than STALE_DAYS is flagged STALE.

With no argument it reads the sources listed below (the proposal under review);
once news_sources.yaml exists it is the argument. Nothing here is written
anywhere: it only reads the feeds and prints.
"""

import sys
from collections import Counter
from datetime import datetime, timezone

import feedparser
import requests

TIMEOUT_SECONDS = 15
STALE_DAYS = 7
# A browser User-Agent: several Italian publishers answer 403 to python-requests.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)

DEFAULT_SOURCES = [
    ("ANSA Economia", "https://www.ansa.it/sito/notizie/economia/economia_rss.xml"),
    ("Il Sole 24 Ore", "https://www.ilsole24ore.com/rss/finanza.xml"),
    ("ANSA Politica", "https://www.ansa.it/sito/notizie/politica/politica_rss.xml"),
    ("AGI Politica", "https://www.agi.it/politica/rss"),
    ("ANSA Tecnologia", "https://www.ansa.it/canale_tecnologia/notizie/tecnologia_rss.xml"),
    ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
    ("ANSA Mondo", "https://www.ansa.it/sito/notizie/mondo/mondo_rss.xml"),
    ("BBC World", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("Internazionale", "https://www.internazionale.it/sitemaps/rss.xml"),
    ("ANSA Scienza", "https://www.ansa.it/canale_scienza_tecnica/notizie/scienzaetecnica_rss.xml"),
    ("Carbon Brief", "https://www.carbonbrief.org/feed/"),
    ("Il Post", "https://www.ilpost.it/feed/"),
    ("ANSA Top news", "https://www.ansa.it/sito/notizie/topnews/topnews_rss.xml"),
]


def load_sources(argv):
    if len(argv) < 2:
        return DEFAULT_SOURCES
    import yaml

    with open(argv[1], encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    sources = [(s["name"], s["url"]) for s in data.get("sources", [])]
    if data.get("top_news_feed"):
        sources.append(("Top news feed", data["top_news_feed"]))
    return sources


def entry_time(entry):
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def check(name, url):
    """Return (report_lines, problem_or_None)."""
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException as e:
        return [f"  NOT VALID — request failed: {type(e).__name__}: {e}"], "unreachable"
    if resp.status_code != 200:
        return [f"  NOT VALID — HTTP {resp.status_code}"], f"HTTP {resp.status_code}"

    feed = feedparser.parse(resp.content)
    if not feed.entries:
        why = feed.get("bozo_exception") or "no items"
        return [f"  NOT VALID — {why}"], "no items"

    times = [t for t in (entry_time(e) for e in feed.entries) if t]
    newest = max(times) if times else None
    categories = Counter(
        tag.get("term", "").strip()
        for e in feed.entries
        for tag in e.get("tags", [])
        if tag.get("term", "").strip()
    )

    lines = [f"  valid — {len(feed.entries)} items"]
    problem = None
    if newest:
        age = (datetime.now(timezone.utc) - newest).total_seconds() / 86400
        stale = age > STALE_DAYS
        lines.append(
            f"  newest item: {newest:%Y-%m-%d %H:%M} UTC ({age:.1f} days ago)"
            + ("  ← STALE" if stale else "")
        )
        if stale:
            problem = f"stale ({age:.0f} days)"
        dated = len(times)
        if dated < len(feed.entries):
            lines.append(f"  {len(feed.entries) - dated} items carry no date")
    else:
        lines.append("  newest item: no item carries a date")
        problem = "no dates"
    if feed.bozo:
        lines.append(f"  note: parser warning — {feed.bozo_exception}")
    if categories:
        cats = ", ".join(f"{c} ({n})" for c, n in categories.most_common())
        lines.append(f"  categories: {cats}")
    else:
        lines.append("  categories: none")
    return lines, problem


def main(argv):
    problems = []
    for name, url in load_sources(argv):
        print(f"{name}  <{url}>")
        lines, problem = check(name, url)
        print("\n".join(lines))
        print()
        if problem:
            problems.append(f"{name}: {problem}")
    print("=" * 60)
    if problems:
        print("Problems:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("Every feed is valid and fresh.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
