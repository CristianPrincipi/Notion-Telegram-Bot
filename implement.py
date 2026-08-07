"""
`Implement [Page] - [Area]` — merge a Learn page into that area's Manual.

WHY THIS IS SECTIONED AND NOT A WHOLE-PAGE REWRITE
--------------------------------------------------
It used to send the ENTIRE Manual plus the new source to Claude and rebuild the
whole page from the reply. That made every run a lossy round-trip:

  • Sections the source said nothing about still went through the model and came
    back subtly reworded. Run it monthly and a Manual drifts away from what you
    actually wrote, one paraphrase at a time, with no diff to point at.
  • The Manual was truncated at 40k characters on the way IN (`manual_text[:40000]`).
    Past that, the tail was silently dropped from the prompt — so Claude merged
    against a Manual it could not see the end of, and the rebuilt page came back
    missing it.

implement_diet.py already solved this for the Diet page. This is the same shape,
for the flat Manual layout:

  1. read the Manual and index it by HEADING into sections
  2. a cheap ROUTING call: section NAMES + the new source → which are affected
  3. a targeted MERGE call: only those sections' content + the new source
  4. write back only those sections

**Untouched sections are never sent to the model, so they cannot drift.** That is
the property the whole design exists for, and `tests/test_implement_sections.py`
asserts it directly.

Note what is and is not saved. Reading the Manual is ONE Notion request either
way — the blocks are flat, so the index is free. The saving is in tokens and in
write scope, not in round trips.

WRITING BACK
------------
Append-then-delete, per section, exactly as in Hard Rule 2: snapshot the
section's content block IDs, append the replacement immediately after the
section heading (`append_children(..., after=heading_id)`), return early on
error with nothing deleted, and only then drop the snapshot.
"""

import asyncio
import os
import re

from anthropic_client import complete_json
from config import ANTHROPIC_TIMEOUT
from page_lock import PageBusy, page_lock
from telegram_text import escape_md, reply
from notion_client import (
    search_page_in_db, get_children, blocks_to_text, append_children,
    delete_block, create_page, get_page_title, update_page, extract_rich_text,
    paragraph as _paragraph, heading2 as _heading2, heading3 as _heading3,
    callout as _callout, bullet as _bullet, numbered as _numbered, divider as _divider,
)

# ─── ENV ───────────────────────────────────────────────────────────────────────
LEARN_ID = os.environ.get("LEARN_ID")
BRAIN_ID = os.environ.get("BRAIN_ID")

# The H2 that holds one H3 per routine step. It is the only section that grows
# new subsections, so it is the only place a new step can be added.
STEPS_SECTION = "Step-by-Step Breakdown"


# ─── 1. AREA ROUTING ───────────────────────────────────────────────────────────

def get_area_db_id(area_name: str) -> str | None:
    """Maps 'Brain' → BRAIN_ID, 'Finance' → FINANCE_ID — matching David's existing env var convention."""
    key = f"{area_name.upper().replace(' ', '_')}_ID"
    return os.environ.get(key)


# ─── 2. NOTION HELPERS ─────────────────────────────────────────────────────────
# All of them BLOCK. handle_implement reaches every one through
# asyncio.to_thread — calling one directly from the event loop stalls the whole
# bot for the length of the round trip.
#
# Only the ones that ADD something live here. get_children, append_children and
# get_page_title are used straight from notion_client: the wrappers that used to
# stand in front of them (get_all_blocks, append_blocks_to_page and a
# `_get_page_title_from_result = get_page_title` alias kept for a name nothing
# has called in a long time) forwarded their arguments unchanged and bought only
# a second name to look up.

def clear_page_blocks_by_id(block_ids: list) -> None:
    """Archive blocks from a snapshot of IDs taken before the replacement was written.

    Takes IDs rather than block objects so the caller can snapshot what to delete
    BEFORE appending the replacement, and only actually delete once the append
    has succeeded. Deleting from a re-read of the page after appending would
    delete the new content too.
    """
    for block_id in block_ids:
        if block_id:
            delete_block(block_id)


def create_manual_page(db_id: str, blocks: list[dict]) -> tuple[str | None, str | None]:
    """Create a new Manual page in a database. Returns (page_id, error)."""
    return create_page(
        db_id,
        {"Name": {"title": [{"text": {"content": "Manual"}}]}},
        children=blocks,
        icon="📋",
    )


# ─── 3. THE SECTION INDEX ──────────────────────────────────────────────────────
# A Manual is a FLAT list of blocks: H2, its content, a divider, the next H2, and
# under "Step-by-Step Breakdown" an H3 per routine step. A section therefore owns
# the blocks between its heading and the next heading of any level.
#
# Dividers are treated as STRUCTURE, not content: they are never sent to the
# model and never deleted, so rewriting a section repeatedly cannot slowly strip
# the page's separators.

_DECORATION = re.compile(r"^[^\w]+")


def _heading_level(block: dict) -> int | None:
    return {"heading_1": 1, "heading_2": 2, "heading_3": 3}.get(block.get("type", ""))


def _heading_text(block: dict) -> str:
    btype = block.get("type", "")
    return extract_rich_text(block.get(btype, {}).get("rich_text", []))


def _clean(title: str) -> str:
    """'⚙️ Perfect Process' → 'Perfect Process'; '→ Active Recall' → 'Active Recall'.

    The Manual bakes decoration into its headings. The model is shown the clean
    name and answers with it, so the decoration never has to survive a round trip
    through the prompt.
    """
    return _DECORATION.sub("", (title or "").strip()) or (title or "").strip()


def _normalise_path(path: str) -> str:
    return " > ".join(p.strip().lower() for p in (path or "").split(">") if p.strip())


class Section:
    """One addressable part of a Manual."""

    __slots__ = ("path", "level", "heading_id", "content_ids", "content_blocks", "tail_id")

    def __init__(self, path, level, heading_id):
        self.path           = path
        self.level          = level
        self.heading_id     = heading_id
        self.content_ids    = []      # replaceable leaf blocks, in page order
        self.content_blocks = []      # the same blocks, for flattening to text
        self.tail_id        = heading_id   # last block of this section's whole region

    @property
    def text(self) -> str:
        return blocks_to_text(self.content_blocks)

    @property
    def style(self) -> str:
        """How this section writes its lines, so a rewrite keeps its shape.

        Without this, merging the numbered 'Perfect Process' would silently
        return it as bullets.
        """
        types = {b.get("type") for b in self.content_blocks}
        if "numbered_list_item" in types:
            return "numbered"
        if "bulleted_list_item" in types:
            return "bullet"
        return "paragraph"


def read_manual_sections(page_id: str) -> tuple[list, str | None]:
    """Index a Manual by heading. Returns (sections, error).

    BLOCKING — one Notion request (paginated). Run via asyncio.to_thread.
    """
    blocks, err = get_children(page_id)
    if err:
        return [], err

    sections, current, current_h2 = [], None, None
    for block in blocks:
        level = _heading_level(block)
        if level in (2, 3):
            name = _clean(_heading_text(block))
            if not name:
                continue
            path = name if level == 2 else f"{current_h2.path} > {name}" if current_h2 else name
            current = Section(path, level, block.get("id"))
            sections.append(current)
            if level == 2:
                current_h2 = current
            elif current_h2:
                current_h2.tail_id = block.get("id")
            continue

        if current is None:            # preamble (the overview callout) — not a section
            continue
        if block.get("type") == "divider":
            continue                   # structure, not content

        block_id = block.get("id")
        current.content_ids.append(block_id)
        current.content_blocks.append(block)
        current.tail_id = block_id
        if current_h2 is not None and current is not current_h2:
            current_h2.tail_id = block_id

    return sections, None


def _resolve(path: str, sections: list):
    """Find the section a model-supplied path refers to, tolerant of spacing/case."""
    wanted = _normalise_path(path)
    for section in sections:
        if _normalise_path(section.path) == wanted:
            return section
    return None


# ─── 4. THE TWO CLAUDE CALLS ───────────────────────────────────────────────────

_ROUTE_SYSTEM = """You route new knowledge into an existing personal Manual.

You are given the SECTION NAMES of a Manual (not their contents) and a SOURCE
document. Decide which sections the source actually informs.

Rules:
- Only name a section if the SOURCE genuinely adds to, corrects, or sharpens it.
  Sections the source says nothing about must be left out — they will not be
  touched, which is the point.
- Prefer the most specific section. If the source is about one routine step, name
  that step, not the whole breakdown.
- If the source introduces a genuinely new routine step that has no section yet,
  name it under new_steps instead of forcing it into an existing one.
- If the source informs nothing in this Manual, return empty lists."""

_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "affected": {
            "type": "array",
            "description": "Existing sections the source informs.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Exactly as given in SECTIONS."},
                    "why":  {"type": "string", "description": "One short line."},
                },
                "required": ["path"],
            },
        },
        "new_steps": {
            "type": "array",
            "description": f"Routine steps to add under '{STEPS_SECTION}' that do not exist yet.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "why":  {"type": "string"},
                },
                "required": ["name"],
            },
        },
    },
    "required": ["affected"],
}


def route_sections(section_paths: list, source_text: str, source_title: str):
    """Which sections does this source touch? Returns (routing, error).

    Deliberately cheap: section NAMES only, never their content. This is what
    keeps the untouched majority of the Manual out of the model entirely.
    """
    listing = "\n".join(f"- {p}" for p in section_paths)
    user_msg = (
        f"=== SECTIONS (names only) ===\n{listing}\n\n"
        f"=== SOURCE: {source_title} ===\n{source_text[:60000]}"
    )
    return complete_json(_ROUTE_SYSTEM, user_msg, _ROUTE_SCHEMA, max_tokens=2048)


_MERGE_SYSTEM = """You merge new knowledge into specific sections of a personal Manual.

For each section you are given its current content and the new SOURCE. Return the
FULL merged content for that section — it replaces what is there.

Rules:
- Merge, never concatenate. Each fact appears exactly once, in its best form.
- Keep everything in the current content that the source does not supersede.
  Content you leave out is deleted.
- Resolve conflicts toward the more specific, evidence-based version.
- Write each line as a complete, standalone, actionable statement.
- Match the section's stated style: numbered = ordered steps, bullet = list
  items, paragraph = prose paragraphs, one per line.
- Return only sections you are actually changing."""

_MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path":  {"type": "string", "description": "Exactly as given."},
                    "lines": {"type": "array", "items": {"type": "string"},
                              "description": "The FULL merged content, one line per block."},
                },
                "required": ["path", "lines"],
            },
        },
    },
    "required": ["updates"],
}


def merge_sections(targets: list, source_text: str, source_title: str):
    """Merge the source into the given sections. Returns (result, error).

    `targets` is a list of {"path", "style", "text"} — only the sections routing
    selected. Everything else in the Manual is absent from this prompt.
    """
    parts = []
    for target in targets:
        body = target["text"].strip() or "(empty — this section is new)"
        parts.append(f"--- SECTION: {target['path']} (style: {target['style']}) ---\n{body}")

    user_msg = (
        "=== SECTIONS TO MERGE ===\n" + "\n\n".join(parts) +
        f"\n\n=== SOURCE: {source_title} ===\n{source_text[:60000]}"
    )
    return complete_json(_MERGE_SYSTEM, user_msg, _MERGE_SCHEMA)


# ─── 5. NOTION BLOCK BUILDERS ──────────────────────────────────────────────────

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


def render_lines(lines: list, style: str) -> list:
    """Merged lines → Notion blocks, in the section's own style."""
    builder = {"numbered": _numbered, "bullet": _bullet}.get(style, _paragraph)
    return [builder(line) for line in lines]


def build_manual_blocks(merged: dict) -> list[dict]:
    """The full Manual, built from scratch. Used ONLY on the first run for an area.

    After that the page exists and every run is sectioned, so this shape is what
    read_manual_sections indexes: an H2 per section, an H3 per routine step.
    """
    blocks: list[dict] = []

    if merged.get("overview"):
        blocks.append(_callout(merged["overview"], "📋", "gray_background"))
    blocks.append(_divider())

    routine = merged.get("routine", [])
    if routine:
        blocks.append(_heading2("⚙️ Perfect Process"))
        for step in routine:
            name   = step.get("name", "")
            action = step.get("action", "")
            blocks.append(_numbered(f"{name}: {action}" if name else action))
    blocks.append(_divider())

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

    explanations = merged.get("step_explanations", [])
    if explanations:
        blocks.append(_heading2(f"📖 {STEPS_SECTION}"))
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

    sources = merged.get("sources", [])
    if sources:
        blocks.append(_heading2("📚 Sources"))
        for source in sources:
            blocks.append(_bullet(source))

    return blocks


# ─── 6. FIRST-RUN FULL BUILD ───────────────────────────────────────────────────

_BUILD_SYSTEM = """You are building a personal Manual page in Notion from scratch.

You receive a SOURCE document. Produce a single, authoritative, conflict-free
Manual for this area.

Rules:
- The routine must be a practical, executable workflow, not a list of concepts.
- Every step in routine has a matching entry in step_explanations.
- Eliminate redundancy — each concept appears exactly once.
- Be specific and actionable throughout."""

_BUILD_SCHEMA = {
    "type": "object",
    "properties": {
        "title":    {"type": "string", "description": "Manual: [short topic name]"},
        "overview": {"type": "string",
                     "description": "2-3 sentences on what this Manual covers."},
        "routine": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step":   {"type": "integer"},
                    "name":   {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["name", "action"],
            },
        },
        "improvements": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title":       {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["title", "description"],
            },
        },
        "step_explanations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step":           {"type": "string",
                                       "description": "Must match a name in routine."},
                    "purpose":        {"type": "string"},
                    "how_to":         {"type": "string"},
                    "best_practices": {"type": "array", "items": {"type": "string"}},
                    "mistakes":       {"type": "array", "items": {"type": "string"}},
                },
                "required": ["step"],
            },
        },
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "routine"],
}


def build_manual(source_text: str, topic: str, source_title: str = ""):
    """Build a whole Manual from one source. Returns (manual, error)."""
    user_msg = (
        f"Topic: {topic}\n\n"
        f"=== SOURCE ===\nTitle: {source_title}\n\n{source_text[:60000]}"
    )
    return complete_json(_BUILD_SYSTEM, user_msg, _BUILD_SCHEMA)


# ─── 7. SURGICAL WRITE-BACK ────────────────────────────────────────────────────

def apply_section_updates(page_id: str, updates: list, sections: list,
                          new_paths: set | None = None) -> tuple[int, list]:
    """Rewrite only the named sections. Returns (applied_count, skipped).

    BLOCKING — several Notion round trips per section touched. Run via
    asyncio.to_thread.

    APPEND FIRST, DELETE AFTER, per section (Hard Rule 2). The stale IDs come
    from the index built BEFORE anything was written, never from a re-read — a
    re-read after appending would list the new blocks too and delete them.

    Sections absent from `updates` are never touched, which is what keeps them
    byte-identical run over run.
    """
    new_paths = new_paths or set()
    applied, skipped = 0, []

    for upd in updates:
        path  = (upd.get("path") or "").strip()
        lines = [ln for ln in upd.get("lines", []) if ln and ln.strip()]
        if not path or not lines:
            continue

        section = _resolve(path, sections)

        # ── A brand-new step: pure append under the steps section, nothing stale ──
        if section is None:
            if _normalise_path(path) not in {_normalise_path(p) for p in new_paths}:
                skipped.append(path)
                continue
            anchor = _resolve(STEPS_SECTION, sections)
            if anchor is None:
                skipped.append(f"{path} (no '{STEPS_SECTION}' section to add it to)")
                continue
            name = path.split(">")[-1].strip()
            blocks = [_heading3(f"→ {name}")] + render_lines(lines, "bullet")
            _, err = append_children(page_id, blocks, after=anchor.tail_id)
            if err:
                skipped.append(f"{path} ({err})")
                continue
            applied += 1
            continue

        # ── An existing section: replace its content in place ────────────────────
        stale_ids = list(section.content_ids)
        _, err = append_children(
            page_id, render_lines(lines, section.style), after=section.heading_id)
        if err:
            # Nothing deleted — the section still holds its previous content.
            skipped.append(f"{path} ({err})")
            continue

        clear_page_blocks_by_id(stale_ids)
        applied += 1

    return applied, skipped


# ─── 8. MAIN HANDLER ───────────────────────────────────────────────────────────

async def handle_implement(update, user_text: str):
    """
    Entry point called from david.py.

    Command format:  Implement [Page Name] - [Target Area]
    Example:         Implement Memory Techniques - Brain

    Flow:
      A) Find [Page Name] in LEARN_ID → extract its content
      B) Find the area's Manual
         · no Manual yet → build the whole thing from the source (one call)
         · Manual exists → route (names only) → merge (affected only) → write back
      C) Tick the source page's 'Implemented' checkbox
    """

    match = re.match(r"(?i)implement\s+(.+?)\s*-\s*(.+)", user_text.strip())
    if not match:
        await reply(
            update,
            "🔧 *Implement command usage:*\n"
            "`Implement [Page Name] - [Target Area]`\n\n"
            "Example: `Implement Memory Techniques - Brain`\n\n"
            "The page must exist in your Learn database.\n"
            "The target area must have `AREA_[NAME]_ID` set on Railway.",
        )
        return

    page_name = match.group(1).strip()
    area_name = match.group(2).strip()

    # ── Diet uses a dedicated structured handler (nested toggles + surgical updates) ──
    if area_name.lower() == "diet":
        from implement_diet import handle_implement_diet
        await handle_implement_diet(update, page_name)
        return

    area_db_id = get_area_db_id(area_name)
    if not area_db_id:
        env_key = f"{area_name.upper().replace(' ', '_')}_ID"
        await reply(
            update,
            f"❌ Area *{escape_md(area_name)}* is not configured.\n"
            f"Add `{env_key}` to your Railway environment variables,\n"
            f"pointing to the Notion database ID for that area.",
        )
        return

    # ── Step A: Retrieve source page from Learn DB ─────────────────────────────
    await reply(update, f"🔍 Searching for *{escape_md(page_name)}* in Learn database…")

    source_page, err = await asyncio.to_thread(search_page_in_db, LEARN_ID, page_name)
    if err:
        await reply(
            update,
            f"❌ Could not find *{escape_md(page_name)}* in your Learn database.\n\n"
            f"Make sure you used `Learn` to save it first, and that the title matches.",
        )
        return

    source_page_id = source_page["id"]
    source_title   = get_page_title(source_page)

    source_blocks, err = await asyncio.to_thread(get_children, source_page_id)
    if err:
        await update.message.reply_text(f"❌ Could not retrieve content of source page: {err}")
        return

    source_text = blocks_to_text(source_blocks)
    if not source_text.strip():
        await update.message.reply_text("❌ Source page appears to be empty.")
        return

    # ── Steps B–D run under a per-area lock ────────────────────────────────────
    # Taken BEFORE the read, not just around the write: the merge is only valid
    # if it was computed from a Manual nobody else is mutating.
    try:
        async with page_lock(area_db_id):
            await reply(update, f"📂 Looking for Manual in *{escape_md(area_name)}*…")
            manual_page, _ = await asyncio.to_thread(
                search_page_in_db, area_db_id, "Manual", exact=True)

            if manual_page is None:
                done = await _first_run(update, area_db_id, area_name,
                                        page_name, source_text, source_title)
            else:
                done = await _sectioned_run(update, manual_page["id"], area_name,
                                            page_name, source_text, source_title)
            if not done:
                return
    except PageBusy:
        await reply(
            update,
            f"⏳ An update to the *{escape_md(area_name)}* Manual is already in progress.\n"
            "Wait for it to finish, then try again.",
        )
        return

    # ── Mark the source Learn page as implemented (best-effort) ────────────────
    await asyncio.to_thread(update_page, source_page_id, {"Implemented": {"checkbox": True}})


async def _first_run(update, area_db_id, area_name, page_name, source_text, source_title) -> bool:
    """No Manual yet — build the whole page from this one source. Returns True on success."""
    await update.message.reply_text("🧠 First run for this area — Claude is building the Manual…")

    try:
        manual, err = await asyncio.wait_for(
            asyncio.to_thread(build_manual, source_text,
                              f"{area_name} — {page_name}", source_title),
            timeout=ANTHROPIC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
            "Nothing was written."
        )
        return False
    if err:
        await update.message.reply_text(f"❌ Build failed: {err}")
        return False

    page_id, err = await asyncio.to_thread(
        create_manual_page, area_db_id, build_manual_blocks(manual))
    if not page_id:
        await update.message.reply_text(f"❌ Could not create Manual page: {err}")
        return False

    # manual['title'] is Claude's, source_title is Notion's — both escaped.
    await reply(
        update,
        f"✅ Manual created ✨\n\n"
        f"📋 *{escape_md(manual.get('title', 'Manual'))}*\n"
        f"📍 Area: {escape_md(area_name)}\n\n"
        f"⚙️ {len(manual.get('routine', []))} process steps\n"
        f"🚀 {len(manual.get('improvements', []))} improvements\n\n"
        f"_Source used: {escape_md(source_title)}_",
    )
    return True


async def _sectioned_run(update, manual_page_id, area_name, page_name,
                         source_text, source_title) -> bool:
    """Route → merge → write back only the affected sections. Returns True on success."""
    sections, err = await asyncio.to_thread(read_manual_sections, manual_page_id)
    if err:
        await update.message.reply_text(f"❌ Could not read the existing Manual: {err}")
        return False
    if not sections:
        await update.message.reply_text(
            "❌ The Manual has no headings to update. Rename its sections, or delete "
            "the page and re-run to rebuild it."
        )
        return False

    # ── Routing: names only ────────────────────────────────────────────────────
    await update.message.reply_text(
        f"🧭 Checking which of the {len(sections)} sections this affects…")
    try:
        routing, err = await asyncio.wait_for(
            asyncio.to_thread(route_sections, [s.path for s in sections],
                              source_text, source_title),
            timeout=ANTHROPIC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
            "Your Manual is unchanged — nothing was written."
        )
        return False
    if err:
        await update.message.reply_text(f"❌ Routing failed: {err}")
        return False

    affected  = [a for a in routing.get("affected", []) if a.get("path")]
    new_steps = [s for s in routing.get("new_steps", []) if s.get("name")]

    if not affected and not new_steps:
        await reply(
            update,
            f"ℹ️ *{escape_md(page_name)}* doesn't map to anything in the "
            f"*{escape_md(area_name)}* Manual — nothing was changed.",
        )
        return True

    new_paths = {f"{STEPS_SECTION} > {s['name'].strip()}" for s in new_steps}
    targets   = []
    for item in affected:
        section = _resolve(item["path"], sections)
        if section is not None:
            targets.append({"path": section.path, "style": section.style, "text": section.text})
    targets += [{"path": p, "style": "bullet", "text": ""} for p in new_paths]

    if not targets:
        await update.message.reply_text(
            "⚠️ Claude named sections I couldn't find in the Manual — nothing was changed."
        )
        return True

    await reply(update, _format_plan(affected, new_steps, len(sections)))

    # ── Merge: only the affected sections ──────────────────────────────────────
    try:
        merged, err = await asyncio.wait_for(
            asyncio.to_thread(merge_sections, targets, source_text, source_title),
            timeout=ANTHROPIC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await update.message.reply_text(
            f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
            "Your Manual is unchanged — nothing was written."
        )
        return False
    if err:
        await update.message.reply_text(f"❌ Merge failed: {err}")
        return False

    await update.message.reply_text("📝 Writing the updated sections to Notion…")
    applied, skipped = await asyncio.to_thread(
        apply_section_updates, manual_page_id, merged.get("updates", []), sections, new_paths)

    msg = (f"✅ Manual updated 🔄\n\n"
           f"📍 Area: {escape_md(area_name)}\n"
           f"✏️ *{applied}* of {len(sections)} section(s) rewritten — the rest were never "
           f"sent to Claude, so they are untouched.\n\n"
           f"_Source used: {escape_md(source_title)}_")
    if skipped:
        # A skipped section is genuinely unchanged: its append failed, so the
        # delete never ran and its previous content is still there. Each entry is
        # "path (notion error)", so both halves need escaping.
        msg += ("\n\n⚠️ Skipped — these are unchanged:\n"
                + "\n".join(f"• {escape_md(s)}" for s in skipped[:8]))
    await reply(update, msg)
    return True


def _format_plan(affected: list, new_steps: list, total: int) -> str:
    """What is about to change, sent before anything is written.

    Every interpolated value is Claude's: the section paths it chose and the free-
    form `why` it wrote for each. Escaped here rather than at the send site so the
    function stays safe wherever it is sent from.
    """
    lines = [f"📋 *Plan* — {len(affected) + len(new_steps)} of {total} sections\n"]
    for item in affected:
        why = item.get("why", "")
        lines.append(f"♻️ {escape_md(item['path'])}"
                     + (f" — _{escape_md(why)}_" if why else ""))
    for step in new_steps:
        why = step.get("why", "")
        lines.append(f"🆕 {STEPS_SECTION} > {escape_md(step['name'])}"
                     + (f" — _{escape_md(why)}_" if why else ""))
    return "\n".join(lines)
