import asyncio
import logging
import os
import re
import requests
import trafilatura
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from clients.anthropic_client import complete_json
from clients.calendar_client import now_local
from config import (
    ANTHROPIC_TIMEOUT, DEFAULT_LEARN_EMOJI, KNOWLEDGE_RECALL_TYPES, LEARN_TYPES,
    PDF_PARSE_TIMEOUT, SOURCE_FETCH_TIMEOUT, SUMMARY_INPUT_CHARS,
    TAKEAWAYS_HEADING, UNVERIFIED_NOTE,
)
from clients.notion_client import (
    CREATED_DESC, create_page, database_property_type, get_page_title,
    query_database, rich,
    paragraph as _paragraph, heading2 as _heading2, callout as _callout,
    quote as _quote, bullet as _bullet, divider as _divider,
)
from telegram_text import escape_md

logger = logging.getLogger(__name__)

# ─── CONTENT TYPES ─────────────────────────────────────────────────────────────
# The types, their icons and their target databases are declared once, in
# config.LEARN_TYPES. Everything here — what the command accepts, what the
# refusal message lists, what the router advertises in `h` — is derived from it,
# so a type cannot be supported without being documented or documented without
# being supported.
SUPPORTED_TYPES = list(LEARN_TYPES)


# ─── 0. SOURCE IDENTITY ────────────────────────────────────────────────────────
# Learn had no idea it had ever seen a URL before. The same link sent twice — and
# after a transient failure it WILL be sent twice — made a second Notion page and
# paid for a second summarisation. The two pages then have near-identical titles,
# which is precisely what search_page_in_db's `contains` filter cannot choose
# between when Implement goes looking for the source.
#
# The check is cheap (one query) and it happens BEFORE the fetch and before
# Claude, so a duplicate costs one request and nothing else.

# The property David matches on. It is not created by this code: adding a column
# to a database David does not own the schema of is a bigger decision than
# de-duplicating one command. When it is missing, run_learn says so and carries
# on — see the note there.
LEARN_SOURCE_PROPERTY = "Source URL"

# Parameters that identify the CLICK, not the DOCUMENT. Stripped so the copy of a
# link out of a newsletter and the same link off the site match each other.
#
# Conservative on purpose, and it can afford to be: a missed match costs a
# duplicate page, and an over-eager one costs a single ` !` to override — but the
# ones below are unambiguous. Anything a site might use for real routing (`id`,
# `p`, `page`, and YouTube's `t`) is NOT here.
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "gbraid", "wbraid", "msclkid", "dclid", "yclid",
    "mc_cid", "mc_eid", "igshid", "igsh", "si", "twclid", "ttclid",
    "_hsenc", "_hsmi", "vero_id", "vero_conv", "ck_subscriber_id",
    "ref", "ref_src", "referrer", "source", "spm", "cmpid", "ncid",
})

# utm_source, utm_medium, utm_campaign, utm_term, utm_content, and the long tail
# of utm_* that individual platforms invent.
_TRACKING_PREFIXES = ("utm_",)


def normalise_source_url(url: str) -> str:
    """The comparable form of a URL. "" for anything that is not http(s).

    Every transformation here answers "would these two strings have fetched the
    same bytes?", and each one is a way the SAME article arrives looking
    different:

      http → https        the same page, linked from an old post
      Host case, `www.`   the same page, typed by hand
      :80 / :443          the same page, from a tool that spells the port out
      utm_* & friends     the same page, from a newsletter
      #fragment           the same page, linked to one of its headings
      trailing /          the same page, with and without

    Remaining parameters are SORTED rather than dropped: `?v=abc&list=xyz` and
    `?list=xyz&v=abc` are one video, but `?v=abc` and `?v=def` are two, so the
    query cannot simply be discarded.

    The normalised form is what is stored and what is matched. The ORIGINAL url
    is what goes in the page's 🔗 Source link, so nothing a person might want to
    click is lost to this.
    """
    url = (url or "").strip()
    if not url:
        return ""

    try:
        parts = urlsplit(url)
    except ValueError:
        # An unparseable URL is not a URL. Returning "" means "no identity to
        # compare", which is the honest answer and disables the check for it.
        return ""

    if parts.scheme.lower() not in ("http", "https"):
        return ""

    host = parts.hostname or ""
    if host.startswith("www."):
        host = host[4:]
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"

    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
            and not k.lower().startswith(_TRACKING_PREFIXES)]

    path = parts.path.rstrip("/")

    return urlunsplit(("https", host, path, urlencode(sorted(kept)), ""))


def _source_url_filter(prop_type: str, normalised: str) -> dict:
    """The Notion filter for an exact match on the Source URL property.

    Both `url` and `rich_text` are accepted because both are reasonable ways to
    have made this column by hand, and a filter that names the wrong one is a
    Notion 400 — which would arrive as "no duplicate found", the same shape of
    lie search_page_in_db's hardcoded "Name" used to produce.
    """
    key = "url" if prop_type == "url" else "rich_text"
    return {"property": LEARN_SOURCE_PROPERTY, key: {"equals": normalised}}


def find_page_by_source_url(db_id: str, prop_type: str, normalised: str):
    """The most recent page in `db_id` already holding this URL. (page, error).

    (None, None) means there genuinely is not one — the caller may proceed.
    """
    pages, err = query_database(
        db_id,
        filter_obj=_source_url_filter(prop_type, normalised),
        sorts=CREATED_DESC,
    )
    if err:
        return None, err
    return (pages[0] if pages else None), None


def _saved_when(page: dict) -> str:
    """'3 days ago, on 07 August 2026' — from a page's created_time.

    The relative half is what makes the duplicate legible at a glance ("that was
    this morning's failed run" vs "that was last spring"); the absolute half is
    what survives being read a week later. Falls back to whatever Notion sent if
    it cannot be parsed, rather than dropping the only fact the message is for.
    """
    stamp = (page or {}).get("created_time") or ""
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return stamp or "at an unknown time"

    today = now_local()
    when  = when.astimezone(today.tzinfo)
    days  = (today.date() - when.date()).days
    # days < 0 is a clock disagreeing with Notion's, not a page from the future.
    relative = "today" if days <= 0 else "yesterday" if days == 1 else f"{days} days ago"
    return f"{relative}, on {when.strftime('%d %B %Y')}"


# ─── 1. CONTENT EXTRACTION ─────────────────────────────────────────────────────
# Everything from here to the end of section 4 is SYNCHRONOUS and blocking —
# HTTP requests and PyPDF2 parsing. run_learn calls all of it through
# asyncio.to_thread; none of it may be awaited directly from the event loop.

def extract_youtube(url: str) -> tuple[str | None, str | None]:
    """Return (transcript_text, error). Uses Supadata API — no IP ban issues."""
    try:
        supadata_key = os.environ.get("SUPADATA_KEY")
        if not supadata_key:
            return None, "SUPADATA_KEY not set in environment."

        resp = requests.get(
            "https://api.supadata.ai/v1/youtube/transcript",
            headers={"x-api-key": supadata_key},
            params={"url": url, "text": "true"},  # text=true returns plain string directly
            timeout=30,
        )

        if resp.status_code != 200:
            return None, f"Supadata error {resp.status_code}: {resp.text[:200]}"

        data = resp.json()
        text = data.get("content", "")
        if not text:
            return None, "Transcript is empty or unavailable for this video."

        return text, None
    except Exception as e:
        return None, str(e)


# Below this many characters, a trafilatura result is treated as a miss and BS4
# is asked instead. trafilatura is precise by design — it would rather return
# nothing than return boilerplate — and its failure mode on an unusual layout is
# a short fragment (a caption, a standfirst) rather than an empty result, which
# no `is None` check can catch.
#
# The trade is deliberate and it is not free: for a genuinely short page, BS4's
# noisier text wins here. That is the cheaper mistake. A page summarised from its
# own caption produces a confident, wrong Manual entry; a short page summarised
# with some nav text attached produces a slightly padded one.
MIN_ARTICLE_CHARS = 250

# An author is a NAME, or a few of them. Past these bounds it is prose, and prose
# in that field means the metadata parser latched onto the wrong element.
#
# Found live rather than reasoned about, and then found AGAIN when the first
# version of this guard proved superficial. Wikipedia's authority-control footer
# box reaches trafilatura's metadata parser as an author, and it arrives at two
# different lengths from the same template:
#
#   /wiki/Espresso    "Authority control databases International GND National
#                      United States Japan Israel"        81 chars, 10 words
#   /wiki/World_War_II "Authority control databases"       27 chars,  3 words
#
# Size bounds catch the first and wave the second through, because 27 characters
# over 3 words is exactly the shape of a real name. So the bounds are necessary
# and not sufficient, and the discriminator has to be about FORM: a byline is
# title-case ("Jane Doe"), a stray noun phrase is not ("Authority control
# databases" — two lowercase words).
#
# Notion is the source of truth, so this errs toward empty in every ambiguous
# case. An empty Author is visibly empty; a wrong one is a fact you later act on.
# The cost is real and accepted: a byline styled "bell hooks" or "cummings" is
# dropped, which returns that page to the hardcoded "" this field had for its
# whole life before the metadata was wired up.
MAX_AUTHOR_CHARS = 80
MAX_AUTHOR_WORDS = 6

# Name particles are lowercase inside a real name, so they cannot count against
# it: "Ludwig van Beethoven", "Vincent van Gogh", "Charles de Gaulle".
_NAME_PARTICLES = {
    "van", "von", "de", "del", "della", "di", "da", "dos", "du", "la", "le",
    "bin", "ibn", "al", "el", "y", "e", "of", "the", "ter", "ten", "op",
}


def _plausible_author(author: str) -> str:
    """The author if it looks like a byline, "" otherwise. Never guesses for us."""
    author = (author or "").strip()
    if not author or len(author) > MAX_AUTHOR_CHARS or len(author.split()) > MAX_AUTHOR_WORDS:
        return ""

    # Every word that carries letters must be capitalised, particles aside.
    # Initials ("J.") and hyphenated names ("Anne-Marie") pass on their first
    # character; separators between multiple authors ("Jane Doe; John Smith")
    # carry no letters at all and are skipped.
    words = [w for w in re.split(r"[\s;,]+", author) if any(c.isalpha() for c in w)]
    if not words:
        return ""
    if any(w.lower() not in _NAME_PARTICLES and not w.lstrip("('\"").istitle()
           and not w.isupper()
           for w in words):
        return ""
    return author


def _fetch(url: str) -> bytes:
    """The page bytes. Raises requests.RequestException — the caller sorts errors out.

    Bytes, not resp.text: both parsers below detect the encoding from the meta
    tags in the markup, which requests' own guess (headers, then chardet) throws
    away before either of them sees it.
    """
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
    resp.raise_for_status()
    return resp.content


def _extract_trafilatura(html: bytes, url: str) -> dict | None:
    """The good path: article body only. Returns None if it found nothing usable.

    trafilatura scores the DOM for content density, so navigation, cookie
    banners, share widgets and footers do not reach the summariser — which is
    what BS4's get_text() cannot do, since to it every string in the document is
    equally text. Noise is not free here: it is tokens we pay for and context the
    model reasons over on the way to a Manual entry.
    """
    text = trafilatura.extract(html, url=url)
    if not text or len(text) < MIN_ARTICLE_CHARS:
        return None

    # extract_metadata returns a Document even for markup it understood nothing
    # of — never None — so every field on it still has to be treated as optional.
    meta   = trafilatura.extract_metadata(html, default_url=url)
    title  = (getattr(meta, "title", None) or "").strip()
    author = _plausible_author(getattr(meta, "author", None))
    return {"title": title, "author": author, "text": text}


# A tag-like run, for flattening markup a parser handed back as literal text.
# Requires a letter or a slash after the "<" so that a title legitimately reading
# "Why 3 < 5 matters" keeps its "<".
_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")


def _title_from_html(soup: BeautifulSoup) -> str:
    """The document's <title>, normalised. "" when there is not one worth having.

    `<title>Post <em>name</em></title>` is ordinary markup and it reaches us in
    TWO different shapes, which is the whole reason this function exists:

    * As a tag with children, on parsers that treat <title> as normal content.
      `.string` is None for any element with more than one child, so the old
      `.string.strip()` raised AttributeError — swallowed by the broad except in
      extract_article and reported as "could not extract content", a message that
      reads as the site's fault and sent every investigation to the wrong place.
    * As ONE string still containing "<em>name</em>", on parsers that treat
      <title> as RCDATA, which is what the HTML5 spec calls for. Here nothing
      raises and nothing looks wrong — the raw markup just becomes the Notion
      page's name.

    CPython's html.parser changed from the first to the second within 3.12 patch
    releases, so the very same page produced a crash on one machine and a
    polluted title on another. Handling only the shape in front of you is how
    this stays half-fixed. get_text() covers the first, _TAG_RE the second.

    NOT get_text(strip=True), which is the obvious spelling and is wrong here: it
    strips each string BEFORE joining them, so the tag-with-children shape comes
    back as "Postname". Splitting on whitespace and rejoining keeps the word
    boundary the markup implies while still collapsing the newlines and runs of
    spaces that titles laid out over several lines carry.
    """
    if not soup.title:
        return ""
    return " ".join(_TAG_RE.sub(" ", soup.title.get_text()).split())


def _extract_bs4(html: bytes, url: str) -> dict:
    """The fallback: strip the obvious chrome, then take every remaining string."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()

    return {"title": _title_from_html(soup), "author": "",
            "text": soup.get_text(separator="\n", strip=True)}


def extract_article(url: str) -> tuple[dict | None, str | None]:
    """Return ({"title", "author", "text"}, error). trafilatura first, BS4 second.

    This USED to open with a `from newspaper import Article` branch, described as
    the good path with BS4 as its fallback. newspaper is not in requirements.txt
    — it was taken out deliberately, because it pulls lxml, which compiled from C
    source — so the import raised ImportError on every call, the bare `except`
    swallowed it, and BS4 did the work every time. The branch documented an
    extraction quality nothing was delivering.

    So trafilatura is imported at MODULE scope and unguarded. It is a declared
    dependency; wrapping it in try/except ImportError is the exact construction
    that produced that bug, and it converts a build problem — loud, at deploy,
    fixable — into a permanent silent downgrade nobody can see from the outside.

    The text is returned WHOLE. Truncating to fit the summariser is run_learn's
    job (see config.SUMMARY_INPUT_CHARS) because it is the only place that can
    tell you it happened.
    """
    try:
        html = _fetch(url)

        result = _extract_trafilatura(html, url)
        if result is None:
            logger.info("extract_article: trafilatura found nothing usable for %s, "
                        "falling back to BeautifulSoup", url)
            result = _extract_bs4(html, url)
        else:
            logger.info("extract_article: trafilatura extracted %d chars from %s",
                        len(result["text"]), url)

        # The title falls back on its own, independently of the body: trafilatura
        # can score the article perfectly and still hand back no title, and the
        # page's own <title> is a far better Notion page name than a raw URL.
        # A missing title is never worth discarding a good body extraction over,
        # which is why this is a fallback chain and not an error.
        if not result["title"]:
            result["title"] = _title_from_html(BeautifulSoup(html, "html.parser")) or url
        return result, None

    except requests.RequestException as e:
        # The genuinely-their-fault case: DNS, TLS, a timeout, a 403 blocking the
        # scraper. Named as a fetch problem because that is what it is.
        return None, f"Could not fetch the page: {e}"
    except Exception as e:
        # OURS. A service may not raise across a module boundary, so this stays
        # broad — but it no longer wears the fetch failure's clothes. The type
        # name goes in the message and the traceback goes to the log, because the
        # nested-<title> AttributeError above spent its whole life being reported
        # as a problem with the website.
        logger.exception("extract_article: internal failure on %s", url)
        return None, f"Internal extraction error ({type(e).__name__}): {e}"


def extract_pdf(file_bytes: bytes) -> tuple[str | None, str | None]:
    """Return (text, error). Receives raw bytes from a Telegram document message."""
    try:
        import io
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
        pages  = [page.extract_text() or "" for page in reader.pages]
        return "\n".join(pages), None
    except Exception as e:
        return None, str(e)


# ─── 2. CLAUDE SUMMARIZATION ───────────────────────────────────────────────────

_SYSTEM = """You are an expert summarizer building a personal knowledge base in Notion.

Rules:
- Depth is proportional to content length: short article → 2-3 sections, long video → 5-7 sections.
- Key takeaways must be specific and actionable, not vague ("Apply X by doing Y", not "X is important").
- Sections should each cover a distinct theme — no redundancy."""

# The shape _SYSTEM used to describe in prose. Declaring it as a schema means the
# model fills in fields rather than writing JSON we then have to find and parse.
_SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title":  {"type": "string", "description": "Note title; infer from content if not obvious."},
        "author": {"type": "string", "description": "Author or creator name, or empty string."},
        "tldr":   {"type": "string",
                   "description": "2-4 sentence overview: what it is, who made it, core message."},
        "sections": {
            "type": "array",
            "description": "One entry per distinct theme.",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "content": {"type": "string",
                                "description": "2-4 paragraphs summarising this theme."},
                    "quotes":  {"type": "array", "items": {"type": "string"},
                                "description": "Verbatim quotes worth preserving."},
                },
                "required": ["heading", "content"],
            },
        },
        "key_takeaways": {"type": "array", "items": {"type": "string"},
                          "description": "Specific, actionable insights."},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "tldr"],
}


def summarize_with_claude(content_type: str, text: str, title: str = "", source: str = "") -> tuple[dict | None, str | None]:
    """Summarise a source into the Learn page structure. Returns (summary, error)."""
    user_msg = (
        f"Content type: {content_type}\n"
        f"Title (if known): {title}\n"
        f"Source: {source}\n\n"
        # A backstop, not the truncation point. run_learn has already cut the
        # text to this budget AND told the user it did; this guard only ensures a
        # future caller that forgets cannot blow the input cap. If it ever fires,
        # it fires silently — which is exactly why it is not the primary place.
        f"Content:\n{text[:SUMMARY_INPUT_CHARS]}"
    )
    return complete_json(_SYSTEM, user_msg, _SUMMARY_SCHEMA, max_tokens=4096)


# ─── 3. NOTION BLOCK BUILDER ───────────────────────────────────────────────────
# Basic builders (_paragraph, _heading2, _callout, _quote, _bullet, _divider)
# are imported from notion_client. Only the link-bearing source builder is local.

def _source_link(url: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": "🔗 Source: "}},
                {"type": "text", "text": {"content": url, "link": {"url": url}}},
            ]}}


def build_notion_blocks(summary: dict, source: str, content_type: str = "") -> list[dict]:
    """The page body. `content_type` decides whether it opens with a warning.

    The unverified callout goes FIRST — above the TL;DR, which is the one part of
    the page anybody reads in a hurry, and above the source link, which is absent
    on exactly the pages that need the warning.
    """
    blocks: list[dict] = []

    # ── The unverified-source marker ───────────────────────────────────────────
    # A knowledge-recall page has no source text: `Learn book X` asks Claude to
    # summarise from memory, and the answer is filed next to pages built from a
    # real transcript. Nothing distinguished the two, and this content flows on
    # into a Manual through Implement as though it had been read somewhere.
    #
    # red_background, not the default blue: this is the one callout on the page
    # that is not information about the subject.
    if content_type in KNOWLEDGE_RECALL_TYPES:
        blocks.append(_callout(UNVERIFIED_NOTE, "⚠️", "red_background"))

    # TL;DR callout
    if summary.get("tldr"):
        blocks.append(_callout(summary["tldr"], "💡"))

    # Source link
    if source and source.startswith("http"):
        blocks.append(_source_link(source))

    blocks.append(_divider())

    # Sections
    for section in summary.get("sections", []):
        if section.get("heading"):
            blocks.append(_heading2(section["heading"]))
        if section.get("content"):
            blocks.append(_paragraph(section["content"]))
        for q in section.get("quotes", []):
            if q:
                blocks.append(_quote(q))

    blocks.append(_divider())

    # Key takeaways
    takeaways = summary.get("key_takeaways", [])
    if takeaways:
        # The constant, not the literal: proactive/takeaway.py finds this section
        # by matching that same string.
        blocks.append(_heading2(TAKEAWAYS_HEADING))
        for t in takeaways:
            blocks.append(_bullet(t))

    return blocks


# ─── 4. NOTION PAGE CREATOR ────────────────────────────────────────────────────

def _emoji(content_type: str) -> str:
    """The page icon for a type — also the one used in the Telegram confirmation."""
    learn_type = LEARN_TYPES.get(content_type)
    return learn_type.emoji if learn_type else DEFAULT_LEARN_EMOJI


def _get_db_id(content_type: str) -> str | None:
    """The database this type is filed in, read from the environment by name."""
    learn_type = LEARN_TYPES.get(content_type)
    return os.environ.get(learn_type.db_env) if learn_type else None


def create_learn_page(content_type: str, title: str, blocks: list[dict],
                      metadata: dict | None = None) -> tuple[bool, str, str | None]:
    """Create a Notion page. Returns (success, page_id_or_error, incomplete).

    THREE outcomes, because Notion writes have three (see create_page):

        (True,  page_id, None)   the page and all of its content are there
        (True,  page_id, why)    the page is there and is MISSING CONTENT
        (False, error,   None)   nothing was created

    The middle one used to come back as the first: create_page returns the page
    id together with the append error, and `if not page_id` cannot see the
    difference — so a page holding its first 100 blocks reported a clean save.

    `metadata` defaults to None, not {}: a mutable default is built ONCE, at
    definition time, and shared by every call that omits the argument — so
    anything that ever wrote to it would be writing into the function itself.
    """
    metadata = metadata or {}

    db_id = _get_db_id(content_type)
    if not db_id:
        return False, f"No Notion database configured for type '{content_type}'.", None

    properties: dict = {
        "Name": {"title": [{"text": {"content": title[:2000]}}]},
    }
    # Add Author if the target DB has that field (books/articles)
    author = metadata.get("author", "")
    if author and content_type in ("book", "article"):
        properties["Author"] = {"rich_text": [{"text": {"content": author[:500]}}]}

    # The de-duplication key, written only when the column exists and the caller
    # resolved its type. A property Notion does not know about is a 400 on the
    # CREATE — which would turn "your database has no Source URL column yet" into
    # "Learn is broken", so the absence disables the write rather than causing it.
    source_url  = metadata.get("source_url", "")
    source_type = metadata.get("source_property_type", "")
    if source_url and source_type:
        properties[LEARN_SOURCE_PROPERTY] = (
            {"url": source_url} if source_type == "url" else {"rich_text": rich(source_url)}
        )

    # Shared create_page handles the >100-block batching and retries internally
    page_id, err = create_page(
        db_id,
        properties,
        children=blocks,
        icon=_emoji(content_type),
    )
    if not page_id:
        return False, err or "Unknown error creating page.", None
    return True, page_id, err


# ─── 5. MAIN HANDLER ───────────────────────────────────────────────────────────

# The override for the duplicate check: a bang as its own trailing token.
#
# `\s+!$` and not `!$`, so `Learn book Wow!` is a book called "Wow!" and not a
# forced re-run of one called "Wow". The same lesson `Remind`'s `t` lookahead
# carries: a token that can swallow the end of a real value is a bug waiting for
# the one input that ends that way.
_FORCE_TOKEN = re.compile(r"\s+!$")


def _strip_force(source: str) -> tuple[str, bool]:
    """('https://…', True) if the source ends in a bare `!`, stripped."""
    if _FORCE_TOKEN.search(source):
        return _FORCE_TOKEN.sub("", source).strip(), True
    return source, False


async def _check_for_duplicate(content_type: str, normalised: str, *, notify, notify_md):
    """Has this URL been learned already? Returns (existing_page, property_type).

    Every failure here DEGRADES rather than blocks, and says so:

      no database configured   silent — create_learn_page reports it properly
      property does not exist  said once; the check AND the write are skipped
      the query failed         said, naming the duplicate it could not rule out

    Refusing to Learn because a duplicate check could not run would make an
    optional safeguard into a hard dependency on a column that may not exist yet.
    The cost of degrading is one duplicate page; the cost of refusing is the
    command.
    """
    db_id = _get_db_id(content_type)
    if not db_id:
        return None, ""

    prop_type, err = await asyncio.to_thread(
        database_property_type, db_id, LEARN_SOURCE_PROPERTY)
    if err:
        await notify(f"⚠️ Could not check for duplicates: {err}\n"
                     "Continuing — this may create a second page for the same source.")
        return None, ""
    if not prop_type:
        await notify_md(
            f"⚠️ No `{LEARN_SOURCE_PROPERTY}` property in this database, so I cannot "
            "tell whether this URL is already saved.\n"
            f"Add a `{LEARN_SOURCE_PROPERTY}` property (type *URL*) to switch duplicate "
            "detection on.")
        return None, ""

    existing, err = await asyncio.to_thread(
        find_page_by_source_url, db_id, prop_type, normalised)
    if err:
        await notify(f"⚠️ Could not check for duplicates: {err}\n"
                     "Continuing — this may create a second page for the same source.")
        # The property type is still good: the SCHEMA read succeeded, only the
        # query failed. Returning it keeps the new page's Source URL written, so
        # one failed check does not also cost the next run its match.
        return None, prop_type

    return existing, prop_type


async def run_learn(user_text: str, file_bytes: bytes | None = None,
                    *, notify, notify_md=None):
    """
    Entry point. `bot/learn.py` binds the callbacks; nothing here sends.

    Supported commands:
      Learn video   https://youtube.com/watch?v=...
      Learn article https://example.com/post
      Learn podcast https://show.com/episode
      Learn book    Atomic Habits          ← summarised from Claude's knowledge
      Learn pdf     <send a PDF file>      ← attach file, send "Learn pdf" as caption

    A trailing ` !` on any of them re-summarises a source already in Notion
    instead of stopping to ask.
    """
    notify_md = notify_md or notify

    # ── Parse command ──────────────────────────────────────────────────────────
    match = re.match(r"(?i)learn\s+(\w+)(?:\s+(.+))?", user_text.strip())
    if not match:
        await notify_md("📚 *Learn command usage:*\n"
            "• `Learn video https://youtu.be/...`\n"
            "• `Learn article https://...`\n"
            "• `Learn podcast https://...`\n"
            "• `Learn book Atomic Habits`\n"
            "• `Learn pdf` _(attach a PDF file as caption)_",
        )
        return

    content_type  = match.group(1).lower()
    source, force = _strip_force((match.group(2) or "").strip())

    if content_type not in SUPPORTED_TYPES:
        # content_type comes from a \w+ group, so it cannot contain a backtick and
        # is safe inside the code span.
        await notify_md(f"❌ Unknown type `{content_type}`. Supported: {', '.join(SUPPORTED_TYPES)}",
        )
        return

    # ── Have we already learned this? ──────────────────────────────────────────
    # BEFORE the fetch and before Claude, which is the whole point: a duplicate
    # costs one Notion query instead of a scrape and a summarisation.
    #
    # Only URL sources have an identity to compare. A book title and a PDF upload
    # do not, so they skip this entirely rather than being matched on something
    # weaker — see PLAN.md.
    normalised   = normalise_source_url(source)
    source_property_type = ""
    if normalised:
        existing, source_property_type = await _check_for_duplicate(
            content_type, normalised, notify=notify, notify_md=notify_md)
        if existing is not None and not force:
            # Notion's title and the user's own URL — both data, both escaped, and
            # NEITHER inside a code span. A backslash escape is literal text
            # inside `code` in Markdown v1 (which is why the error reporters send
            # plain), so a code span here would either show the backslashes or,
            # unescaped, break on the first `_` in a URL. The command line is
            # escaped plain text: Telegram renders it back as typed, so it is
            # still copy-pasteable.
            await notify_md(
                f"📎 *Already saved* — nothing was fetched and nothing was summarised.\n\n"
                f"{_emoji(content_type)} *{escape_md(get_page_title(existing))}*\n"
                f"🗓️ Saved {escape_md(_saved_when(existing))}\n"
                f"🔗 Matched on: {escape_md(normalised)}\n\n"
                f"To summarise it again anyway, put a `!` on the end:\n"
                f"{escape_md(f'Learn {content_type} {source} !')}")
            return
        if existing is not None and force:
            await notify("🔁 Already saved — re-summarising as asked. "
                         "This will create a second page.")

    # ── Extract raw text ───────────────────────────────────────────────────────
    await notify(f"⏳ Fetching {content_type}…")

    text    = ""
    title   = ""
    author  = ""

    if content_type == "video":
        if not source.startswith("http"):
            await notify("❌ Please provide a YouTube URL.")
            return
        try:
            text, err = await asyncio.wait_for(
                asyncio.to_thread(extract_youtube, source),
                timeout=SOURCE_FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await notify(
                f"❌ Fetching the transcript timed out after {SOURCE_FETCH_TIMEOUT}s.\n"
                "Supadata may be slow or down — try again in a minute."
            )
            return
        if err:
            await notify(f"❌ Could not get transcript: {err}\n\nTip: paste the transcript manually.")
            return
        title = source

    elif content_type in ("article", "podcast"):
        if not source.startswith("http"):
            await notify("❌ Please provide a URL.")
            return
        # extract_article does its own fetch with a per-request timeout, but that
        # is per socket read and restarts on every byte. This outer cap is what
        # bounds the whole operation — fetch plus both parsers — for a server
        # trickling one byte at a time.
        try:
            result, err = await asyncio.wait_for(
                asyncio.to_thread(extract_article, source),
                timeout=SOURCE_FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await notify(
                f"❌ Fetching that page timed out after {SOURCE_FETCH_TIMEOUT}s.\n"
                "The site may be slow or blocking us."
            )
            return
        if err:
            await notify(f"❌ Could not extract content: {err}")
            return
        text   = result["text"]
        title  = result["title"]
        author = result["author"]

    elif content_type == "book":
        if not source:
            await notify_md("❌ Provide the book title: `Learn book Atomic Habits`")
            return
        # No scraping needed — Claude summarises from its own knowledge
        text  = f"Please summarise the book: {source}"
        title = source
        await notify("📖 Summarising from knowledge base…")

    elif content_type == "pdf":
        if file_bytes is None:
            await notify_md("❌ Attach a PDF file and use `Learn pdf` as the *caption*.")
            return
        # PyPDF2 walks every page; on a long book that is seconds to minutes of
        # pure CPU, which would pin the event loop just as hard as a network call.
        try:
            text, err = await asyncio.wait_for(
                asyncio.to_thread(extract_pdf, file_bytes),
                timeout=PDF_PARSE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await notify(
                f"❌ Reading that PDF timed out after {PDF_PARSE_TIMEOUT}s.\n"
                "Try a shorter document."
            )
            return
        if err:
            await notify(f"❌ Could not read PDF: {err}")
            return
        title = source or "PDF Document"

    if not text:
        await notify("❌ No content could be extracted.")
        return

    # ── Fit the summariser's budget, out loud ──────────────────────────────────
    # THE ONLY PLACE SOURCE TEXT IS CUT, and it covers every type — a long
    # article, a 3-hour transcript and a 400-page PDF all reach the same cap.
    # Silence here is the whole defect: a summary built from an eighth of an
    # article is indistinguishable, in the reply and on the page, from one built
    # from all of it. You find out from a thin Manual entry, months later, with
    # no way to tell which entries were affected.
    if len(text) > SUMMARY_INPUT_CHARS:
        logger.info("run_learn: %s source is %d chars, truncating to %d",
                    content_type, len(text), SUMMARY_INPUT_CHARS)
        # Plain notify: this string is David's own and interpolates only
        # integers, so there is nothing to escape and no reason to risk a
        # parse_mode rejection on the message that exists to warn you.
        await notify(
            f"⚠️ Source is {len(text):,} characters; summarising the first "
            f"{SUMMARY_INPUT_CHARS:,}. This summary is partial."
        )
        text = text[:SUMMARY_INPUT_CHARS]

    # ── Claude summarization ───────────────────────────────────────────────────
    await notify("🧠 Claude is reading and summarising…")

    # THE call this whole change exists for: a long transcript can hold Claude
    # for minutes, and until now that froze every other command and every
    # scheduled job for the duration.
    try:
        summary, err = await asyncio.wait_for(
            asyncio.to_thread(summarize_with_claude, content_type, text, title, source),
            timeout=ANTHROPIC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await notify(
            f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
            "Nothing was saved — try again, or use a shorter source."
        )
        return
    if err:
        await notify(f"❌ Summarization failed: {err}")
        return

    final_title  = summary.get("title") or title or source[:80]
    final_author = summary.get("author") or author

    # ── Build Notion blocks ────────────────────────────────────────────────────
    blocks = build_notion_blocks(summary, source, content_type)

    # ── Save to Notion ─────────────────────────────────────────────────────────
    await notify("📝 Saving to Notion…")

    # No wait_for: this WRITES. A wait_for cannot cancel the worker thread, so
    # timing out here would report failure while the page was still being
    # created. notion_request already bounds each request and its retries.
    ok, result, incomplete = await asyncio.to_thread(
        create_learn_page,
        content_type, final_title, blocks,
        metadata={"author": final_author,
                  "source_url": normalised,
                  "source_property_type": source_property_type},
    )

    if not ok:
        await notify(f"❌ Could not save to Notion: {result}")
        return

    # Both values are Claude's prose (final_title falls back to a raw URL), so
    # both are escaped — a title with an underscore used to lose the whole
    # confirmation even though the page had been written.
    tldr_preview = summary.get("tldr", "")[:220]
    headline = "⚠️ *Saved partially*" if incomplete else "✅ Saved to Notion!"
    message = (f"{headline}\n\n"
               f"{_emoji(content_type)} *{escape_md(final_title)}*\n\n"
               f"💡 {escape_md(tldr_preview)}")
    await notify_md(message)

    if incomplete:
        # Plain, and a SECOND message: this one carries a raw Notion error, which
        # cannot be made Markdown-safe (see telegram_text) — and it is the half
        # that must arrive even if the first is rejected.
        await notify(f"⚠️ The page exists but is missing content: {incomplete}\n"
                     "Re-running Learn will create a SECOND page rather than "
                     "completing this one — delete this page first.")

    if content_type in KNOWLEDGE_RECALL_TYPES:
        await notify("⚠️ No source was read for this one — it is Claude's "
                     "recollection of the work, and the page says so at the top. "
                     "Treat quotes, figures and dates as unverified.")
