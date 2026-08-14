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
import logging
import os
import re

from clients.anthropic_client import complete_json
from clients.calendar_client import now_local
from config import ANTHROPIC_TIMEOUT, MANUAL_SOURCE_CHARS, is_unverified_source
from page_lock import PageBusy, page_lock
from telegram_text import escape_md
from clients.notion_client import (
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

# The H2 David owns. Every other section belongs to Claude and to you; this one
# is a ledger of what has been merged into the page and when, so it is the one
# section that must never be rewritten by a merge.
#
# `build_manual_blocks` writes it on the FIRST run only, from whatever Claude
# listed, and no sectioned run has ever appended to it — so until now a Manual
# recorded nothing at all about what went into it after day one.
SOURCES_SECTION = "Sources"

logger = logging.getLogger(__name__)


# ─── 0. THE INPUT BUDGET ───────────────────────────────────────────────────────

def fit_to_budget(text: str, cap: int, *, label: str) -> tuple[str, str | None]:
    """Cut `text` to `cap`. Returns (text, warning) — the warning is None if it fit.

    THE ONLY PLACE EITHER IMPLEMENT PATH CUTS ITS SOURCE, and it is shared with
    services/implement_diet.py rather than copied into it. Both halves of that
    matter:

      • ONE PLACE. It was five inline slices with three different numbers, so a
        long Learn page merged into a Manual from its first 60k characters —
        indistinguishable, in the reply and on the page, from a full merge. The
        prompt builders below no longer slice at all: with a cap at both ends, a
        source of exactly the budget and one cut down to it are the same string,
        and the warning stops being reliable at exactly its own boundary.
      • ONE WORDING. Two copies of the warning is how one of them gets reworded
        and the other silently does not.

    It returns the warning rather than sending it because this function is
    synchronous and knows nothing about Telegram — the caller awaits `notify`.

    The log is at WARNING and carries the length BEFORE the cut, which is the
    only number that says how much was lost. The reply is for now, and it scrolls
    away; the log is what is left afterwards.
    """
    if len(text) <= cap:
        return text, None
    logger.warning("%s: source is %d chars, cut to %d — the merge sees %.0f%% of it",
                   label, len(text), cap, 100 * cap / len(text))
    return text[:cap], (
        f"⚠️ Source is {len(text):,} characters; merging from the first "
        f"{cap:,}. Anything after that was not read."
    )


# ─── 1. AREA ROUTING ───────────────────────────────────────────────────────────

def get_area_db_id(area_name: str) -> str | None:
    """Maps 'Brain' → BRAIN_ID, 'Finance' → FINANCE_ID — matching David's existing env var convention."""
    key = f"{area_name.upper().replace(' ', '_')}_ID"
    return os.environ.get(key)


# ─── 2. NOTION HELPERS ─────────────────────────────────────────────────────────
# All of them BLOCK. run_implement reaches every one through
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
    def lines(self) -> list:
        """The section's content as RAW lines, one per block.

        Deliberately not `self.text.splitlines()`. `blocks_to_text` prefixes each
        block by type (`- `, `• `, `## `) and the lines a merge returns carry no
        prefix at all — they get one from `render_lines` on the way back in. So
        comparing the two forms would compare the markdown and not the content,
        and `_survives` below would report every line as lost.
        """
        return [extract_rich_text(b.get(b.get("type", ""), {}).get("rich_text", []))
                for b in self.content_blocks]

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


def is_sources_path(path: str) -> bool:
    """True for the Sources section and anything nested under it.

    Matched on the FIRST segment, so an H3 that ends up beneath the ledger cannot
    slip through a check that only compared whole paths.
    """
    first = _normalise_path(path).split(" > ")[0]
    return first == SOURCES_SECTION.lower()


def routable_sections(sections: list) -> list:
    """The section paths a routing call may be shown. Sources is not one of them.

    ONE function, called from one place, because the requirement is that this
    exclusion cannot be silently undone. A future change to how the taxonomy is
    built goes through here; a `[s.path for s in sections]` written inline at the
    call site would quietly put the ledger back in front of the model.

    This is half the guard. The other half is in `apply_section_updates`, which
    refuses to WRITE a Sources path however it was arrived at — because this list
    only controls what Claude is shown, and a model can name a section it was
    never offered.
    """
    return [s.path for s in sections if not is_sources_path(s.path)]


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

    `source_text` arrives already within MANUAL_SOURCE_CHARS — run_implement owns
    the cap and is the only place it is applied. Do not re-slice here.
    """
    listing = "\n".join(f"- {p}" for p in section_paths)
    user_msg = (
        f"=== SECTIONS (names only) ===\n{listing}\n\n"
        f"=== SOURCE: {source_title} ===\n{source_text}"
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


# Appended to the merge prompt when the source is a recollection. It is the HINT,
# not the guard: `_hold_back_rewrites` below checks the same rule mechanically and
# refuses to write a section that breaks it. Both, because the instruction alone
# is a request a model can decline silently, and the check alone would hold back
# far more sections than it needs to.
_UNVERIFIED_MERGE_RULE = """

=== THIS SOURCE IS UNVERIFIED ===
It is a model's recollection of a work, not an extract of any text. Nothing was
read to produce it, so relative to the Manual it is the WEAKER evidence.

- Reproduce every existing line of each section EXACTLY as it is given to you.
  Do not reword, reorder, shorten, or combine them.
- You may ADD new lines from this source. You may not replace or delete an
  existing one, however much this source appears to contradict it.
- A section whose existing lines are not all reproduced verbatim will be
  discarded and left unchanged."""


def merge_sections(targets: list, source_text: str, source_title: str,
                   unverified: bool = False):
    """Merge the source into the given sections. Returns (result, error).

    `targets` is a list of {"path", "style", "text"} — only the sections routing
    selected. Everything else in the Manual is absent from this prompt.

    `source_text` arrives already within MANUAL_SOURCE_CHARS — run_implement owns
    the cap. The section texts in `targets` are NOT capped and must not be: a
    section is sent whole or not at all, because what comes back replaces it.
    """
    parts = []
    for target in targets:
        body = target["text"].strip() or "(empty — this section is new)"
        parts.append(f"--- SECTION: {target['path']} (style: {target['style']}) ---\n{body}")

    user_msg = (
        "=== SECTIONS TO MERGE ===\n" + "\n\n".join(parts) +
        f"\n\n=== SOURCE: {source_title} ===\n{source_text}"
        + (_UNVERIFIED_MERGE_RULE if unverified else "")
    )
    return complete_json(_MERGE_SYSTEM, user_msg, _MERGE_SCHEMA)


# ─── 4b. THE ADDITIONS-ONLY RULE ───────────────────────────────────────────────
# What an unverified source is allowed to do to a Manual, checked rather than
# asked for.
#
# WHAT THIS ENFORCES: no line already in the Manual is deleted or reworded by a
# merge whose source was a recollection. That is a real, deterministic guarantee —
# David holds both sides before it writes, so it needs no judgement from anyone.
#
# WHAT IT DOES NOT ENFORCE, and must never be described as enforcing: anything at
# all about the lines it ADDS. Nothing can check whether a recollected fact is
# true. That is why the Sources ledger exists — the trail is what you have when
# the content itself cannot be verified.
#
# THE COST IS REAL. _MERGE_SYSTEM asks for "the FULL merged content", and a model
# reproducing twenty lines verbatim will eventually reword one. Those sections are
# held back and named rather than written, which is the safe direction but is not
# a free one. See PLAN.md's open question.


def _comparable(line: str) -> str:
    """A line stripped of differences that are not differences.

    Whitespace only. Case and punctuation stay significant: "do it daily" and "Do
    it daily." are the same claim to a reader and a REWRITE to this rule, which is
    exactly the edit an unverified source must not be allowed to make silently.
    """
    return " ".join((line or "").split())


def _survives(old_lines: list, new_lines: list) -> list:
    """The old lines that do NOT appear, verbatim and in order, in the new ones.

    A subsequence walk, not a set difference: order carries meaning in a numbered
    routine, and a merge that reversed two steps while keeping both would pass a
    membership check.
    """
    remaining = [_comparable(line) for line in new_lines]
    lost = []
    for line in old_lines:
        wanted = _comparable(line)
        if not wanted:
            continue
        try:
            remaining = remaining[remaining.index(wanted) + 1:]
        except ValueError:
            lost.append(line)
    return lost


def _hold_back_rewrites(updates: list, sections: list) -> tuple[list, list]:
    """Split an unverified merge into (may_be_written, held_back).

    Runs BEFORE apply_section_updates rather than inside it. What may be written
    is a policy decision about this source; the writer stays a writer, and its
    three buckets (applied / skipped / partial) keep describing what Notion did
    rather than what David decided.
    """
    allowed, held = [], []
    for upd in updates:
        section = _resolve((upd.get("path") or "").strip(), sections)
        # No section, or nothing there yet (a new step, an empty section): there
        # is no existing content to protect, so the rule has nothing to say.
        if section is None or not section.lines:
            allowed.append(upd)
            continue

        lost = _survives(section.lines, upd.get("lines", []))
        if lost:
            held.append((section.path, lost))
        else:
            allowed.append(upd)
    return allowed, held


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
    """Build a whole Manual from one source. Returns (manual, error).

    `source_text` arrives already within MANUAL_SOURCE_CHARS — run_implement owns
    the cap. Do not re-slice here.
    """
    user_msg = (
        f"Topic: {topic}\n\n"
        f"=== SOURCE ===\nTitle: {source_title}\n\n{source_text}"
    )
    return complete_json(_BUILD_SYSTEM, user_msg, _BUILD_SCHEMA)


# ─── 7. SURGICAL WRITE-BACK ────────────────────────────────────────────────────

def apply_section_updates(page_id: str, updates: list, sections: list,
                          new_paths: set | None = None) -> tuple[int, list, list]:
    """Rewrite only the named sections. Returns (applied_count, skipped, partial).

    BLOCKING — several Notion round trips per section touched. Run via
    asyncio.to_thread.

    APPEND FIRST, DELETE AFTER, per section (Hard Rule 2). The stale IDs come
    from the index built BEFORE anything was written, never from a re-read — a
    re-read after appending would list the new blocks too and delete them.

    Sections absent from `updates` are never touched, which is what keeps them
    byte-identical run over run.

    THREE buckets, not two. A failed append used to go in `skipped`, which the
    reply prints under "these are unchanged" — true when the append failed on its
    first batch and FALSE when it failed on a later one, because those earlier
    batches are on the page and the stale content is still there underneath them.
    A section in `partial` is the one state a re-run cannot fix by itself.
    """
    new_paths = new_paths or set()
    applied, skipped, partial = 0, [], []

    for upd in updates:
        path  = (upd.get("path") or "").strip()
        lines = [ln for ln in upd.get("lines", []) if ln and ln.strip()]
        if not path or not lines:
            continue

        # THE LEDGER IS NOT WRITEABLE, and this is checked here rather than only
        # in routable_sections. That list decides what Claude is SHOWN; a model
        # can name a section it was never offered, and one that did would rewrite
        # the only record of where the page's content came from — with the
        # provenance of an unverified merge as the first thing to go.
        if is_sources_path(path):
            skipped.append(f"{path} (David's own section — never rewritten by a merge)")
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
            written, err = append_children(page_id, blocks, after=anchor.tail_id)
            if err:
                # A new step has no stale content to lose, so a half-written one
                # is a half-written ADDITION rather than a mangled section — but
                # it is still not "unchanged".
                (partial if written.partial else skipped).append(f"{path} ({err})")
                continue
            applied += 1
            continue

        # ── An existing section: replace its content in place ────────────────────
        stale_ids = list(section.content_ids)
        written, err = append_children(
            page_id, render_lines(lines, section.style), after=section.heading_id)
        if err:
            if written.partial:
                # The old content is intact — the delete never ran — and part of
                # the replacement is now sitting next to it. Both copies are on
                # the page, which is the accepted cost of never showing neither
                # (Hard Rule 2), but it has to be SAID or the next run merges
                # against a section that reads as duplicated.
                partial.append(f"{path} ({written.summary} written, "
                               f"old content still there: {err})")
            else:
                # Nothing deleted, nothing appended — genuinely unchanged.
                skipped.append(f"{path} ({err})")
            continue

        clear_page_blocks_by_id(stale_ids)
        applied += 1

    return applied, skipped, partial


# ─── 7b. THE SOURCES LEDGER ────────────────────────────────────────────────────

def _source_record(source_title: str, unverified: bool) -> str:
    """One ledger line: what was merged, whether it was read, and when."""
    provenance = " — unverified (model recollection)" if unverified else ""
    return f"{source_title}{provenance} — {now_local():%d %b %Y}"


def record_source(page_id: str, sections: list | None,
                  source_title: str, unverified: bool) -> tuple[bool, str | None]:
    """Append this run to the Manual's Sources section. Returns (ok, error).

    BLOCKING — one or two Notion round trips. Run via asyncio.to_thread.

    A PURE APPEND, which is why it can run after the section writes without any of
    Hard Rule 2's ordering care: there is nothing to delete, so there is no window
    in which the page holds neither the old nor the new.

    `sections` is the index the caller already built, or None to read one. The
    index stays valid across the section writes because Sources is the one section
    those writes can never touch — which is the whole point of the two guards
    above.

    The provenance goes HERE and not into the merged lines. An inline marker would
    be sent back to the model as "current content" on the next run, reworded at its
    discretion, and never removed if a real source later confirmed the claim. This
    line is written by David and read by people.
    """
    if sections is None:
        sections, err = read_manual_sections(page_id)
        if err:
            return False, err

    bullet = _bullet(_source_record(source_title, unverified))
    ledger = next((s for s in sections if is_sources_path(s.path)), None)

    if ledger is None:
        # A Manual whose first run produced no sources has no ledger to append to.
        # Both blocks go at the END of the page, which is where the section lives
        # in every Manual build_manual_blocks has ever emitted.
        _, err = append_children(page_id, [_heading2(f"📚 {SOURCES_SECTION}"), bullet])
        return (err is None), err

    _, err = append_children(page_id, [bullet], after=ledger.tail_id)
    return (err is None), err


# ─── 8. MAIN HANDLER ───────────────────────────────────────────────────────────

async def run_implement(user_text: str, *, notify, notify_md=None):
    """
    Entry point. `bot/implement.py` binds the callbacks; nothing here sends.

    Command format:  Implement [Page Name] - [Target Area]
    Example:         Implement Memory Techniques - Brain

    Flow:
      A) Find [Page Name] in LEARN_ID → extract its content
      B) Find the area's Manual
         · no Manual yet → build the whole thing from the source (one call)
         · Manual exists → route (names only) → merge (affected only) → write back
      C) Tick the source page's 'Implemented' checkbox
    """
    notify_md = notify_md or notify

    match = re.match(r"(?i)implement\s+(.+?)\s*-\s*(.+)", user_text.strip())
    if not match:
        await notify_md("🔧 *Implement command usage:*\n"
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
        from services.implement_diet import run_implement_diet
        await run_implement_diet(page_name, notify=notify, notify_md=notify_md)
        return

    area_db_id = get_area_db_id(area_name)
    if not area_db_id:
        env_key = f"{area_name.upper().replace(' ', '_')}_ID"
        await notify_md(f"❌ Area *{escape_md(area_name)}* is not configured.\n"
            f"Add `{env_key}` to your Railway environment variables,\n"
            f"pointing to the Notion database ID for that area.",
        )
        return

    # ── Step A: Retrieve source page from Learn DB ─────────────────────────────
    await notify_md(f"🔍 Searching for *{escape_md(page_name)}* in Learn database…")

    source_page, err = await asyncio.to_thread(search_page_in_db, LEARN_ID, page_name)
    if err:
        await notify_md(f"❌ Could not find *{escape_md(page_name)}* in your Learn database.\n\n"
            f"Make sure you used `Learn` to save it first, and that the title matches.",
        )
        return

    source_page_id = source_page["id"]
    source_title   = get_page_title(source_page)

    source_blocks, err = await asyncio.to_thread(get_children, source_page_id)
    if err:
        await notify(f"❌ Could not retrieve content of source page: {err}")
        return

    source_text = blocks_to_text(source_blocks)
    if not source_text.strip():
        await notify("❌ Source page appears to be empty.")
        return

    # ── Fit the merge's budget, out loud, ONCE ─────────────────────────────────
    # Ahead of is_unverified_source below, so the predicate reads exactly the
    # string the model will be given. That ordering is safe in one direction
    # only: the marker is the FIRST block Learn writes (services/learn.py, above
    # the TL;DR), so a head-slice always keeps it. Were it ever moved to the
    # bottom of the page, truncating here would silently disarm the
    # additions-only rule on precisely the long recollections that need it most.
    source_text, over_budget = fit_to_budget(
        source_text, MANUAL_SOURCE_CHARS, label="run_implement")
    if over_budget:
        # Plain notify: David's own string, interpolating only integers, so there
        # is nothing to escape and no reason to risk a parse_mode rejection on
        # the one message that exists to warn you.
        await notify(over_budget)

    # Is this source Claude's recollection rather than something that was read?
    # The marker is a sentence Learn writes into the page body, so it is already
    # here in source_text — the same text the merge call is about to be given.
    # Nothing extra is fetched to find out.
    unverified = is_unverified_source(source_text)

    # ── Steps B–D run under a per-area lock ────────────────────────────────────
    # Taken BEFORE the read, not just around the write: the merge is only valid
    # if it was computed from a Manual nobody else is mutating.
    try:
        async with page_lock(area_db_id):
            await notify_md(f"📂 Looking for Manual in *{escape_md(area_name)}*…")
            manual_page, _ = await asyncio.to_thread(
                search_page_in_db, area_db_id, "Manual", exact=True)

            if manual_page is None:
                done = await _first_run(area_db_id, area_name,
                                        page_name, source_text, source_title,
                                        unverified=unverified,
                                        notify=notify, notify_md=notify_md)
            else:
                done = await _sectioned_run(manual_page["id"], area_name,
                                            page_name, source_text, source_title,
                                            unverified=unverified,
                                            notify=notify, notify_md=notify_md)
            if not done:
                return
    except PageBusy:
        await notify_md(f"⏳ An update to the *{escape_md(area_name)}* Manual is already in progress.\n"
            "Wait for it to finish, then try again.",
        )
        return

    # ── Mark the source Learn page as implemented (best-effort) ────────────────
    await asyncio.to_thread(update_page, source_page_id, {"Implemented": {"checkbox": True}})


# The one line every Implement message uses to say the source was not read.
# One string, so the warning cannot be worded three ways in three places and then
# fixed in one of them.
_UNVERIFIED_LINE = ("⚠️ *Unverified source* — this page is Claude's recollection, "
                    "not an extract of a text. Anything merged from it inherits that.")


async def _first_run(area_db_id, area_name, page_name, source_text, source_title,
                     *, unverified=False, notify, notify_md) -> bool:
    """No Manual yet — build the whole page from this one source. Returns True on success."""
    # Said BEFORE the build, not only after it: on a first run this one source
    # becomes the entire Manual, which is the worst case for an unverified one.
    if unverified:
        await notify_md(_UNVERIFIED_LINE)

    await notify("🧠 First run for this area — Claude is building the Manual…")

    try:
        manual, err = await asyncio.wait_for(
            asyncio.to_thread(build_manual, source_text,
                              f"{area_name} — {page_name}", source_title),
            timeout=ANTHROPIC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await notify(
            f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
            "Nothing was written."
        )
        return False
    if err:
        await notify(f"❌ Build failed: {err}")
        return False

    page_id, err = await asyncio.to_thread(
        create_manual_page, area_db_id, build_manual_blocks(manual))
    if not page_id:
        await notify(f"❌ Could not create Manual page: {err}")
        return False

    # A page id AND an error means the page exists and is missing content — see
    # notion_client.create_page. Reported as its own outcome, because "created"
    # and "created with half the Manual on it" need different next steps.
    # manual['title'] is Claude's, source_title is Notion's — both escaped.
    await notify_md(f"{'⚠️ *Manual created, but incomplete*' if err else '✅ Manual created ✨'}\n\n"
        f"📋 *{escape_md(manual.get('title', 'Manual'))}*\n"
        f"📍 Area: {escape_md(area_name)}\n\n"
        f"⚙️ {len(manual.get('routine', []))} process steps\n"
        f"🚀 {len(manual.get('improvements', []))} improvements\n\n"
        f"_Source used: {escape_md(source_title)}_"
        + (f"\n\n{_UNVERIFIED_LINE}" if unverified else ""),
    )
    if err:
        # Plain: a raw Notion error inside a code span cannot be escaped safely.
        await notify(f"⚠️ Not all of the Manual was written: {err}\n"
                     "Re-running appends a second copy of what did land — open "
                     "the page and check it before you do.")

    # The ledger's first entry. No index to pass — the page was created moments
    # ago, so record_source reads one for itself.
    await _record(page_id, None, source_title, unverified, notify=notify)
    return True


async def _record(page_id, sections, source_title, unverified, *, notify) -> None:
    """Write the ledger line, and say so if it could not be written.

    Its own failure, reported separately: the Manual write succeeded and the
    provenance did not, which is a different thing from either half failing alone
    — and the provenance is the part you cannot reconstruct later.
    """
    ok, err = await asyncio.to_thread(
        record_source, page_id, sections, source_title, unverified)
    if not ok:
        await notify(f"⚠️ The Manual was updated but I could not record the source "
                     f"in its Sources section: {err}")


async def _sectioned_run(manual_page_id, area_name, page_name,
                         source_text, source_title, *,
                         unverified=False, notify, notify_md) -> bool:
    """Route → merge → write back only the affected sections. Returns True on success."""
    sections, err = await asyncio.to_thread(read_manual_sections, manual_page_id)
    if err:
        await notify(f"❌ Could not read the existing Manual: {err}")
        return False
    if not sections:
        await notify(
            "❌ The Manual has no headings to update. Rename its sections, or delete "
            "the page and re-run to rebuild it."
        )
        return False

    # ── Routing: names only ────────────────────────────────────────────────────
    await notify(
        f"🧭 Checking which of the {len(sections)} sections this affects…")
    try:
        routing, err = await asyncio.wait_for(
            # routable_sections, never [s.path for s in sections]: the Sources
            # ledger is David's and is not offered to the model. Asserted by
            # test_implement_sections, because an inline list comprehension here
            # is exactly how that exclusion gets undone without anyone noticing.
            asyncio.to_thread(route_sections, routable_sections(sections),
                              source_text, source_title),
            timeout=ANTHROPIC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await notify(
            f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
            "Your Manual is unchanged — nothing was written."
        )
        return False
    if err:
        await notify(f"❌ Routing failed: {err}")
        return False

    affected  = [a for a in routing.get("affected", []) if a.get("path")]
    new_steps = [s for s in routing.get("new_steps", []) if s.get("name")]

    if not affected and not new_steps:
        await notify_md(f"ℹ️ *{escape_md(page_name)}* doesn't map to anything in the "
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
        await notify(
            "⚠️ Claude named sections I couldn't find in the Manual — nothing was changed."
        )
        return True

    await notify_md(_format_plan(affected, new_steps, len(sections), unverified))

    # ── Merge: only the affected sections ──────────────────────────────────────
    try:
        merged, err = await asyncio.wait_for(
            asyncio.to_thread(merge_sections, targets, source_text, source_title,
                              unverified),
            timeout=ANTHROPIC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        await notify(
            f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
            "Your Manual is unchanged — nothing was written."
        )
        return False
    if err:
        await notify(f"❌ Merge failed: {err}")
        return False

    # ── The additions-only rule, for an unverified source only ─────────────────
    # Checked here rather than trusted to the prompt above. What this catches is
    # narrow and real: a merge that would delete or reword something already in
    # the Manual on the authority of a recollection. It says nothing about whether
    # what it lets through is TRUE — see the note above _comparable.
    updates = merged.get("updates", [])
    held = []
    if unverified:
        updates, held = _hold_back_rewrites(updates, sections)

    await notify("📝 Writing the updated sections to Notion…")
    applied, skipped, partial = await asyncio.to_thread(
        apply_section_updates, manual_page_id, updates, sections, new_paths)

    msg = (f"✅ Manual updated 🔄\n\n"
           f"📍 Area: {escape_md(area_name)}\n"
           f"✏️ *{applied}* of {len(sections)} section(s) rewritten — the rest were never "
           f"sent to Claude, so they are untouched.\n\n"
           f"_Source used: {escape_md(source_title)}_")
    if unverified:
        msg += f"\n\n{_UNVERIFIED_LINE}"
    if skipped:
        # A skipped section is genuinely unchanged: its append failed, so the
        # delete never ran and its previous content is still there. Each entry is
        # "path (notion error)", so both halves need escaping.
        msg += ("\n\n⚠️ Skipped — these are unchanged:\n"
                + "\n".join(f"• {escape_md(s)}" for s in skipped[:8]))
    if partial:
        # Its own heading, deliberately not folded into "skipped": these sections
        # hold their old content AND part of a replacement, and only a person
        # looking at the page can sort that out.
        msg += ("\n\n🚨 Half-written — open these in Notion, the old and new "
                "content are both there:\n"
                + "\n".join(f"• {escape_md(s)}" for s in partial[:8]))
    if held:
        # A THIRD kind of unchanged, and its own heading for the same reason the
        # other two have theirs: this one is not Notion failing, it is David
        # refusing. The lines that would have gone are named, because "a section
        # was held back" is not something you can act on and "these two lines
        # would have been replaced by a recollection" is.
        msg += ("\n\n🛡️ Held back — an unverified source cannot rewrite what is "
                "already there. These are unchanged:\n"
                + "\n".join(f"• {escape_md(path)} — would have lost: "
                            + "; ".join(escape_md(line[:60]) for line in lost[:3])
                            for path, lost in held[:5]))
    await notify_md(msg)

    # The ledger, after the writes and only on a run that got this far. `sections`
    # is the index from before the writes and is still correct for Sources — the
    # one section apply_section_updates is not allowed to touch.
    await _record(manual_page_id, sections, source_title, unverified, notify=notify)
    return True


def _format_plan(affected: list, new_steps: list, total: int,
                 unverified: bool = False) -> str:
    """What is about to change, sent before anything is written.

    Every interpolated value is Claude's: the section paths it chose and the free-
    form `why` it wrote for each. Escaped here rather than at the send site so the
    function stays safe wherever it is sent from.

    The unverified warning goes at the TOP and before the merge call, which is the
    only moment it is actionable: after the write, the Manual already holds the
    content and the message is a post-mortem.
    """
    lines = [f"📋 *Plan* — {len(affected) + len(new_steps)} of {total} sections\n"]
    if unverified:
        lines.append(f"{_UNVERIFIED_LINE}\n")
    for item in affected:
        why = item.get("why", "")
        lines.append(f"♻️ {escape_md(item['path'])}"
                     + (f" — _{escape_md(why)}_" if why else ""))
    for step in new_steps:
        why = step.get("why", "")
        lines.append(f"🆕 {STEPS_SECTION} > {escape_md(step['name'])}"
                     + (f" — _{escape_md(why)}_" if why else ""))
    return "\n".join(lines)
