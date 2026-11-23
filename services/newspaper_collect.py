"""Gathering the day's items: fetch each feed, keep the last 24 hours, sort each
item into a section, drop the duplicates.

Every source is fetched on its own: one feed down, or answering garbage, is
logged and named in the delivery message, and the newspaper is built from the
others.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

import config
from services.newspaper_sources import AUTO, SECTIONS

logger = logging.getLogger(__name__)

# A browser User-Agent: several Italian publishers answer 403 to python-requests.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


@dataclass
class Article:
    title: str
    url: str
    source: str
    published: datetime           # aware
    summary: str = ""             # plain text, from the feed
    categories: tuple = ()
    section: str | None = None
    body_html: str = ""           # sanitised; filled by newspaper_fulltext
    full_text: bool = False       # False: the body is the feed summary
    text_chars: int = 0           # length of the body as plain text
    anchor: str = ""              # its id in the EPUB, set when the edition is laid out


def fetch(url: str) -> bytes:
    """GET with the browser User-Agent and the per-request timeout. Raises."""
    resp = requests.get(url, headers={"User-Agent": USER_AGENT},
                        timeout=config.NEWSPAPER_FETCH_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def _plain(html: str) -> str:
    if not html:
        return ""
    text = BeautifulSoup(html, "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", text).strip()


def _published(entry) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc)


def parse_feed(raw: bytes, source_name: str) -> tuple[list[Article], int]:
    """Every dated item of one feed, newest first, and how many had no date.

    An item with no date cannot be placed in the 24-hour window, so it is left
    out rather than guessed into today's paper.
    """
    feed = feedparser.parse(raw)
    if not feed.entries and feed.bozo:
        raise ValueError(f"not a feed ({feed.bozo_exception})")
    articles, undated = [], 0
    for entry in feed.entries:
        when = _published(entry)
        link = (entry.get("link") or "").strip()
        title = _plain(entry.get("title", ""))
        if when is None:
            undated += 1
            continue
        if not link or not title:
            continue
        articles.append(Article(
            title=title, url=link, source=source_name, published=when,
            summary=_plain(entry.get("summary", "")),
            categories=tuple(t.get("term", "").strip() for t in entry.get("tags", [])
                             if t.get("term", "").strip()),
        ))
    articles.sort(key=lambda a: a.published, reverse=True)
    return articles, undated


def in_window(articles: list[Article], now: datetime,
              hours: int = config.NEWSPAPER_WINDOW_HOURS) -> list[Article]:
    since = now - timedelta(hours=hours)
    # A date in the future (a publisher's wrong timezone) still counts as today.
    return [a for a in articles if a.published >= since]


# ─── CLASSIFICATION ────────────────────────────────────────────────────────────

def _keyword_pattern(word: str) -> re.Pattern:
    # Whole words only: "ai" must not match "mai", nor "ue" "questue".
    return re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.I)


def keyword_section(text: str, keywords: dict) -> str | None:
    """The section whose keywords occur most often in `text`; ties go to the
    section that comes first in the newspaper. None when nothing matches."""
    best, best_hits = None, 0
    for section in SECTIONS:
        hits = sum(1 for word in keywords.get(section, ())
                   if _keyword_pattern(word).search(text))
        if hits > best_hits:
            best, best_hits = section, hits
    return best


def classify(article: Article, source_section: str, category_map: dict,
             keywords: dict) -> str | None:
    """A fixed section wins; `auto` tries the category map (in the map's order),
    then the keywords on title + summary. None: drop it."""
    if source_section != AUTO:
        return source_section
    tags = {c.lower() for c in article.categories}
    for category, section in category_map.items():
        if category.lower() in tags:
            return section
    return keyword_section(f"{article.title} {article.summary}", keywords)


# ─── COLLECTION ────────────────────────────────────────────────────────────────

def collect_source(source, now: datetime, category_map: dict, keywords: dict,
                   fetcher=fetch) -> tuple[list[Article], list[Article]]:
    """One source's articles for today, classified, newest first: (at most `max`
    of them for the paper, all of them for scoring the top stories).

    The cap is applied AFTER classification: Il Post's ten newest items are
    mostly sport some mornings, and capping first would keep the sport and then
    drop it, leaving nothing. Raises on a fetch or parse failure.
    """
    articles, undated = parse_feed(fetcher(source.url), source.name)
    recent = in_window(articles, now)
    kept, dropped = [], 0
    for article in recent:
        article.section = classify(article, source.section, category_map, keywords)
        if article.section is None:
            dropped += 1
            logger.debug("Newspaper: %s dropped (no section): %s", source.name, article.title)
            continue
        kept.append(article)
    classified, kept = kept, kept[:source.max]
    logger.info("Newspaper: %s — %d items, %d in the last %dh, %d unclassified, %d kept%s",
                source.name, len(articles), len(recent), config.NEWSPAPER_WINDOW_HOURS,
                dropped, len(kept), f", {undated} undated skipped" if undated else "")
    return kept, classified


def collect(cfg, now: datetime, fetcher=fetch) -> tuple[list[Article], list[Article], list[str]]:
    """(the paper's articles, every classified item of the day, the sources that
    failed with why). The second list is wider than the first by the `max` caps:
    a story counts every source that ran it, not only the ones that made the cut."""
    articles, pool, failed = [], [], []
    for source in cfg.sources:
        try:
            kept, classified = collect_source(source, now, cfg.category_map,
                                              cfg.keywords, fetcher)
        except Exception as e:
            logger.warning("Newspaper: source %s failed: %s: %s",
                           source.name, type(e).__name__, e)
            failed.append(f"{source.name} ({_short_error(e)})")
            continue
        articles += kept
        pool += classified
    return articles, pool, failed


def top_news_titles(url: str | None, fetcher=fetch) -> tuple[list[str], str | None]:
    """The titles of the top-news feed, for the +1 bonus. A failure costs only
    the bonus, so it is returned as an error to name, not raised."""
    if not url:
        return [], None
    try:
        articles, _ = parse_feed(fetcher(url), "top news")
    except Exception as e:
        logger.warning("Newspaper: top-news feed failed: %s: %s", type(e).__name__, e)
        return [], f"top news ({_short_error(e)})"
    return [a.title for a in articles], None


def _short_error(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    if isinstance(e, requests.Timeout):
        return "timeout"
    if isinstance(e, requests.ConnectionError):
        return "unreachable"
    return type(e).__name__


# ─── DE-DUPLICATION ────────────────────────────────────────────────────────────

def normalise_url(url: str) -> str:
    """Scheme and host lower-cased, query and fragment and trailing slash gone:
    a feed's tracking parameters must not make one article two."""
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def _length(article: Article) -> int:
    return article.text_chars or len(article.summary)


def dedupe(articles: list[Article],
           threshold: int = config.NEWSPAPER_DEDUPE_THRESHOLD) -> tuple[list[Article], int]:
    """Drop repeats: the same normalised URL anywhere, or titles at or above
    `threshold` (rapidfuzz ratio, case folded) within one section. The longest
    of each group is kept. Returns (kept, how many were dropped); order kept."""
    kept: list[Article] = []
    for article in articles:
        twin = next((k for k in kept if _same(k, article, threshold)), None)
        if twin is None:
            kept.append(article)
        elif _length(article) > _length(twin):
            kept[kept.index(twin)] = article
    dropped = len(articles) - len(kept)
    if dropped:
        logger.info("Newspaper: %d duplicates dropped", dropped)
    return kept, dropped


def _same(a: Article, b: Article, threshold: int) -> bool:
    if normalise_url(a.url) == normalise_url(b.url):
        return True
    return (a.section == b.section
            and fuzz.ratio(a.title.lower(), b.title.lower()) >= threshold)
