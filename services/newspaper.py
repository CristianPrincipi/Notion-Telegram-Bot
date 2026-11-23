"""The daily newspaper: RSS feeds in, one EPUB out. `Giornale` and the morning job.

    news_sources.yaml → collect (24 h, classify, cap) → dedupe → full text
    → "Le 3 notizie del giorno" → EPUB → re-opened to check it

No model and no paid API anywhere in it. Synchronous on purpose (the callers
run it via asyncio.to_thread under an asyncio.wait_for cap); it sends nothing,
and returns the finished file as bytes with its name and caption — the temporary
file it was written to is gone before it returns, so a sender has nothing to
clean up.
"""

import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime

from services import newspaper_collect, newspaper_epub, newspaper_fulltext, newspaper_topnews
from services.newspaper_sources import SECTIONS, load_config

logger = logging.getLogger(__name__)

# Telegram's limit on a document caption.
CAPTION_LIMIT = 1024


@dataclass
class Result:
    epub: bytes
    filename: str
    when: datetime
    counts: dict                                   # section → articles
    failed: list = field(default_factory=list)     # "Source (why)"
    summaries: int = 0                             # articles shown as the feed summary

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def caption(self) -> str:
        return caption(self)


def filename_for(when: datetime) -> str:
    return f"Giornale_{when:%Y-%m-%d}.epub"


def caption(result: Result) -> str:
    """Plain text: source names are interpolated, and they are not Markdown-safe."""
    lines = [f"📰 Giornale di {newspaper_epub.italian_date(result.when)}",
             f"{result.total} articoli"]
    lines += [f"• {section}: {result.counts.get(section, 0)}" for section in SECTIONS]
    if result.summaries:
        lines.append(f"({result.summaries} solo sommario: paywall o testo non leggibile)")
    if result.failed:
        lines.append("⚠️ Fonti non raggiunte: " + ", ".join(result.failed))
    text = "\n".join(lines)
    return text if len(text) <= CAPTION_LIMIT else text[:CAPTION_LIMIT - 1] + "…"


def build_newspaper(now: datetime, *, fetcher=newspaper_collect.fetch,
                    config_path: str | None = None) -> tuple[Result | None, str | None]:
    """Build today's edition. (Result, None), or (None, why) when there is no
    newspaper to send — a broken YAML, every source down, or an EPUB that does
    not read back. A few sources down is NOT an error: it is in Result.failed."""
    cfg, err = load_config(config_path) if config_path else load_config()
    if err:
        return None, err

    logger.info("Newspaper: building the edition for %s from %d sources",
                now.date(), len(cfg.sources))
    articles, pool, failed = newspaper_collect.collect(cfg, now, fetcher=fetcher)
    if not articles:
        if len(failed) == len(cfg.sources):
            return None, "no source could be read: " + ", ".join(failed)
        logger.warning("Newspaper: no article in the last day from any source")

    articles, _ = newspaper_collect.dedupe(articles)
    summaries = newspaper_fulltext.fill_bodies(articles, fetcher=fetcher)

    top_titles, top_err = newspaper_collect.top_news_titles(cfg.top_news_feed, fetcher=fetcher)
    if top_err:
        failed.append(top_err)
    stories = newspaper_topnews.top_stories(articles, pool, top_titles, cfg.cluster_threshold)

    sections = {section: [] for section in SECTIONS}
    for article in articles:
        sections[article.section].append(article)
    for section in SECTIONS:
        sections[section].sort(key=lambda a: a.published, reverse=True)

    edition = newspaper_epub.Edition(title=cfg.title, when=now, sections=sections,
                                     stories=stories)
    name = filename_for(now)
    with tempfile.TemporaryDirectory(prefix="giornale-") as folder:
        path = os.path.join(folder, name)
        try:
            newspaper_epub.build_epub(edition, path)
        except Exception as e:
            logger.exception("Newspaper: the EPUB could not be written")
            return None, f"the EPUB could not be written: {type(e).__name__}: {e}"
        problem = newspaper_epub.check_epub(path, edition.total)
        if problem:
            return None, problem
        with open(path, "rb") as fh:
            data = fh.read()

    counts = {section: len(sections[section]) for section in SECTIONS}
    logger.info("Newspaper: %s written — %d articles (%s), %d summaries, %d failed sources",
                name, edition.total, ", ".join(f"{s} {n}" for s, n in counts.items()),
                summaries, len(failed))
    return Result(epub=data, filename=name, when=now, counts=counts, failed=failed,
                  summaries=summaries), None
