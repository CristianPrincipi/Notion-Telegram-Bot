"""Each article's body: the full text where the site gives it, the feed's
summary where it does not.

trafilatura reads the page; what it returns is cut down to a short list of
tags an e-reader renders well and nothing can run in (`ALLOWED_TAGS`). Under
NEWSPAPER_MIN_TEXT_CHARS of text the page is taken for a paywall or a teaser:
the summary is used, with a note pointing at the site.
"""

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import trafilatura
from bs4 import BeautifulSoup, Comment, Declaration, Doctype, NavigableString, ProcessingInstruction, Tag

import config
from services.newspaper_collect import Article, fetch

logger = logging.getLogger(__name__)

ALLOWED_TAGS = {"p", "h2", "h3", "ul", "ol", "li", "blockquote", "a", "strong", "em"}
# Renamed rather than unwrapped, so the emphasis or the heading survives.
RENAMED_TAGS = {"b": "strong", "i": "em", "h1": "h2", "h4": "h3", "h5": "h3", "h6": "h3"}
# Removed WITH their content: none of it is article text.
DROPPED_TAGS = {"script", "style", "iframe", "object", "embed", "form", "button",
                "input", "select", "textarea", "noscript", "svg", "img", "figure",
                "video", "audio", "picture", "source", "table", "head", "title"}
_NOT_TEXT = (Comment, Declaration, Doctype, ProcessingInstruction)
BLOCK_TAGS = {"p", "h2", "h3", "ul", "ol", "blockquote"}

FALLBACK_NOTE = "Articolo completo sul sito"

# A page that answers with a consent or subscription wall instead of the article
# still yields 300+ characters — of the wall. Seen on ANSA (2026-11-23): its
# "Consentless" offer, extracted whole as if it were the story. Any of these, in
# the extracted text, means the summary is used instead. Lower case.
WALL_MARKERS = (
    "consentless",
    "cookie di profilazione",
    "contenuti consentiti in 30 giorni",
    "per continuare a leggere",
    "abbonati per leggere",
    "riservato agli abbonati",
    "subscribe to continue reading",
    "this content is for subscribers",
)


def sanitize(markup: str) -> str:
    """Only ALLOWED_TAGS survive; links keep an http(s) href and nothing else;
    every other attribute goes. A <br> splits its paragraph in two (ANSA writes
    paragraphs as one <p> with <br>s). Loose text is wrapped in <p>."""
    soup = BeautifulSoup(markup or "", "html.parser")
    for tag in soup.find_all(DROPPED_TAGS):
        tag.decompose()
    for node in soup.find_all(string=lambda s: isinstance(s, _NOT_TEXT)):
        node.extract()
    for tag in soup.find_all(True):
        name = RENAMED_TAGS.get(tag.name, tag.name)
        if name not in ALLOWED_TAGS and tag.name != "br":
            tag.unwrap()
            continue
        tag.name = name
        href = tag.get("href", "") if name == "a" else ""
        tag.attrs = {}
        if name == "a":
            if href.startswith(("http://", "https://")):
                tag["href"] = href
            else:
                tag.unwrap()
    return _blocks(soup)


def _blocks(soup) -> str:
    """Re-serialise as a flat run of block elements, each one well-formed."""
    out, loose = [], []

    def flush():
        text = "".join(loose).strip()
        if _text_of(text):
            out.append(f"<p>{text}</p>")
        loose.clear()

    for node in soup.contents:
        if isinstance(node, Tag) and node.name in BLOCK_TAGS:
            flush()
            if node.name == "p":
                out += [f"<p>{part}</p>" for part in _split_on_br(node) if _text_of(part)]
            elif _text_of(_inner(node)):
                for br in node.find_all("br"):
                    br.replace_with(" ")
                out.append(f"<{node.name}>{_inner(node).strip()}</{node.name}>")
        elif isinstance(node, Tag) and node.name == "br":
            flush()
        elif isinstance(node, NavigableString):
            loose.append(html.escape(str(node), quote=False))
        else:
            loose.append(str(node))
    flush()
    return "\n".join(out)


def _inner(tag: Tag) -> str:
    return "".join(str(child) if isinstance(child, Tag)
                   else html.escape(str(child), quote=False) for child in tag.contents)


def _split_on_br(p: Tag) -> list[str]:
    parts, current = [], []
    for child in p.contents:
        if isinstance(child, Tag) and child.name == "br":
            parts.append("".join(current).strip())
            current = []
        elif isinstance(child, Tag):
            for br in child.find_all("br"):
                br.replace_with(" ")
            current.append(str(child))
        else:
            current.append(html.escape(str(child), quote=False))
    parts.append("".join(current).strip())
    return parts


def _text_of(markup: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(markup, "html.parser").get_text(" ")).strip()


def extract(raw: bytes, url: str) -> str:
    """trafilatura's reading of the page, sanitised. Empty when it found nothing."""
    found = trafilatura.extract(raw, url=url, output_format="html", include_links=True,
                                include_images=False, include_tables=False,
                                include_comments=False)
    if not found:
        return ""
    soup = BeautifulSoup(found, "html.parser")
    body = soup.body or soup
    return sanitize(_inner(body))


def summary_body(article: Article) -> str:
    summary = html.escape(article.summary) if article.summary else ""
    note = f'<p><em>{FALLBACK_NOTE}</em></p>'
    return (f"<p>{summary}</p>\n{note}" if summary else note)


def _fold(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def tidy(body: str, title: str) -> str:
    """Drop the blocks that are not the story: the title repeated at the top
    (Il Sole's <h2>, ANSA's "Title - Notizie - Ansa.it"), and any block that
    repeats one before it ("3' di lettura" twice)."""
    folded_title = _fold(title)
    kept, seen, leading = [], set(), True
    for block in body.split("\n"):
        folded = _fold(_text_of(block))
        if not folded or folded in seen:
            continue
        if leading and folded_title and folded.startswith(folded_title):
            continue
        leading = False
        seen.add(folded)
        kept.append(block)
    return "\n".join(kept)


def behind_a_wall(body: str) -> bool:
    text = _text_of(body).lower()
    return any(marker in text for marker in WALL_MARKERS)


def fill_body(article: Article, fetcher=fetch) -> Article:
    """Give `article` its body. Never raises: any failure is the summary."""
    reason = None
    try:
        body = tidy(extract(fetcher(article.url), article.url), article.title)
        chars = len(_text_of(body))
        if behind_a_wall(body):
            reason = "consent or subscription wall"
        elif chars >= config.NEWSPAPER_MIN_TEXT_CHARS:
            article.body_html, article.full_text, article.text_chars = body, True, chars
            return article
        else:
            reason = f"only {chars} chars"
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
    logger.info("Newspaper: summary used for %s (%s): %s", article.source, reason, article.url)
    article.body_html = summary_body(article)
    article.full_text = False
    article.text_chars = len(article.summary)
    return article


def fill_bodies(articles: list[Article], fetcher=fetch,
                workers: int = config.NEWSPAPER_FETCH_WORKERS) -> int:
    """Every article's body, `workers` pages at a time. Returns how many fell
    back to the summary. The worker threads only fetch and parse; nothing here
    is shared between them but the article each one owns."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda a: fill_body(a, fetcher), articles))
    fallbacks = sum(1 for a in articles if not a.full_text)
    logger.info("Newspaper: %d full texts, %d summaries", len(articles) - fallbacks, fallbacks)
    return fallbacks
