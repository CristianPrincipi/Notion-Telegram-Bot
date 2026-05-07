import os
import re
import json
import requests
from bs4 import BeautifulSoup

# ─── ENV ───────────────────────────────────────────────────────────────────────
NOTION_KEY = os.environ.get("NOTION_KEY")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
LEARN_ID = os.environ.get("LEARN_ID")                  # videos, articles, podcasts
LETTI_ID = os.environ.get("LETTI_ID")                  # books  (already exists in David)

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

SUPPORTED_TYPES = ["video", "article", "book", "podcast", "pdf"]

TYPE_EMOJI = {
    "video":   "🎬",
    "article": "📰",
    "book":    "📚",
    "podcast": "🎙️",
    "pdf":     "📄",
}


# ─── 1. CONTENT EXTRACTION ─────────────────────────────────────────────────────

def extract_youtube(url: str) -> tuple[str | None, str | None]:
    """Return (transcript_text, error). Uses youtube-transcript-api."""
    try:
        from youtube_transcript_api import YouTubeTranscriptApi

        vid_match = re.search(r"(?:v=|youtu\.be/)([^&\n?#]+)", url)
        if not vid_match:
            return None, "Could not parse video ID from URL."

        vid_id = vid_match.group(1)
        api = YouTubeTranscriptApi()
        transcript = api.fetch(vid_id, languages=["en", "it", "en-US", "it-IT"])
        text = " ".join(snippet.text for snippet in transcript)
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

Return ONLY a valid JSON object — no markdown, no preamble — with this exact structure:
{
  "title":         "Note title (infer from content if not obvious)",
  "author":        "Author or creator name, or empty string",
  "tldr":          "2-4 sentence overview: what it is, who made it, core message",
  "sections": [
    {
      "heading": "Section title",
      "content": "2-4 paragraphs summarising this theme",
      "quotes":  ["Optional verbatim quote worth preserving"]
    }
  ],
  "key_takeaways": ["Actionable insight 1", "Actionable insight 2", "Actionable insight 3"],
  "tags":          ["tag1", "tag2"]
}

Rules:
- Depth is proportional to content length: short article → 2-3 sections, long video → 5-7 sections.
- Key takeaways must be specific and actionable, not vague ("Apply X by doing Y", not "X is important").
- Sections should each cover a distinct theme — no redundancy.
- Do NOT wrap in markdown code fences. Return raw JSON only."""


def summarize_with_claude(content_type: str, text: str, title: str = "", source: str = "") -> tuple[dict | None, str | None]:
    """Call Claude API, return (summary_dict, error)."""
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY is not set in environment."

    user_msg = (
        f"Content type: {content_type}\n"
        f"Title (if known): {title}\n"
        f"Source: {source}\n\n"
        f"Content (truncated to 14 000 chars):\n{text[:14000]}"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key":         ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      "claude-sonnet-4-5",
                "max_tokens": 2048,
                "system":     _SYSTEM,
                "messages":   [{"role": "user", "content": user_msg}],
            },
            timeout=90,
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()

        # Strip accidental ```json fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)

        return json.loads(raw), None

    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    except Exception as e:
        return None, str(e)


# ─── 3. NOTION BLOCK BUILDER ───────────────────────────────────────────────────

def _paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def _heading2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def _callout(text: str, emoji: str = "💡", color: str = "blue_background") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": [{"type": "text", "text": {"content": text}}],
                        "icon": {"emoji": emoji}, "color": color}}

def _quote(text: str) -> dict:
    return {"object": "block", "type": "quote",
            "quote": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def _bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def _divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}

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

    page_body = {
        "parent":     {"database_id": db_id},
        "icon":       {"emoji": TYPE_EMOJI.get(content_type, "📖")},
        "properties": properties,
        "children":   blocks[:100],          # Notion API limit: 100 blocks per request
    }

    resp = requests.post("https://api.notion.com/v1/pages", headers=NOTION_HEADERS, json=page_body)
    if resp.status_code != 200:
        return False, f"Notion {resp.status_code}: {resp.text[:300]}"

    page_id = resp.json()["id"]

    # Append remaining blocks in batches of 100
    remaining = blocks[100:]
    while remaining:
        batch, remaining = remaining[:100], remaining[100:]
        requests.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json={"children": batch},
        )

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
        await update.message.reply_text(
            "📚 *Learn command usage:*\n"
            "• `Learn video https://youtu.be/...`\n"
            "• `Learn article https://...`\n"
            "• `Learn podcast https://...`\n"
            "• `Learn book Atomic Habits`\n"
            "• `Learn pdf` _(attach a PDF file as caption)_",
            parse_mode="Markdown",
        )
        return

    content_type = match.group(1).lower()
    source       = (match.group(2) or "").strip()

    if content_type not in SUPPORTED_TYPES:
        await update.message.reply_text(
            f"❌ Unknown type `{content_type}`. Supported: {', '.join(SUPPORTED_TYPES)}",
            parse_mode="Markdown",
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
        text, err = extract_youtube(source)
        if err:
            await update.message.reply_text(f"❌ Could not get transcript: {err}\n\nTip: paste the transcript manually.")
            return
        title = source

    elif content_type in ("article", "podcast"):
        if not source.startswith("http"):
            await update.message.reply_text("❌ Please provide a URL.")
            return
        result, err = extract_article(source)
        if err:
            await update.message.reply_text(f"❌ Could not extract content: {err}")
            return
        text   = result["text"]
        title  = result["title"]
        author = result["author"]

    elif content_type == "book":
        if not source:
            await update.message.reply_text("❌ Provide the book title: `Learn book Atomic Habits`", parse_mode="Markdown")
            return
        # No scraping needed — Claude summarises from its own knowledge
        text  = f"Please summarise the book: {source}"
        title = source
        await update.message.reply_text("📖 Summarising from knowledge base…")

    elif content_type == "pdf":
        if file_bytes is None:
            await update.message.reply_text(
                "❌ Attach a PDF file and use `Learn pdf` as the *caption*.",
                parse_mode="Markdown",
            )
            return
        text, err = extract_pdf(file_bytes)
        if err:
            await update.message.reply_text(f"❌ Could not read PDF: {err}")
            return
        title = source or "PDF Document"

    if not text:
        await update.message.reply_text("❌ No content could be extracted.")
        return

    # ── Claude summarization ───────────────────────────────────────────────────
    await update.message.reply_text("🧠 Claude is reading and summarising…")

    summary, err = summarize_with_claude(content_type, text, title, source)
    if err:
        await update.message.reply_text(f"❌ Summarization failed: {err}")
        return

    final_title  = summary.get("title") or title or source[:80]
    final_author = summary.get("author") or author

    # ── Build Notion blocks ────────────────────────────────────────────────────
    blocks = build_notion_blocks(summary, source)

    # ── Save to Notion ─────────────────────────────────────────────────────────
    await update.message.reply_text("📝 Saving to Notion…")

    ok, result = create_learn_page(
        content_type, final_title, blocks,
        metadata={"author": final_author},
    )

    if ok:
        tldr_preview = summary.get("tldr", "")[:220]
        await update.message.reply_text(
            f"✅ Saved to Notion!\n\n"
            f"{TYPE_EMOJI.get(content_type, '📖')} *{final_title}*\n\n"
            f"💡 {tldr_preview}",
            parse_mode="Markdown",
        )
    else:
        await update.message.reply_text(f"❌ Could not save to Notion: {result}")
