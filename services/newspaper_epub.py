"""Laying the edition out as an EPUB (ebooklib), for an e-ink reader.

One chapter for the cover, one for "Le 3 notizie del giorno", one per section
in SECTIONS order — an empty section included, saying so, so the paper always
has the same shape. The table of contents nests every article under its
section. Article bodies arrive sanitised (newspaper_fulltext.sanitize); every
other string is escaped here.
"""

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

import pytz
from ebooklib import epub

import config
from services.newspaper_sources import SECTIONS

logger = logging.getLogger(__name__)

_TZ = pytz.timezone(config.PROACTIVE_TIMEZONE)

WEEKDAYS = ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica")
MONTHS = ("gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
          "agosto", "settembre", "ottobre", "novembre", "dicembre")

CSS = """
body { font-family: Georgia, "Times New Roman", serif; line-height: 1.5;
       margin: 0 5%; color: #000; background: none; }
h1 { font-weight: bold; font-size: 1.8em; text-align: center; margin: 2em 0 0.5em; }
h2 { font-weight: bold; font-size: 1.3em; margin: 1.6em 0 0.3em; page-break-after: avoid; }
h3 { font-weight: bold; font-size: 1.1em; margin: 1.2em 0 0.3em; }
p { margin: 0 0 0.8em; text-align: left; }
blockquote { margin: 0.8em 1.5em; font-style: italic; }
a { color: #000; text-decoration: underline; }
.meta { font-size: 0.85em; font-style: italic; margin-bottom: 1em; }
.cover { text-align: center; margin-top: 30%; }
.cover p { text-align: center; font-size: 1.1em; }
.original { font-size: 0.85em; margin-top: 1.2em; border-top: 1px solid #000; padding-top: 0.4em; }
.article { page-break-before: always; }
.article:first-of-type { page-break-before: auto; }
.story { margin-bottom: 2em; }
"""


@dataclass
class Edition:
    title: str
    when: datetime                       # aware; the edition's date
    sections: dict                       # section → [Article], SECTIONS order
    stories: list = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.sections.values())


def italian_date(when: datetime) -> str:
    return f"{WEEKDAYS[when.weekday()]} {when.day} {MONTHS[when.month - 1]} {when.year}"


def chapter_file(section: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", section.lower()).strip("-")
    return f"sezione-{slug}.xhtml"


def assign_anchors(edition: Edition) -> None:
    number = 0
    for section in SECTIONS:
        for article in edition.sections.get(section, []):
            number += 1
            article.anchor = f"art-{number}"


def _e(text: str) -> str:
    return html.escape(text or "", quote=True)


def _stamp(article) -> str:
    local = article.published.astimezone(_TZ)
    return f"{local.day:02d}/{local.month:02d}/{local.year} {local:%H:%M}"


def _article_html(article) -> str:
    return (f'<div class="article" id="{article.anchor}">\n'
            f"<h2>{_e(article.title)}</h2>\n"
            f'<p class="meta">{_e(article.source)} · {_stamp(article)}</p>\n'
            f"{article.body_html}\n"
            f'<p class="original"><a href="{_e(article.url)}">Leggi l\'originale su '
            f"{_e(article.source)}</a></p>\n</div>")


def _cover_html(edition: Edition) -> str:
    return (f'<div class="cover">\n<h1>{_e(edition.title)}</h1>\n'
            f"<p>{_e(italian_date(edition.when).capitalize())}</p>\n"
            f"<p>{edition.total} articoli</p>\n</div>")


def _first_paragraph(body_html: str) -> str:
    match = re.search(r"<p>(.*?)</p>", body_html, re.S)
    return match.group(0) if match else ""


def _stories_html(edition: Edition) -> str:
    if not edition.stories:
        return "<h1>Le 3 notizie del giorno</h1>\n<p>Nessuna notizia oggi.</p>"
    parts = ["<h1>Le 3 notizie del giorno</h1>"]
    for number, story in enumerate(edition.stories, 1):
        main = story.main
        others = "".join(f'<li><a href="{_e(a.url)}">{_e(a.source)}</a>: {_e(a.title)}</li>'
                         for a in story.others)
        parts.append(
            f'<div class="story">\n<h2>{number}. {_e(main.title)}</h2>\n'
            f'<p class="meta">{_e(main.source)} · {_stamp(main)}</p>\n'
            f"{_first_paragraph(main.body_html)}\n"
            + (f"<p>Ne parlano anche:</p>\n<ul>{others}</ul>\n" if others else "")
            + f'<p><a href="{chapter_file(main.section)}#{main.anchor}">Articolo completo '
              f"nella sezione {_e(main.section)}</a></p>\n</div>")
    return "\n".join(parts)


def _section_html(section: str, articles: list) -> str:
    if not articles:
        return f"<h1>{_e(section)}</h1>\n<p>Nessun articolo oggi.</p>"
    return f"<h1>{_e(section)}</h1>\n" + "\n".join(_article_html(a) for a in articles)


def _chapter(file_name: str, title: str, body: str, css) -> epub.EpubHtml:
    chapter = epub.EpubHtml(title=title, file_name=file_name, lang="it")
    chapter.content = body
    chapter.add_item(css)
    return chapter


def build_epub(edition: Edition, path: str) -> None:
    """Write the EPUB to `path`. Raises on a write failure."""
    assign_anchors(edition)
    date_label = italian_date(edition.when)
    book = epub.EpubBook()
    book.set_identifier(f"david-giornale-{edition.when:%Y-%m-%d}")
    book.set_title(f"{edition.title} — {date_label}")
    book.set_language("it")
    book.add_author("David")

    css = epub.EpubItem(uid="style", file_name="style/giornale.css",
                        media_type="text/css", content=CSS)
    book.add_item(css)

    cover = _chapter("copertina.xhtml", "Copertina", _cover_html(edition), css)
    top = _chapter("tre-notizie.xhtml", "Le 3 notizie del giorno", _stories_html(edition), css)
    chapters = [cover, top]
    toc = [epub.Link(cover.file_name, "Copertina", "copertina"),
           epub.Link(top.file_name, "Le 3 notizie del giorno", "tre-notizie")]
    for section in SECTIONS:
        articles = edition.sections.get(section, [])
        chapter = _chapter(chapter_file(section), section, _section_html(section, articles), css)
        chapters.append(chapter)
        links = [epub.Link(f"{chapter.file_name}#{a.anchor}", a.title, a.anchor) for a in articles]
        toc.append((epub.Section(f"{section} ({len(articles)})", chapter.file_name), links))

    for chapter in chapters:
        book.add_item(chapter)
    book.toc = toc
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]
    epub.write_epub(path, book)


def check_epub(path: str, expected_articles: int) -> str | None:
    """Re-open the written file with ebooklib: None if it reads back whole, else
    what is wrong. Counts the article anchors across the section chapters."""
    try:
        book = epub.read_epub(path)
    except Exception as e:
        return f"the EPUB does not re-open: {type(e).__name__}: {e}"
    names = {item.file_name for item in book.get_items()}
    missing = [chapter_file(s) for s in SECTIONS if chapter_file(s) not in names]
    if missing:
        return f"the EPUB is missing {', '.join(missing)}"
    found = 0
    for section in SECTIONS:
        content = book.get_item_with_href(chapter_file(section)).get_content().decode("utf-8")
        found += len(re.findall(r'class="article" id="art-\d+"', content))
    if found != expected_articles:
        return f"the EPUB holds {found} articles, {expected_articles} were laid out"
    return None
