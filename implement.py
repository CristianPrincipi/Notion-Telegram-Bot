import os
import re
import json
import requests

from notion_client import (
    search_page_in_db, get_children, blocks_to_text, append_children,
    delete_block, create_page, get_page_title, update_page,
    paragraph as _paragraph, heading2 as _heading2, heading3 as _heading3,
    callout as _callout, bullet as _bullet, numbered as _numbered, divider as _divider,
)

# ─── ENV ───────────────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
LEARN_ID = os.environ.get("LEARN_ID")
BRAIN_ID = os.environ.get("BRAIN_ID")


# ─── 1. AREA ROUTING ───────────────────────────────────────────────────────────

def get_area_db_id(area_name: str) -> str | None:
    """Maps 'Brain' → BRAIN_ID, 'Finance' → FINANCE_ID — matching David's existing env var convention."""
    key = f"{area_name.upper().replace(' ', '_')}_ID"
    return os.environ.get(key)


# ─── 2. NOTION HELPERS (thin wrappers over the shared client) ──────────────────
# Most helpers now live in notion_client. These wrappers preserve the existing
# call sites in this file (get_all_blocks, clear_page_blocks, etc.).

_get_page_title_from_result = get_page_title  # alias for the old name used below


def get_all_blocks(page_id: str) -> tuple[list[dict], str | None]:
    """Retrieve all top-level blocks of a Notion page. Delegates to shared get_children."""
    return get_children(page_id)


def clear_page_blocks(blocks: list[dict]) -> None:
    """Archive all blocks by deleting them one by one (via shared delete_block)."""
    for block in blocks:
        block_id = block.get("id")
        if block_id:
            delete_block(block_id)


def append_blocks_to_page(page_id: str, blocks: list[dict]) -> str | None:
    """Append blocks to a page in batches. Returns error string or None."""
    _, err = append_children(page_id, blocks)
    return err


def create_manual_page(db_id: str, blocks: list[dict]) -> tuple[str | None, str | None]:
    """Create a new Manual page in a database. Returns (page_id, error)."""
    return create_page(
        db_id,
        {"Name": {"title": [{"text": {"content": "Manual"}}]}},
        children=blocks,
        icon="📋",
    )


# ─── 3. CLAUDE MERGE ───────────────────────────────────────────────────────────

_IMPLEMENT_SYSTEM = """You are a knowledge integration expert building a personal Manual page in Notion.

You receive two documents:
- SOURCE: new knowledge just learned (video, article, book, etc.)
- MANUAL: the existing Manual page for a specific life area (may be empty on first run)

Your task: produce a single, authoritative, conflict-free Manual page.

Return ONLY valid JSON — no markdown fences, no preamble:
{
  "title": "Manual: [short topic name]",
  "overview": "2-3 sentence description of what this Manual covers and its current state",
  "routine": [
    {"step": 1, "name": "Step Name", "action": "Concise, concrete description of what to do"}
  ],
  "improvements": [
    {"title": "Technique or optimization name", "description": "What it is, why it is better, when to apply it"}
  ],
  "step_explanations": [
    {
      "step": "Step Name (must match a name in routine)",
      "purpose": "Why this step exists and what it achieves",
      "how_to": "Detailed, executable instructions",
      "best_practices": ["Specific practice 1", "Specific practice 2"],
      "mistakes": ["Common mistake 1", "Common mistake 2"]
    }
  ],
  "sources": ["Source title or reference"]
}

Integration rules:
- If MANUAL is empty → build the full Manual from SOURCE alone.
- If MANUAL exists → intelligently merge; never just concatenate.
- Resolve conflicts by preferring the more specific, evidence-based approach.
- Eliminate all redundancy — each concept appears exactly once.
- The routine must be a practical, executable workflow, not a list of concepts.
- Every step in routine must have a matching entry in step_explanations.
- Be specific and actionable throughout.
- Return raw JSON only."""


def merge_with_claude(
    source_text: str,
    manual_text: str,
    topic: str,
    source_title: str = "",
) -> tuple[dict | None, str | None]:
    """Call Claude to merge source knowledge into the existing Manual. Returns (merged_dict, error)."""
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY not set in environment."

    user_msg = (
        f"Topic: {topic}\n\n"
        f"=== SOURCE (new knowledge to integrate) ===\n"
        f"Title: {source_title}\n\n"
        f"{source_text[:60000]}\n\n"
        f"=== MANUAL (existing content to update) ===\n"
        f"{'[Empty — this is the first implementation for this area]' if not manual_text.strip() else manual_text[:40000]}"
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
                "max_tokens": 8192,
                "system":     _IMPLEMENT_SYSTEM,
                "messages":   [{"role": "user", "content": user_msg}],
            },
            timeout=(10, 300),
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()

        # Robustly extract the JSON object even if Claude adds surrounding text
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            return None, f"No JSON found in Claude response: {raw[:300]}"

        return json.loads(json_match.group(0)), None

    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    except Exception as e:
        return None, str(e)


# ─── 4. NOTION BLOCK BUILDER ───────────────────────────────────────────────────
# Basic builders are imported from notion_client. Only the two with custom bold
# formatting are defined locally.

def _labeled_paragraph(label: str, text: str) -> dict:
    """Paragraph with a bold label prefix: 'Label: content'."""
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": [
                {"type": "text", "text": {"content": f"{label}: "}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": text}},
            ]}}

def _bullet_bold_prefix(bold_part: str, rest: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": [
                {"type": "text", "text": {"content": bold_part}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": f" — {rest}"}},
            ]}}


def build_manual_blocks(merged: dict, source_title: str) -> list[dict]:
    """Convert the merged JSON dict into a structured list of Notion blocks."""
    blocks: list[dict] = []

    # ── Overview callout ───────────────────────────────────────────────────────
    if merged.get("overview"):
        blocks.append(_callout(merged["overview"], "📋", "gray_background"))
    blocks.append(_divider())

    # ── Perfect Process ────────────────────────────────────────────────────────
    routine = merged.get("routine", [])
    if routine:
        blocks.append(_heading2("⚙️ Perfect Process"))
        for step in routine:
            name   = step.get("name", "")
            action = step.get("action", "")
            blocks.append(_numbered(f"{name}: {action}" if name else action))
    blocks.append(_divider())

    # ── Improvements & Optimizations ──────────────────────────────────────────
    improvements = merged.get("improvements", [])
    if improvements:
        blocks.append(_heading2("🚀 Improvements & Optimizations"))
        for imp in improvements:
            title = imp.get("title", "")
            desc  = imp.get("description", "")
            if title and desc:
                blocks.append(_bullet_bold_prefix(title, desc))
            else:
                blocks.append(_bullet(title or desc))
    blocks.append(_divider())

    # ── Step-by-Step Breakdown ─────────────────────────────────────────────────
    explanations = merged.get("step_explanations", [])
    if explanations:
        blocks.append(_heading2("📖 Step-by-Step Breakdown"))
        for exp in explanations:
            step_name = exp.get("step", "")
            if step_name:
                blocks.append(_heading3(f"→ {step_name}"))
            if exp.get("purpose"):
                blocks.append(_labeled_paragraph("Purpose", exp["purpose"]))
            if exp.get("how_to"):
                blocks.append(_labeled_paragraph("How to", exp["how_to"]))
            practices = exp.get("best_practices", [])
            if practices:
                blocks.append(_paragraph("✅ Best Practices"))
                for p in practices:
                    blocks.append(_bullet(p))
            mistakes = exp.get("mistakes", [])
            if mistakes:
                blocks.append(_paragraph("⚠️ Common Mistakes"))
                for m in mistakes:
                    blocks.append(_bullet(m))
    blocks.append(_divider())

    # ── Sources ────────────────────────────────────────────────────────────────
    sources = merged.get("sources", [])
    if sources:
        blocks.append(_heading2("📚 Sources"))
        for source in sources:
            blocks.append(_bullet(source))

    return blocks


# ─── 5. MAIN HANDLER ───────────────────────────────────────────────────────────

async def handle_implement(update, user_text: str):
    """
    Entry point called from david.py.

    Command format:  Implement [Page Name] - [Target Area]
    Example:         Implement Memory Techniques - Brain

    Flow:
      A) Find [Page Name] in LEARN_ID database → extract its content
      B) Find (or prepare) 'Manual' page in AREA_{TARGET_AREA}_ID database
      C) Merge both with Claude → structured Manual JSON
      D) Update (or create) the Manual page in Notion
      E) Tick the source page's 'Implemented' checkbox (feeds the Learn-nudge job)
    """

    # ── Parse command ──────────────────────────────────────────────────────────
    match = re.match(r"(?i)implement\s+(.+?)\s*-\s*(.+)", user_text.strip())
    if not match:
        await update.message.reply_text(
            "🔧 *Implement command usage:*\n"
            "`Implement [Page Name] - [Target Area]`\n\n"
            "Example: `Implement Memory Techniques - Brain`\n\n"
            "The page must exist in your Learn database.\n"
            "The target area must have `AREA_[NAME]_ID` set on Railway.",
            parse_mode="Markdown",
        )
        return

    page_name = match.group(1).strip()
    area_name = match.group(2).strip()

    # ── Diet uses a dedicated structured handler (nested toggles + surgical updates) ──
    if area_name.lower() == "diet":
        from implement_diet import handle_implement_diet
        await handle_implement_diet(update, page_name)
        return

    # ── Validate area DB ───────────────────────────────────────────────────────
    area_db_id = get_area_db_id(area_name)
    if not area_db_id:
        env_key = f"{area_name.upper().replace(' ', '_')}_ID"
        await update.message.reply_text(
            f"❌ Area *{area_name}* is not configured.\n"
            f"Add `{env_key}` to your Railway environment variables,\n"
            f"pointing to the Notion database ID for that area.",
            parse_mode="Markdown",
        )
        return

    # ── Step A: Retrieve source page from Learn DB ─────────────────────────────
    await update.message.reply_text(
        f"🔍 Searching for *{page_name}* in Learn database…", parse_mode="Markdown"
    )

    source_page, err = search_page_in_db(LEARN_ID, page_name)
    if err:
        await update.message.reply_text(
            f"❌ Could not find *{page_name}* in your Learn database.\n\n"
            f"Make sure you used `Learn` to save it first, and that the title matches.",
            parse_mode="Markdown",
        )
        return

    source_page_id = source_page["id"]
    source_title   = _get_page_title_from_result(source_page)

    source_blocks, err = get_all_blocks(source_page_id)
    if err:
        await update.message.reply_text(f"❌ Could not retrieve content of source page: {err}")
        return

    source_text = blocks_to_text(source_blocks)
    if not source_text.strip():
        await update.message.reply_text("❌ Source page appears to be empty.")
        return

    # ── Step B: Retrieve (or prepare) Manual in target area ────────────────────
    await update.message.reply_text(
        f"📂 Looking for Manual in *{area_name}*…", parse_mode="Markdown"
    )

    manual_page, _  = search_page_in_db(area_db_id, "Manual", exact=True)
    manual_page_id  = manual_page["id"] if manual_page else None
    manual_text     = ""
    is_new_manual   = manual_page_id is None

    if manual_page_id:
        manual_blocks, _ = get_all_blocks(manual_page_id)
        manual_text       = blocks_to_text(manual_blocks)

    # ── Step C: Merge with Claude ──────────────────────────────────────────────
    await update.message.reply_text("🧠 Claude is merging knowledge into Manual…")

    merged, err = merge_with_claude(
        source_text  = source_text,
        manual_text  = manual_text,
        topic        = f"{area_name} — {page_name}",
        source_title = source_title,
    )
    if err:
        await update.message.reply_text(f"❌ Merge failed: {err}")
        return

    # ── Build Notion blocks ────────────────────────────────────────────────────
    new_blocks = build_manual_blocks(merged, source_title)

    # ── Write to Notion ────────────────────────────────────────────────────────
    await update.message.reply_text("📝 Writing updated Manual to Notion…")

    if is_new_manual:
        page_id, err = create_manual_page(area_db_id, new_blocks)
        if not page_id:
            await update.message.reply_text(f"❌ Could not create Manual page: {err}")
            return
        action = "created ✨"
    else:
        # Clear existing content and replace with merged version
        existing_blocks, _ = get_all_blocks(manual_page_id)
        clear_page_blocks(existing_blocks)
        err = append_blocks_to_page(manual_page_id, new_blocks)
        if err:
            await update.message.reply_text(f"❌ Could not update Manual: {err}")
            return
        action = "updated 🔄"

    # ── Step E: Mark the source Learn page as implemented (best-effort) ─────────
    # Drives the Learn-nudge job, which only surfaces still-unimplemented items.
    update_page(source_page_id, {"Implemented": {"checkbox": True}})

    routine_count     = len(merged.get("routine", []))
    improvement_count = len(merged.get("improvements", []))

    await update.message.reply_text(
        f"✅ Manual {action}\n\n"
        f"📋 *{merged.get('title', 'Manual')}*\n"
        f"📍 Area: {area_name}\n\n"
        f"⚙️ {routine_count} process steps\n"
        f"🚀 {improvement_count} improvements\n\n"
        f"_Source used: {source_title}_",
        parse_mode="Markdown",
    )
