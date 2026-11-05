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
Edits, not rewrites. The merge call is shown each section as numbered lines and
answers with OPERATIONS on them — add after [n], replace [n], remove [n] — so a
line it does not name keeps its block, its formatting and anything nested under
it. It used to return a section's full content, which replaced every block in
the section: a line kept word for word still lost its bold label and its nested
lines, which `get_children` (one level) had never even read.

The write is still Hard Rule 2, per section: the IDs to delete come from the
index taken before anything was written; every replacement and addition is
appended right after its anchor line (`append_children(..., after=…)`); an error
returns with nothing deleted; only then are the replaced and removed lines
deleted.
"""

import asyncio
import logging
import os
import re
from collections import Counter

from clients.anthropic_client import complete_json
from clients.calendar_client import now_local
from config import (
    ANTHROPIC_ROUTE_EFFORT, ANTHROPIC_ROUTE_MAX_TOKENS, ANTHROPIC_TIMEOUT, IMPLEMENTED_PROPERTY,
    MANUAL_PAGE_TITLE, MANUAL_SOURCE_CHARS, MANUAL_TITLE_PROPERTY, is_unverified_source,
)
from page_lock import PageBusy, page_lock
from telegram_text import escape_md
from clients.notion_client import (
    search_page_in_db, get_children, blocks_to_text, append_children,
    delete_block, create_page, get_page_title, update_page, extract_rich_text, rich,
    paragraph as _paragraph, heading2 as _heading2, heading3 as _heading3,
    callout as _callout, bullet as _bullet, numbered as _numbered, divider as _divider,
)

# ─── ENV ───────────────────────────────────────────────────────────────────────
LEARN_ID = os.environ.get("LEARN_ID")

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
        {MANUAL_TITLE_PROPERTY: {"title": [{"text": {"content": MANUAL_PAGE_TITLE}}]}},
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


# The block types a merge may replace or remove: each is one run of rich text
# that David can read whole and write back in the same type. Any other block (a
# callout, a quote, a to-do, an image) is shown to the model as FIXED — it can be
# added after, never replaced or removed.
_EDITABLE_TYPES = ("paragraph", "bulleted_list_item", "numbered_list_item")


class Line:
    """One numbered line of a section, as the merge call is shown it."""

    __slots__ = ("n", "block", "text", "fixed")

    def __init__(self, n, block, text, fixed):
        self.n     = n
        self.block = block
        self.text  = text
        self.fixed = fixed

    @property
    def block_id(self):
        return self.block.get("id")


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
    def numbered(self) -> list:
        """The section's lines as the merge call is shown them: [1], [2], ….

        The numbers index THIS object, the index taken before anything is
        written, and the ops that come back are resolved against the same object.
        So [3] always means the block it meant when the prompt was built.

        A blank text block (spacing) is neither numbered nor touched. A block
        that is not plain text, or that has blocks nested under it, is FIXED:
        `get_children` reads one level, so its nested lines were never read, and
        replacing or removing it would delete them unseen.
        """
        lines = []
        for block in self.content_blocks:
            btype = block.get("type", "")
            text = extract_rich_text(block.get(btype, {}).get("rich_text", []))
            editable = btype in _EDITABLE_TYPES and not block.get("has_children")
            if editable and not text.strip():
                continue
            lines.append(Line(len(lines) + 1, block,
                              text.strip() or f"({btype.replace('_', ' ')})",
                              fixed=not editable))
        return lines

    @property
    def listing(self) -> str:
        """The numbered lines as one string, for the merge prompt.

        Whitespace is flattened: a soft line break inside a block would otherwise
        start an unnumbered row the model could read as a line of its own.
        """
        return "\n".join(f"[{line.n}]{' (fixed)' if line.fixed else ''} "
                         + " ".join(line.text.split())
                         for line in self.numbered)

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
                "additionalProperties": False,
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
                "additionalProperties": False,
            },
        },
    },
    "required": ["affected"],
    "additionalProperties": False,
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
    return complete_json(_ROUTE_SYSTEM, user_msg, _ROUTE_SCHEMA,
                         max_tokens=ANTHROPIC_ROUTE_MAX_TOKENS, effort=ANTHROPIC_ROUTE_EFFORT)


_MERGE_SYSTEM = """You edit specific sections of a personal Manual with new knowledge from a SOURCE.

Each section is given as numbered lines, "[n] text". Answer with EDIT OPERATIONS
on those lines, not with rewritten sections: every line you do not name stays
exactly as it is, formatting and all.

Operations:
- add: a new line after line [n]. Use 0 to put it at the top of the section.
  Several adds after the same line appear in the order you give them.
- replace: line [n] becomes your text, when the source corrects it, makes it
  more specific, or backs it with better evidence.
- remove: line [n] goes, only when the source shows it is wrong or it now says
  the same as another line.
- To merge two lines that say the same thing: replace one with the merged line
  and remove the other.
- A line marked (fixed) holds content you cannot see or edit. You may add after
  it; never replace or remove it.
- Name each line by the number it was given; never renumber after your own
  edits. Name a line in at most one replace or remove.
- Every operation has a why: one short line on what in the source justifies it.

Rules:
- Merge, never concatenate. Do not add a line that restates an existing one;
  sharpen that line instead.
- Resolve conflicts toward the more specific, evidence-based version.
- Write each new or replaced line as a complete, standalone, actionable
  statement in the section's style: numbered = ordered steps, bullet = list
  items, paragraph = prose.
- A section shown as empty is new: give it add operations at line 0.
- Return only sections you are actually changing."""

_MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Exactly as given."},
                    "ops": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op":   {"type": "string", "enum": ["add", "replace", "remove"]},
                                "line": {"type": "integer",
                                         "description": "replace / remove: the line's [n]. "
                                                        "add: the [n] it goes after; 0 = top."},
                                "text": {"type": "string",
                                         "description": "add / replace: the line as it should "
                                                        "read. remove: empty."},
                                "why":  {"type": "string",
                                         "description": "One short line: what in the source "
                                                        "justifies this."},
                            },
                            "required": ["op", "line", "text", "why"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["path", "ops"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["updates"],
    "additionalProperties": False,
}


# Appended to the merge prompt when the source is a recollection. It is the HINT,
# not the guard: `_hold_back_rewrites` below drops every replace and remove from
# such a source whatever the model answers. Both, because the instruction alone
# is a request a model can decline silently, and the check alone would hold back
# edits the model need never have proposed.
_UNVERIFIED_MERGE_RULE = """

=== THIS SOURCE IS UNVERIFIED ===
It is a model's recollection of a work, not an extract of any text. Nothing was
read to produce it, so relative to the Manual it is the WEAKER evidence.

- Use add only. Do not replace or remove any existing line, however much this
  source appears to contradict it.
- A replace or remove from this source is discarded, not applied."""


def merge_sections(targets: list, source_text: str, source_title: str,
                   unverified: bool = False):
    """Edit operations for the given sections. Returns (result, error).

    `targets` is a list of {"path", "style", "text"} — only the sections routing
    selected, `text` being the section's numbered listing (`Section.listing`).
    Everything else in the Manual is absent from this prompt.

    `source_text` arrives already within MANUAL_SOURCE_CHARS — run_implement owns
    the cap. The section listings in `targets` are NOT capped and must not be: a
    section is sent whole or not at all, because the line numbers the ops come
    back with index into it.
    """
    parts = []
    for target in targets:
        body = target["text"].strip() or "(empty — this section is new)"
        parts.append(f"--- SECTION: {target['path']} (style: {target['style']}) ---\n{body}")

    user_msg = (
        "=== SECTIONS TO EDIT ===\n" + "\n\n".join(parts) +
        f"\n\n=== SOURCE: {source_title} ===\n{source_text}"
        + (_UNVERIFIED_MERGE_RULE if unverified else "")
    )
    return complete_json(_MERGE_SYSTEM, user_msg, _MERGE_SCHEMA)


# ─── 4b. THE ADDITIONS-ONLY RULE ───────────────────────────────────────────────
# What an unverified source is allowed to do to a Manual, checked rather than
# asked for.
#
# WHAT THIS ENFORCES: no line already in the Manual is replaced or removed by a
# merge whose source was a recollection. Its adds are written; every other
# operation is held back and named. That is a real, deterministic guarantee: it
# reads the KIND of each operation, so it needs no judgement from anyone.
#
# WHAT IT DOES NOT ENFORCE, and must never be described as enforcing: anything at
# all about the lines it ADDS. Nothing can check whether a recollected fact is
# true. That is why the Sources ledger exists — the trail is what you have when
# the content itself cannot be verified.
#
# It used to hold back a whole SECTION whenever a line went missing or changed
# from the model's full rewrite of it — and a model reproducing twenty lines
# verbatim eventually rewords one. With edit operations nothing has to be
# reproduced: an add cannot touch an existing line, so what is held back is only
# ever an edit that asked to change one.


def _hold_back_rewrites(updates: list, sections: list) -> tuple[list, list]:
    """Split an unverified merge into (may_be_written, held_back).

    Every `add` may be written. Every other operation is held back, named by the
    line it would have changed, as (path, [line text, …]).

    Runs BEFORE apply_section_updates rather than inside it. What may be written
    is a policy decision about this source; the writer stays a writer, and its
    buckets (edited / skipped / partial) keep describing what Notion did rather
    than what David decided.
    """
    allowed, held = [], []
    for upd in updates:
        path = (upd.get("path") or "").strip()
        ops  = [op for op in upd.get("ops") or [] if isinstance(op, dict)]
        adds = [op for op in ops if op.get("op") == "add"]
        rest = [op for op in ops if op.get("op") != "add"]
        if adds:
            allowed.append({**upd, "ops": adds})
        if rest:
            section = _resolve(path, sections)
            texts = {line.n: line.text for line in section.numbered} if section else {}
            held.append((path, [texts.get(op.get("line"), f"line [{op.get('line')}]")
                                for op in rest]))
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
        blocks.append(_heading2(f"📚 {SOURCES_SECTION}"))
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
                "additionalProperties": False,
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
                "additionalProperties": False,
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
                "additionalProperties": False,
            },
        },
        "sources": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "routine"],
    "additionalProperties": False,
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

class SectionEdit:
    """What one section's operations come to, checked against the pre-write index.

    Also the record the reply is built from, so the diff a person reads is the
    edit that was written, not the answer Claude gave.
    """

    __slots__ = ("path", "inserts", "added", "replaced", "removed", "ignored")

    def __init__(self, path):
        self.path     = path
        self.inserts  = []     # [(after_id, [blocks])], in page order
        self.added    = 0
        self.replaced = []     # [(Line, why)]
        self.removed  = []     # [(Line, why)]
        self.ignored  = []     # [reason], one per operation not applied

    @property
    def doomed_ids(self) -> list:
        """What is deleted once every insert has landed: the replaced and removed lines."""
        return [line.block_id for line, _ in self.replaced + self.removed]


def _replacement_block(line: Line, text: str) -> dict:
    """`text` in the old line's own block type, keeping its bold label.

    The type, because a numbered step replaced by a bullet would restyle the
    routine. The label, because `**Spacing** — beats massed practice` is how the
    Manual is built, and a replacement that starts with the same label keeps it
    bold. Any other formatting belonged to the old text and goes with it.
    """
    btype = line.block.get("type", "paragraph")
    runs  = line.block.get(btype, {}).get("rich_text", [])
    first = runs[0] if runs else {}
    label = first.get("plain_text", "") if (first.get("annotations") or {}).get("bold") else ""
    if label.strip() and text.startswith(label):
        rest = text[len(label):]
        rich_text = ([{"type": "text", "text": {"content": label[:2000]},
                       "annotations": {"bold": True}}]
                     + (rich(rest) if rest else []))
    else:
        rich_text = rich(text)
    return {"object": "block", "type": btype, btype: {"rich_text": rich_text}}


def _plan_edits(section: Section, ops: list) -> SectionEdit:
    """Resolve a section's operations against its numbered lines. No I/O.

    An operation that names a line which is not there, a FIXED line, or a line
    another replace or remove also names is ignored, and the reason kept. Two
    edits to one line contradict each other; keeping the line is the direction
    that loses nothing.
    """
    edit  = SectionEdit(section.path)
    lines = {line.n: line for line in section.numbered}
    named = Counter(op.get("line") for op in ops if op.get("op") in ("replace", "remove"))
    first, after = {}, {}       # line n → its replacement block; → blocks added after it

    for op in ops:
        kind, n = op.get("op"), op.get("line")
        text, why = (op.get("text") or "").strip(), (op.get("why") or "").strip()
        line = lines.get(n)
        if kind == "add":
            if not text:
                edit.ignored.append(f"add after [{n}] had no text")
            elif n != 0 and line is None:
                edit.ignored.append(f"add after [{n}]: there is no line [{n}]")
            else:
                after.setdefault(n, []).append(render_lines([text], section.style)[0])
                edit.added += 1
        elif kind in ("replace", "remove"):
            if line is None:
                edit.ignored.append(f"{kind} [{n}]: there is no line [{n}]")
            elif line.fixed:
                edit.ignored.append(f"{kind} [{n}]: that line is fixed")
            elif named[n] > 1:
                edit.ignored.append(f"{kind} [{n}]: the line was named twice")
            elif kind == "replace" and not text:
                edit.ignored.append(f"replace [{n}] had no text")
            elif kind == "replace":
                first[n] = _replacement_block(line, text)
                edit.replaced.append((line, why))
            else:
                edit.removed.append((line, why))
        else:
            edit.ignored.append(f"unknown operation {kind!r}")

    # One append per anchor, in page order: a replacement goes straight after the
    # line it replaces, ahead of anything added after that line, so deleting the
    # old line afterwards leaves the order the model asked for.
    for n in sorted(set(first) | set(after)):
        anchor = section.heading_id if n == 0 else lines[n].block_id
        edit.inserts.append((anchor, ([first[n]] if n in first else []) + after.get(n, [])))
    return edit


def apply_section_updates(page_id: str, updates: list, sections: list,
                          new_paths: set | None = None) -> tuple[list, list, list]:
    """Apply each named section's edit operations. Returns (edited, skipped, partial).

    `edited` is the SectionEdit of every section written in full.

    BLOCKING — one Notion round trip per insertion point and per deleted line.
    Run via asyncio.to_thread.

    APPEND FIRST, DELETE AFTER, per section (Hard Rule 2). The IDs to delete come
    from the index built BEFORE anything was written, never from a re-read — a
    re-read after appending would list the new blocks too. A line no operation
    names is never written to at all, which is what keeps its formatting and its
    nested lines.

    THREE buckets, not two. A section in `skipped` is genuinely unchanged: its
    first append failed, so nothing landed and nothing was deleted. A section in
    `partial` had some insertions land before one failed — they are on the page,
    nothing was deleted, and a line being replaced is there twice. That is the
    one state a re-run cannot fix by itself.
    """
    new_paths = new_paths or set()
    edited, skipped, partial = [], [], []

    for upd in updates:
        path = (upd.get("path") or "").strip()
        ops  = [op for op in upd.get("ops") or [] if isinstance(op, dict)]
        if not path or not ops:
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
            edit  = SectionEdit(path)
            texts = []
            for op in ops:
                text = (op.get("text") or "").strip()
                if op.get("op") == "add" and text:
                    texts.append(text)
                else:
                    edit.ignored.append(f"{op.get('op')} [{op.get('line')}]: a new step "
                                        "has no lines to change")
            if not texts:
                skipped.append(f"{path} (no lines to add)")
                continue
            name = path.split(">")[-1].strip()
            blocks = [_heading3(f"→ {name}")] + render_lines(texts, "bullet")
            written, err = append_children(page_id, blocks, after=anchor.tail_id)
            if err:
                # A new step has no stale content to lose, so a half-written one
                # is a half-written ADDITION rather than a mangled section — but
                # it is still not "unchanged".
                (partial if written.partial else skipped).append(f"{path} ({err})")
                continue
            edit.added = len(texts)
            edited.append(edit)
            _log_edit(edit)
            continue

        # ── An existing section: its operations, and nothing else ────────────────
        edit = _plan_edits(section, ops)
        if not edit.inserts and not edit.removed:
            skipped.append(f"{path} (no edit named a line that can be changed)")
            _log_edit(edit)
            continue

        landed, err = 0, None
        for after, blocks in edit.inserts:
            written, err = append_children(page_id, blocks, after=after)
            if err:
                break
            landed += 1
        if err:
            if landed or written.partial:
                done = (f"{landed} of {len(edit.inserts)} insertions" if landed
                        else written.summary)
                partial.append(f"{path} ({done} written, nothing removed: {err})")
            else:
                # Nothing appended, nothing deleted — genuinely unchanged.
                skipped.append(f"{path} ({err})")
            continue

        clear_page_blocks_by_id(edit.doomed_ids)
        edited.append(edit)
        _log_edit(edit)

    return edited, skipped, partial


def _log_edit(edit: SectionEdit) -> None:
    """One line per section: the counts, and why any operation was not applied."""
    logger.info("Implement %s: +%d ~%d -%d, %d ignored%s", edit.path, edit.added,
                len(edit.replaced), len(edit.removed), len(edit.ignored),
                f" ({'; '.join(edit.ignored)})" if edit.ignored else "")


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
            "The target area's database ID must be set on Railway as "
            "`[AREA]_ID` — `BRAIN_ID` for Brain.",
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
        # An outage, not a typo: telling you to check the title would send you
        # after the wrong problem.
        await notify_md(f"❌ Could not search your Learn database for *{escape_md(page_name)}*, "
                        "so nothing was changed. Notion said:")
        await notify(err)
        return
    if source_page is None:
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
            manual_page, err = await asyncio.to_thread(
                search_page_in_db, area_db_id, MANUAL_PAGE_TITLE, exact=True)
            if err:
                # REFUSED, never read as "no Manual yet". That branch is
                # _first_run, which builds a whole Manual from scratch — so a
                # transient failure here used to put a second one beside yours.
                await notify_md(
                    f"❌ Could not check the *{escape_md(area_name)}* database for its Manual, "
                    "so nothing was written — building one now could duplicate the one you "
                    "have. Try again in a minute. Notion said:")
                await notify(err)
                return

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
    await asyncio.to_thread(update_page, source_page_id, {IMPLEMENTED_PROPERTY: {"checkbox": True}})


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
            targets.append({"path": section.path, "style": section.style,
                            "text": section.listing})
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
    # narrow and real: an edit that would replace or remove something already in
    # the Manual on the authority of a recollection. It says nothing about whether
    # what it lets through is TRUE — see the note above _hold_back_rewrites.
    updates = merged.get("updates", [])
    held = []
    if unverified:
        updates, held = _hold_back_rewrites(updates, sections)
        for path, lines in held:
            # The count the open question about this rule asks for (design-notes).
            logger.info("Implement %s: held back %d edit(s) from an unverified source",
                        path, len(lines))

    await notify("📝 Writing the edits to Notion…")
    edited, skipped, partial = await asyncio.to_thread(
        apply_section_updates, manual_page_id, updates, sections, new_paths)

    msg = (f"✅ Manual updated 🔄\n\n"
           f"📍 Area: {escape_md(area_name)}\n"
           f"✏️ *{len(edited)}* of {len(sections)} section(s) edited — the rest were never "
           f"sent to Claude, so they are untouched.\n\n"
           + "".join(f"{line}\n" for line in _format_edits(edited))
           + ("\n" if edited else "")
           + f"_Source used: {escape_md(source_title)}_")
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
        msg += ("\n\n🚨 Half-written — open these in Notion: part of the edit is "
                "there and nothing was removed, so a line being replaced is there twice:\n"
                + "\n".join(f"• {escape_md(s)}" for s in partial[:8]))
    if held:
        # A THIRD kind of unchanged, and its own heading for the same reason the
        # other two have theirs: this one is not Notion failing, it is David
        # refusing. The lines are named, because "an edit was held back" is not
        # something you can act on and "these two lines would have been replaced
        # by a recollection" is. Its additions, if any, were written above.
        msg += ("\n\n🛡️ Held back — an unverified source can add lines but not "
                "change or remove one. These lines are unchanged:\n"
                + "\n".join(f"• {escape_md(path)} — "
                            + "; ".join(f"“{escape_md(_short(line))}”" for line in lines)
                            for path, lines in held))
    await notify_md(msg)

    # The ledger, after the writes and only on a run that got this far. `sections`
    # is the index from before the writes and is still correct for Sources — the
    # one section apply_section_updates is not allowed to touch.
    await _record(manual_page_id, sections, source_title, unverified, notify=notify)
    return True


def _short(text: str, limit: int = 60) -> str:
    """A line cut to `limit` characters for a reply, marked when it was cut."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _format_edits(edited: list) -> list:
    """The diff, one line per section plus one per replaced or removed line.

    Additions are counted, not quoted: they are new, and the page shows them. A
    replaced or removed line is quoted with Claude's reason, because the page no
    longer shows it — this reply is the only place it is still visible.

    Escaped here: the paths, the old lines and the reasons are all Claude's or
    yours. bot/implement.py splits the message if it outgrows Telegram's limit.
    """
    out = []
    for edit in edited:
        counts = [label for count, label in (
            (edit.added,         f"+{edit.added} added"),
            (len(edit.replaced), f"~{len(edit.replaced)} sharpened"),
            (len(edit.removed),  f"−{len(edit.removed)} removed"),
            (len(edit.ignored),  f"{len(edit.ignored)} ignored")) if count]
        out.append(f"*{escape_md(edit.path)}* — " + " · ".join(counts))
        for mark, changed in (("~", edit.replaced), ("−", edit.removed)):
            for line, why in changed:
                out.append(f"  {mark} “{escape_md(_short(line.text))}”"
                           + (f" — _{escape_md(_short(why, 100))}_" if why else ""))
    if any(edit.ignored for edit in edited):
        out.append("_ignored — an edit that named a missing line, a line David never "
                   "rewrites (lines nested under it, or not plain text), the same line "
                   "twice, or had no text. Nothing was done for it._")
    return out


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
