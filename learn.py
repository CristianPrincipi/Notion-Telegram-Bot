import asyncio
import os
import re
import requests
from bs4 import BeautifulSoup

from anthropic_client import complete_json
from config import (
    ANTHROPIC_TIMEOUT,
    PDF_PARSE_TIMEOUT, SOURCE_FETCH_TIMEOUT,
)
from notion_client import (
    create_page,
    paragraph as _paragraph, heading2 as _heading2, callout as _callout,
    quote as _quote, bullet as _bullet, divider as _divider,
)
from telegram_text import escape_md, reply

# ─── ENV ───────────────────────────────────────────────────────────────────────
LEARN_ID = os.environ.get("LEARN_ID")                  # videos, articles, podcasts
LETTI_ID = os.environ.get("LETTI_ID")                  # books  (already exists in David)

SUPPORTED_TYPES = ["video", "article", "book", "podcast", "pdf"]

TYPE_EMOJI = {
    "video":   "🎬",
    "article": "📰",
    "book":    "📚",
    "podcast": "🎙️",
    "pdf":     "📄",
}


# ─── 1. CONTENT EXTRACTION ─────────────────────────────────────────────────────
# Everything from here to the end of section 4 is SYNCHRONOUS and blocking —
# HTTP requests and PyPDF2 parsing. handle_learn calls all of it through
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


def extract_article(url: str) -> tuple[dict | None, str | None]:
    """Return ({"title", "author", "text"}, error). Uses newspaper3k if available, falls back to BS4."""
    try:
        from newspaper import Article
        art = Article(url)
        art.download()
        art.parse()
        return {
            "title":  art.title or url,
            "author": ", ".join(art.authors) if art.authors else "",
            "text":   art.text,
        }, None
    except Exception:
        pass

    # Fallback: raw requests + BeautifulSoup
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()
        title = soup.title.string.strip() if soup.title else url
        text  = soup.get_text(separator="\n", strip=True)
        return {"title": title, "author": "", "text": text[:12000]}, None
    except Exception as e:
        return None, str(e)


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
        f"Content:\n{text[:100000]}"  # 100k chars ≈ covers a 2-hour video in one API call
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

def _get_db_id(content_type: str) -> str | None:
    return {
        "video":   LEARN_ID,
        "article": LEARN_ID,
        "podcast": LEARN_ID,
        "pdf":     LEARN_ID,
        "book":    LETTI_ID,
    }.get(content_type)


def create_learn_page(content_type: str, title: str, blocks: list[dict], metadata: dict = {}) -> tuple[bool, str]:
    """Create a Notion page. Returns (success, page_id_or_error)."""
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
        icon=TYPE_EMOJI.get(content_type, "📖"),
    )
    if not page_id:
        return False, err or "Unknown error creating page."
    return True, page_id


# ─── 5. MAIN HANDLER ───────────────────────────────────────────────────────────

async def handle_learn(update, user_text: str, file_bytes: bytes | None = None):
    """
    Entry point called from david.py handle_message.

    Supported commands:
      Learn video   https://youtube.com/watch?v=...
      Learn article https://example.com/post
      Learn podcast https://show.com/episode
      Learn book    Atomic Habits          ← summarised from Claude's knowledge
      Learn pdf     <send a PDF file>      ← attach file, send "Learn pdf" as caption
    """

    # ── Parse command ──────────────────────────────────────────────────────────
    match = re.match(r"(?i)learn\s+(\w+)(?:\s+(.+))?", user_text.strip())
    if not match:
        await reply(
            update,
            "📚 *Learn command usage:*\n"
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
        await reply(
            update,
            f"❌ Unknown type `{content_type}`. Supported: {', '.join(SUPPORTED_TYPES)}",
        )
        return

    # ── Extract raw text ───────────────────────────────────────────────────────
    await update.message.reply_text(f"⏳ Fetching {content_type}…")

    text    = ""
    title   = ""
    author  = ""

    if content_type == "video":
        if not source.startswith("http"):
            await update.message.reply_text("❌ Please provide a YouTube URL.")
            return
        try:
            text, err = await asyncio.wait_for(
                asyncio.to_thread(extract_youtube, source),
                timeout=SOURCE_FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"❌ Fetching the transcript timed out after {SOURCE_FETCH_TIMEOUT}s.\n"
                "Supadata may be slow or down — try again in a minute."
            )
            return
        if err:
            await update.message.reply_text(f"❌ Could not get transcript: {err}\n\nTip: paste the transcript manually.")
            return
        title = source

    elif content_type in ("article", "podcast"):
        if not source.startswith("http"):
            await update.message.reply_text("❌ Please provide a URL.")
            return
        # The newspaper3k branch of extract_article calls download() with no
        # timeout of its own, so this outer cap is the only bound on it.
        try:
            result, err = await asyncio.wait_for(
                asyncio.to_thread(extract_article, source),
                timeout=SOURCE_FETCH_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"❌ Fetching that page timed out after {SOURCE_FETCH_TIMEOUT}s.\n"
                "The site may be slow or blocking us."
            )
            return
        if err:
            await update.message.reply_text(f"❌ Could not extract content: {err}")
            return
        text   = result["text"]
        title  = result["title"]
        author = result["author"]

    elif content_type == "book":
        if not source:
            await reply(update, "❌ Provide the book title: `Learn book Atomic Habits`")
            return
        # No scraping needed — Claude summarises from its own knowledge
        text  = f"Please summarise the book: {source}"
        title = source
        await update.message.reply_text("📖 Summarising from knowledge base…")

    elif content_type == "pdf":
        if file_bytes is None:
            await reply(update, "❌ Attach a PDF file and use `Learn pdf` as the *caption*.")
            return
        # PyPDF2 walks every page; on a long book that is seconds to minutes of
        # pure CPU, which would pin the event loop just as hard as a network call.
        try:
            text, err = await asyncio.wait_for(
                asyncio.to_thread(extract_pdf, file_bytes),
                timeout=PDF_PARSE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            await update.message.reply_text(
                f"❌ Reading that PDF timed out after {PDF_PARSE_TIMEOUT}s.\n"
                "Try a shorter document."
            )
            return
        if err:
            await update.message.reply_text(f"❌ Could not read PDF: {err}")
            return
        title = source or "PDF Document"

    if not text:
        await update.message.reply_text("❌ No content could be extracted.")
        return

    # ── Claude summarization ───────────────────────────────────────────────────
    await update.message.reply_text("🧠 Claude is reading and summarising…")

    # THE call this whole change exists for: a long transcript can hold Claude
    # for minutes, and until now that froze every other command and every
    # scheduled job for the duration.
    try:
        summary, err = await asyncio.wait_for(
            asyncio.to_thread(summarize_with_claude, content_type, text, title, source),
            timeout=ANTHROPIC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
            "Nothing was saved — try again, or use a shorter source."
        )
        return
    if err:
        await update.message.reply_text(f"❌ Summarization failed: {err}")
        return

    final_title  = summary.get("title") or title or source[:80]
    final_author = summary.get("author") or author

    # ── Build Notion blocks ────────────────────────────────────────────────────
    blocks = build_notion_blocks(summary, source)

    # ── Save to Notion ─────────────────────────────────────────────────────────
    await update.message.reply_text("📝 Saving to Notion…")

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
        await reply(
            update,
            f"✅ Saved to Notion!\n\n"
            f"{TYPE_EMOJI.get(content_type, '📖')} *{escape_md(final_title)}*\n\n"
            f"💡 {escape_md(tldr_preview)}",
        )
    else:
        await update.message.reply_text(f"❌ Could not save to Notion: {result}")
