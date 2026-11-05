"""Edit operations on the numbered lines of one section.

A merge used to return each section's FULL content, and the writer replaced every
block in it. So a line Claude kept word for word was still deleted and written back
as plain text: its bold label went, and so did everything nested under it, which a
one-level `get_children` had never even read. Both merges that write into Notion —
the flat Manual (`services/implement.py`) and the Diet page
(`services/implement_diet.py`) — now show Claude each section as numbered lines and
get back operations on them: add after [n], replace [n], remove [n]. A line no
operation names is never written to.

This module is the one place those operations are numbered, checked, written and
reported, so the two paths cannot drift apart in what they protect.

WHAT STAYS WITH EACH CALLER: its prompt and schema, where a section's lines live
(a flat page's sibling blocks, or a toggle heading's children), what line 0 means
there, how a new line is styled, and any section the caller refuses outright (the
Manual's Sources ledger).

NO NOTION I/O OF ITS OWN. `write_edit` is handed the caller's `append_children`
and delete function, so the writes still go through the caller's module — the
functions its tests replace.
"""

import logging
from collections import Counter

from clients.notion_client import extract_rich_text, rich
from telegram_text import escape_md

logger = logging.getLogger(__name__)

# The block types a merge may replace or remove: each is one run of rich text
# that can be read whole and written back in the same type. Any other block (a
# callout, a quote, a to-do, an image) is shown to the model as FIXED — it can be
# added after, never replaced or removed.
EDITABLE_TYPES = ("paragraph", "bulleted_list_item", "numbered_list_item")


def op_schema(line_zero: str) -> dict:
    """One edit operation, as both merge schemas ask Claude for it.

    `line_zero` is what an add at line 0 means where the caller writes: the top
    of a flat page's section, the end of a toggle heading's children.
    """
    return {
        "type": "object",
        "properties": {
            "op":   {"type": "string", "enum": ["add", "replace", "remove"]},
            "line": {"type": "integer",
                     "description": "replace / remove: the line's [n]. "
                                    f"add: the [n] it goes after; 0 = {line_zero}."},
            "text": {"type": "string",
                     "description": "add / replace: the line as it should read. "
                                    "remove: empty."},
            "why":  {"type": "string",
                     "description": "One short line: what in the source justifies this."},
        },
        "required": ["op", "line", "text", "why"],
        "additionalProperties": False,
    }


# ─── 1. NUMBERED LINES ─────────────────────────────────────────────────────────

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


def number_lines(blocks: list) -> list:
    """A section's blocks as the merge call is shown them: [1], [2], ….

    The numbers index THIS list, built from the read taken before anything is
    written, and the ops that come back are resolved against the same list. So
    [3] always means the block it meant when the prompt was built.

    A blank text block (spacing) is neither numbered nor touched. A block that is
    not plain text, or that has blocks nested under it, is FIXED: `get_children`
    reads one level, so its nested lines were never read, and replacing or
    removing it would delete them unseen.
    """
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        text = extract_rich_text(block.get(btype, {}).get("rich_text", []))
        editable = btype in EDITABLE_TYPES and not block.get("has_children")
        if editable and not text.strip():
            continue
        lines.append(Line(len(lines) + 1, block,
                          text.strip() or f"({btype.replace('_', ' ')})",
                          fixed=not editable))
    return lines


def listing(lines: list) -> str:
    """The numbered lines as one string, for a merge prompt.

    Whitespace is flattened: a soft line break inside a block would otherwise
    start an unnumbered row the model could read as a line of its own.
    """
    return "\n".join(f"[{line.n}]{' (fixed)' if line.fixed else ''} "
                     + " ".join(line.text.split())
                     for line in lines)


# ─── 2. CHECKING THE OPERATIONS ────────────────────────────────────────────────

class SectionEdit:
    """What one section's operations come to, checked against its numbered lines.

    Also the record the reply is built from, so the diff a person reads is the
    edit that was written, not the answer Claude gave.
    """

    __slots__ = ("path", "inserts", "added", "replaced", "removed", "ignored")

    def __init__(self, path):
        self.path     = path
        self.inserts  = []     # [(after_id or None, [blocks])], in page order
        self.added    = 0
        self.replaced = []     # [(Line, why)]
        self.removed  = []     # [(Line, why)]
        self.ignored  = []     # [reason], one per operation not applied

    @property
    def doomed_ids(self) -> list:
        """What is deleted once every insert has landed: the replaced and removed lines."""
        return [line.block_id for line, _ in self.replaced + self.removed]


def replacement_block(line: Line, text: str) -> dict:
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


def plan_edits(path: str, lines: list, ops: list, *, render, top) -> SectionEdit:
    """Resolve a section's operations against its numbered lines. No I/O.

    `render(text)` builds a new line's block in the section's style. `top` is
    where an add at line 0 goes: a block id to insert after, or None for the end
    of the parent's children.

    An operation that names a line which is not there, a FIXED line, or a line
    another replace or remove also names is ignored, and the reason kept. Two
    edits to one line contradict each other; keeping the line is the direction
    that loses nothing.
    """
    edit  = SectionEdit(path)
    by_n  = {line.n: line for line in lines}
    named = Counter(op.get("line") for op in ops if op.get("op") in ("replace", "remove"))
    first, after = {}, {}       # line n → its replacement block; → blocks added after it

    for op in ops:
        kind, n = op.get("op"), op.get("line")
        text, why = (op.get("text") or "").strip(), (op.get("why") or "").strip()
        line = by_n.get(n)
        if kind == "add":
            if not text:
                edit.ignored.append(f"add after [{n}] had no text")
            elif n != 0 and line is None:
                edit.ignored.append(f"add after [{n}]: there is no line [{n}]")
            else:
                after.setdefault(n, []).append(render(text))
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
                first[n] = replacement_block(line, text)
                edit.replaced.append((line, why))
            else:
                edit.removed.append((line, why))
        else:
            edit.ignored.append(f"unknown operation {kind!r}")

    # One append per anchor, in page order: a replacement goes straight after the
    # line it replaces, ahead of anything added after that line, so deleting the
    # old line afterwards leaves the order the model asked for.
    for n in sorted(set(first) | set(after)):
        anchor = top if n == 0 else by_n[n].block_id
        edit.inserts.append((anchor, ([first[n]] if n in first else []) + after.get(n, [])))
    return edit


def hold_back_rewrites(updates: list, lines_for) -> tuple[list, list]:
    """Split an unverified merge into (may_be_written, held_back).

    THE ADDITIONS-ONLY RULE, checked rather than asked for. Every `add` may be
    written. Every other operation is held back, named by the line it would have
    changed, as (path, [line text, …]). `lines_for(path)` is the section's
    numbered lines, or None for a path the caller does not know.

    WHAT THIS ENFORCES: no line already on the page is replaced or removed on the
    authority of a recollection. Deterministic — it reads the KIND of each
    operation, so it needs no judgement from anyone.

    WHAT IT DOES NOT ENFORCE, and must never be described as enforcing: anything
    at all about the lines it ADDS. Nothing can check whether a recollected fact
    is true.

    Runs BEFORE the writer rather than inside it. What may be written is a policy
    decision about this source; the writer stays a writer, and its buckets keep
    describing what Notion did rather than what David decided.
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
            texts = {line.n: line.text for line in lines_for(path) or []}
            held.append((path, [texts.get(op.get("line"), f"line [{op.get('line')}]")
                                for op in rest]))
    return allowed, held


# ─── 3. WRITING ONE SECTION ────────────────────────────────────────────────────

def write_edit(parent_id: str, edit: SectionEdit, *, append_children, delete_ids):
    """Write one section's edit. Returns (bucket, note).

    BLOCKING — one Notion round trip per insertion point and per deleted line.

    `bucket` is "edited", "skipped" or "partial", and `note` says why for the
    last two. `append_children(parent_id, blocks, after=…)` and `delete_ids(ids)`
    are the caller's.

    APPEND FIRST, DELETE AFTER (Hard Rule 2). The IDs to delete come from the
    lines numbered BEFORE anything was written, never from a re-read — a re-read
    after appending would list the new blocks too. A line no operation names is
    never written to at all, which is what keeps its formatting and its nested
    lines.

    THREE buckets, not two. "skipped" is genuinely unchanged: the first append
    failed, so nothing landed and nothing was deleted. "partial" had some
    insertions land before one failed — they are on the page, nothing was
    deleted, and a line being replaced is there twice. That is the one state a
    re-run cannot fix by itself.
    """
    if not edit.inserts and not edit.removed:
        log_edit(edit)
        return "skipped", "no edit named a line that can be changed"

    landed, err, written = 0, None, None
    for after, blocks in edit.inserts:
        written, err = append_children(parent_id, blocks, after=after)
        if err:
            break
        landed += 1
    if err:
        if landed or written.partial:
            done = (f"{landed} of {len(edit.inserts)} insertions" if landed
                    else written.summary)
            return "partial", f"{done} written, nothing removed: {err}"
        # Nothing appended, nothing deleted — genuinely unchanged.
        return "skipped", err

    delete_ids(edit.doomed_ids)
    log_edit(edit)
    return "edited", None


def log_edit(edit: SectionEdit) -> None:
    """One line per section: the counts, and why any operation was not applied."""
    logger.info("Implement %s: +%d ~%d -%d, %d ignored%s", edit.path, edit.added,
                len(edit.replaced), len(edit.removed), len(edit.ignored),
                f" ({'; '.join(edit.ignored)})" if edit.ignored else "")


# ─── 4. THE REPLY ──────────────────────────────────────────────────────────────

def short(text: str, limit: int = 60) -> str:
    """A line cut to `limit` characters for a reply, marked when it was cut."""
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def format_edits(edited: list) -> list:
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
                out.append(f"  {mark} “{escape_md(short(line.text))}”"
                           + (f" — _{escape_md(short(why, 100))}_" if why else ""))
    if any(edit.ignored for edit in edited):
        out.append("_ignored — an edit that named a missing line, a line David never "
                   "rewrites (lines nested under it, or not plain text), the same line "
                   "twice, or had no text. Nothing was done for it._")
    return out


def format_held(held: list) -> str:
    """The held-back lines of an unverified merge, for the reply. "" when none.

    A kind of unchanged with its own heading: this one is not Notion failing, it
    is David refusing. The lines are named, because "an edit was held back" is
    not something you can act on and "these two lines would have been replaced
    by a recollection" is.
    """
    if not held:
        return ""
    return ("\n\n🛡️ Held back — an unverified source can add lines but not "
            "change or remove one. These lines are unchanged:\n"
            + "\n".join(f"• {escape_md(path)} — "
                        + "; ".join(f"“{escape_md(short(line))}”" for line in lines)
                        for path, lines in held))
