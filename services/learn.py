import asyncio
import logging
import os
import re
import requests
import trafilatura
from bs4 import BeautifulSoup

from clients.anthropic_client import complete_json
from config import (
    ANTHROPIC_TIMEOUT, DEFAULT_LEARN_EMOJI, LEARN_TYPES,
    PDF_PARSE_TIMEOUT, SOURCE_FETCH_TIMEOUT, SUMMARY_INPUT_CHARS,
)
from clients.notion_client import (
    create_page,
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
    author = (getattr(meta, "author", None) or "").strip()
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


def build_notion_blocks(summary: dict, source: str) -> list[dict]:
    blocks: list[dict] = []

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
        blocks.append(_heading2("✅ Key Takeaways"))
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
                      metadata: dict | None = None) -> tuple[bool, str]:
    """Create a Notion page. Returns (success, page_id_or_error).

    `metadata` defaults to None, not {}: a mutable default is built ONCE, at
    definition time, and shared by every call that omits the argument — so
    anything that ever wrote to it would be writing into the function itself.
    """
    metadata = metadata or {}

    db_id = _get_db_id(content_type)
    if not db_id:
        return False, f"No Notion database configured for type '{content_type}'."

    properties: dict = {
        "Name": {"title": [{"text": {"content": title[:2000]}}]},
    }
    # Add Author if the target DB has that field (books/articles)
    author = metadata.get("author", "")
    if author and content_type in ("book", "article"):
        properties["Author"] = {"rich_text": [{"text": {"content": author[:500]}}]}

    # Shared create_page handles the >100-block batching and retries internally
    page_id, err = create_page(
        db_id,
        properties,
        children=blocks,
        icon=_emoji(content_type),
    )
    if not page_id:
        return False, err or "Unknown error creating page."
    return True, page_id


# ─── 5. MAIN HANDLER ───────────────────────────────────────────────────────────

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

    content_type = match.group(1).lower()
    source       = (match.group(2) or "").strip()

    if content_type not in SUPPORTED_TYPES:
        # content_type comes from a \w+ group, so it cannot contain a backtick and
        # is safe inside the code span.
        await notify_md(f"❌ Unknown type `{content_type}`. Supported: {', '.join(SUPPORTED_TYPES)}",
        )
        return

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
    blocks = build_notion_blocks(summary, source)

    # ── Save to Notion ─────────────────────────────────────────────────────────
    await notify("📝 Saving to Notion…")

    # No wait_for: this WRITES. A wait_for cannot cancel the worker thread, so
    # timing out here would report failure while the page was still being
    # created. notion_request already bounds each request and its retries.
    ok, result = await asyncio.to_thread(
        create_learn_page,
        content_type, final_title, blocks,
        metadata={"author": final_author},
    )

    if ok:
        # Both values are Claude's prose (final_title falls back to a raw URL), so
        # both are escaped — a title with an underscore used to lose the whole
        # confirmation even though the page had been written.
        tldr_preview = summary.get("tldr", "")[:220]
        await notify_md(f"✅ Saved to Notion!\n\n"
            f"{_emoji(content_type)} *{escape_md(final_title)}*\n\n"
            f"💡 {escape_md(tldr_preview)}",
        )
    else:
        await notify(f"❌ Could not save to Notion: {result}")
