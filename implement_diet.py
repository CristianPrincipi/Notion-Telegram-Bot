import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

from clients.anthropic_client import complete_json
from config import ANTHROPIC_TIMEOUT
from page_lock import PageBusy, page_lock
from telegram_text import escape_md, reply
from clients.notion_client import (
    search_page_in_db, get_children, blocks_to_text,
    append_children, delete_block, create_page, extract_rich_text, rich,
    update_page,
    bullet as _bullet,
)

# ─── ENV ───────────────────────────────────────────────────────────────────────
LEARN_ID      = os.environ.get("LEARN_ID")
DIET_ID       = os.environ.get("DIET_ID")

# ─── DIET PAGE BLUEPRINT (translated from the handwritten diagram) ─────────────
# H1 → list of H2 rows. Each H2 row carries the same set of H3 attributes.
# This is the source-of-truth skeleton built on first run.

DIET_STRUCTURE = {
    "Mediterranean Diet": {
        "rows": ["Principles", "Allowed Foods", "Limited Foods", "Benefits", "Evidence"],
        "attributes": [],   # rows are leaf-level here (content goes directly inside the H2)
    },
    "Seasonality": {
        "rows": ["Fruit", "Vegetables", "Fish"],
        "attributes": [],
    },
    "Goals": {
        "rows": ["Fat Loss", "Muscle Mass", "Recomposition", "Gut Health"],
        "attributes": ["Strategies", "Foods", "Supplements", "Mistakes to Avoid", "Evidence"],
    },
    "Supplementation": {
        "rows": ["Fundamentals", "Performance", "Recovery", "Sleep", "Health"],
        "attributes": ["What It's For", "Dosage", "Timing", "Cost / Benefit", "Evidence"],
    },
}

# Evidence sub-fields (the boxed list in the diagram: Question/Result/Limits/Practical Conclusion)
EVIDENCE_FIELDS = ["Question", "Result", "Limits", "Practical Conclusion"]


# ─── 1. TOGGLE BLOCK BUILDERS (Diet-specific) ──────────────────────────────────
# Toggleable headings are unique to this module — the shared client has only
# plain headings. Bullets/paragraphs come from notion_client.

def _toggle_heading(text: str, level: int, children: list = None) -> dict:
    """A toggleable heading (H1/H2/H3). children render inside the toggle."""
    htype = f"heading_{level}"
    payload = {
        "rich_text": rich(text),
        "is_toggleable": True,
        "color": "default",
    }
    block = {"object": "block", "type": htype, htype: payload}
    if children:
        block[htype]["children"] = children[:100]
    return block


def build_full_skeleton() -> list:
    """Build the complete empty Diet structure (H1>H2>H3) per the blueprint.
    Used only on first run. Empty sections stay empty."""
    h1_blocks = []
    for h1_name, spec in DIET_STRUCTURE.items():
        h2_blocks = []
        for row in spec["rows"]:
            attrs = spec["attributes"]
            if attrs:
                # Build H3 toggles inside this H2
                h3_blocks = []
                for attr in attrs:
                    h3_children = []
                    if attr == "Evidence":
                        # Evidence gets its boxed sub-fields as empty bullets
                        h3_children = [_bullet(f"{f}: ") for f in EVIDENCE_FIELDS]
                    h3_blocks.append(_toggle_heading(attr, 3, h3_children))
                h2_blocks.append(_toggle_heading(row, 2, h3_blocks))
            else:
                # Leaf row — no H3, just an empty H2 toggle
                h2_blocks.append(_toggle_heading(row, 2))
        h1_blocks.append(_toggle_heading(h1_name, 1, h2_blocks))
    return h1_blocks


# ─── 2. READ EXISTING TREE ─────────────────────────────────────────────────────

# Notion has no recursive block read — children come one request per parent — so
# the three-level Diet tree costs ~67 requests on a populated skeleton
# (1 page + 4 H1 + 17 H2 + 45 H3). Walked depth-first and sequentially that is
# the slowest thing David does, and every one of those round trips is spent
# waiting rather than working.
#
# Siblings at a level are independent, so read_diet_structure walks BREADTH-first
# and fetches each level concurrently instead.
#
# Four workers, not more. Notion rate-limits an integration to roughly three
# requests per second on average, so a wider pool mostly buys 429s and the
# retry backoff in notion_request — slower overall, and rude. Four is enough to
# keep requests in flight while others wait on the wire.
_TREE_FETCH_WORKERS = 4


def _heading_name(block: dict, level: int) -> str | None:
    """The text of a toggle heading at `level`, or None if it is not one.

    startswith rather than ==: a toggleable heading is still type "heading_2".
    """
    if not block.get("type", "").startswith(f"heading_{level}"):
        return None
    return extract_rich_text(block[f"heading_{level}"]["rich_text"]) or None


def _children_of_many(block_ids: list) -> tuple[dict, str | None]:
    """Fetch the children of many blocks at once. Returns ({block_id: blocks}, error).

    Any error fails the whole read. The previous depth-first version discarded
    errors below the top level (`h2_blocks, _ = get_children(...)`), which meant
    a transient failure reading one section made that section look EMPTY rather
    than unreadable — and an empty section is exactly what makes Claude decide to
    populate it. apply_updates would then replace real content with content
    merged against nothing. Failing the read is the safe direction: the handler
    reports it and writes nothing.
    """
    if not block_ids:
        return {}, None

    children, error = {}, None
    workers = min(_TREE_FETCH_WORKERS, len(block_ids))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="diet-tree") as pool:
        for block_id, (blocks, err) in zip(block_ids, pool.map(get_children, block_ids)):
            if err:
                error = error or err
            else:
                children[block_id] = blocks
    return children, error


def read_diet_structure(page_id: str):
    """Read the page's section TAXONOMY — levels 1-3, and no content at all.

    BLOCKING — Notion requests, four at a time. handle_implement_diet runs it via
    asyncio.to_thread.

    Returns (tree, block_map, error) where:
      tree      = {h1: {h2: {h3: {}, ...}, ...}, ...}  — every node is a dict of
                  its children, a leaf is {}
      block_map = {"h1>h2>h3": block_id}  — how a path is located for reading or
                  writing later

    WHY THIS NO LONGER READS CONTENT
    --------------------------------
    It used to be `read_diet_tree`, and it fetched every section's text so the
    whole page could be serialised into one prompt. Routing only needs the PATHS,
    so levels 1-3 are enough to produce them, and the ~45 level-4 requests that
    fetched H3 leaf content are now made after routing, for the handful of
    sections that turned out to be affected. See read_section_contents.

    ONE SHAPE AT EVERY LEVEL. The old tree returned a `str` for an H2 holding leaf
    content and a `dict` for an H2 holding H3 toggles, so the same position in the
    structure had two types and every consumer had to test which.

    AND NO `content` FIELD, deliberately. Carrying one would mean writing `""` for
    every section not yet fetched — and `""` is also what a genuinely empty
    section looks like. That is the empty-versus-unknown collapse that already
    cost this module a content overwrite once (see _children_of_many): a section
    that reads as empty is exactly what makes Claude decide to populate it. A
    structure that does not claim to know content cannot be wrong about it.
    """
    tree, block_map = {}, {}

    # ── Level 1: the page's own children ───────────────────────────────────────
    h1_blocks, err = get_children(page_id)
    if err:
        return {}, {}, err

    h1s = []                                   # [(h1_name, h1_block), ...]
    for h1 in h1_blocks:
        h1_name = _heading_name(h1, 1)
        if not h1_name:
            continue
        tree[h1_name] = {}
        block_map[h1_name] = h1["id"]
        h1s.append((h1_name, h1))

    # ── Level 2: every H1's children, in one pass ──────────────────────────────
    # has_children is already on the parent payload, so a childless block is
    # never asked for — that alone skips most of the empty skeleton.
    h2_children, err = _children_of_many(
        [h1["id"] for _, h1 in h1s if h1.get("has_children")])
    if err:
        return {}, {}, err

    h2s = []                                   # [(h1_name, h2_name, h2_block), ...]
    for h1_name, h1 in h1s:
        for h2 in h2_children.get(h1["id"], []):
            h2_name = _heading_name(h2, 2)
            if not h2_name:
                continue
            block_map[f"{h1_name}>{h2_name}"] = h2["id"]
            h2s.append((h1_name, h2_name, h2))

    # ── Level 3: every H2's children, in one pass ──────────────────────────────
    h3_children, err = _children_of_many(
        [h2["id"] for _, _, h2 in h2s if h2.get("has_children")])
    if err:
        return {}, {}, err

    for h1_name, h2_name, h2 in h2s:
        tree[h1_name][h2_name] = {}
        if not h2.get("has_children"):
            continue                   # a leaf row — its content is fetched later

        blocks = h3_children.get(h2["id"], [])
        # An H2 either holds H3 toggles, or leaf content directly. The content
        # case is a leaf here too: {} either way, and read_section_contents is
        # what tells them apart when the content is actually wanted.
        for h3 in blocks:
            h3_name = _heading_name(h3, 3)
            if not h3_name:
                continue
            block_map[f"{h1_name}>{h2_name}>{h3_name}"] = h3["id"]
            tree[h1_name][h2_name][h3_name] = {}

    return tree, block_map, None


def content_paths(tree: dict) -> list:
    """The paths that can actually hold content, in page order.

    Leaf H2 rows and H3 attributes — never an H1 category, and never an H2 that
    holds H3 toggles. Those are containers: the blueprint puts content under
    them, not in them, and offering one to the router only creates a way for it
    to name a section nothing can be written to.
    """
    paths = []

    def walk(node, prefix):
        for name, children in node.items():
            path = f"{prefix}>{name}" if prefix else name
            if children:
                walk(children, path)
            elif prefix:               # depth >= 2 — H1 categories are excluded
                paths.append(path)

    walk(tree, "")
    return paths


def read_section_contents(sections: dict):
    """Fetch the current content of specific sections. Returns (contents, error).

    BLOCKING — one Notion request per section, four at a time.

    `sections` is {path: block_id}; the result is {path: "text content"}. This is
    the level-4 read that read_diet_structure no longer does up front, and it now
    runs only for the sections routing selected — a handful rather than ~45.

    Any failed read fails the WHOLE fetch, for the reason in _children_of_many:
    a section that failed to read is indistinguishable from an empty one once the
    error is dropped, and an empty section is what makes Claude populate it.
    """
    if not sections:
        return {}, None

    paths = list(sections)
    children, err = _children_of_many([sections[p] for p in paths])
    if err:
        return {}, err

    contents = {}
    for path in paths:
        blocks = children.get(sections[path], [])
        # Nested toggle headings are structure, not this section's content — the
        # same filter apply_updates uses to decide what is replaceable, so what
        # the model is shown is exactly what a rewrite would replace.
        leaf = [b for b in blocks if not b.get("type", "").startswith("heading_")]
        contents[path] = blocks_to_text(leaf, style="plain")
    return contents, None


# ─── 3. THE TWO CLAUDE CALLS ───────────────────────────────────────────────────
#
# WHY TWO CALLS AND NOT ONE
# -------------------------
# `decide_updates` used to send CURRENT_TREE — the whole page, serialised — plus
# the summary, on EVERY run. Three things were wrong with that:
#
#   • Cost and latency scaled with the size of the knowledge base rather than the
#     size of the update. A one-paragraph summary about creatine paid to ship
#     every seasonality note and every fat-loss strategy along with it.
#   • The payload was sliced at `[:30000]`. On a page with four bullets per
#     section the tree JSON is already ~20k characters, so the tail of the page
#     was heading for the same silent truncation `manual_text[:40000]` inflicted
#     on the flat Manuals — dropped from the prompt with nothing raised, and
#     therefore dropped from the model's view of what already exists.
#   • Judgement degrades on noise. Most of that payload was irrelevant to any
#     given summary.
#
# So it splits, the same way implement.py already does it:
#
#   ROUTE — the section paths, names ONLY, plus the summary. "Which of these does
#           this inform?" A few hundred tokens of taxonomy.
#   MERGE — the current content of ONLY the routed sections, plus the summary.
#           "Here is what those sections say now; return their full merged text."
#
# Measured on a populated page: ~5,800 input tokens down to ~2,500, and ~45
# Notion reads deferred to the sections that turn out to matter. The summary is
# in both calls, which is why the saving is ~56% rather than ~90%.
#
# The routing call is the one whose failures are INVISIBLE — a section it does
# not name is never fetched, never merged, never written, and the run still
# reports success. That is why it stays on config.ANTHROPIC_MODEL rather than
# being moved to a cheaper model to save a fraction of a cent per run, and why
# handle_implement_diet accounts for every path at every stage below.

_ROUTE_SYSTEM = """You route newly learned content into a structured personal DIET page in Notion.

You are given the SECTION PATHS of the page (names only, no content) and a SUMMARY of
something newly learned. Decide which sections the summary actually informs.

The hierarchy is: category > row > attribute. Every path you are given can hold content.

Rules:
- Only name a section if the SUMMARY genuinely adds to, corrects, or sharpens it.
  Sections the summary says nothing about must be left out — they will not be touched,
  which is the point.
- "path" MUST be copied EXACTLY from the SECTIONS list, including the '>' separators.
- Prefer the most specific section. A fact about creatine dosing belongs in that
  supplement's Dosage attribute, not in the whole Supplementation category.
- Name every section the summary genuinely informs — a summary often touches several.
- If the summary informs nothing on this page, return an empty list."""

_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "affected": {
            "type": "array",
            "description": "The sections this summary informs.",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Exactly as given in SECTIONS."},
                    "why":  {"type": "string", "description": "One short line."},
                },
                "required": ["path"],
            },
        },
    },
    "required": ["affected"],
}


def route_sections(paths: list, summary_text: str, summary_title: str):
    """Which sections does this summary touch? Returns (routing, error).

    Deliberately cheap: section PATHS only, never their content. That is what
    keeps the untouched majority of the page out of the model entirely.
    """
    listing = "\n".join(f"- {p}" for p in paths)
    user_msg = (
        f"=== SECTIONS (names only) ===\n{listing}\n\n"
        f"=== SUMMARY: {summary_title} ===\n{summary_text[:50000]}"
    )
    return complete_json(_ROUTE_SYSTEM, user_msg, _ROUTE_SCHEMA, max_tokens=2048)


_MERGE_SYSTEM = """You merge newly learned content into specific sections of a personal DIET page.

For each section you are given its current content and the new SUMMARY. Return the FULL
merged content for that section — it replaces what is there.

Rules:
- Merge, never concatenate. Each fact appears exactly once, in its best form.
- Keep everything in the current content that the summary does not supersede.
  Content you leave out is deleted.
- Store ACTIONABLE information, not raw notes. Each bullet is a concrete, standalone statement.
- mode "merge": you combined your bullets with the existing content, removing duplicates
  and keeping the stronger version.
- mode "replace": the existing content is outdated or wrong and the summary supersedes it.
- For Evidence sections, structure bullets as "Question: …", "Result: …", "Limits: …",
  "Practical Conclusion: …" when the summary provides them.
- Do NOT invent content. Do NOT infer beyond the summary.
- "path" MUST be copied EXACTLY from the section headers given to you."""

# `updates` is byte-for-byte the shape apply_updates already consumes, so the
# write path — append-then-delete, and both modes replacing the section's leaf
# content — is untouched by this split.
_MERGE_SCHEMA = {
    "type": "object",
    "properties": {
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string",
                                "description": "Exactly matching a section header given to you."},
                    "mode":    {"type": "string", "enum": ["merge", "replace"]},
                    "bullets": {"type": "array", "items": {"type": "string"},
                                "description": "The FULL merged content for this section."},
                },
                "required": ["path", "mode", "bullets"],
            },
        },
        "conflicts": {"type": "array", "items": {"type": "string"},
                      "description": "Contradictions found between the summary and the "
                                     "existing content, and how you resolved them."},
    },
    "required": ["updates"],
}


def merge_sections(contents: dict, summary_text: str, summary_title: str):
    """Merge the summary into the given sections. Returns (result, error).

    `contents` is {path: current text} for the routed sections ONLY. Everything
    else on the page is absent from this prompt, which is the only guarantee that
    it comes back unchanged — because it never went.
    """
    parts = []
    for path, text in contents.items():
        body = text.strip() or "(empty — this section has no content yet)"
        parts.append(f"--- SECTION: {path} ---\n{body}")

    user_msg = (
        "=== SECTIONS TO MERGE ===\n" + "\n\n".join(parts) +
        f"\n\n=== SUMMARY: {summary_title} ===\n{summary_text[:50000]}"
    )
    return complete_json(_MERGE_SYSTEM, user_msg, _MERGE_SCHEMA)


# ─── 4. APPLY UPDATES SURGICALLY ───────────────────────────────────────────────

def apply_updates(updates: list, block_map: dict):
    """For each update, locate the target section block and refresh its content.

    BLOCKING — several Notion round trips per section touched. Run via
    asyncio.to_thread, never directly from the event loop.

    BOTH modes replace the section's leaf content. _DIET_SYSTEM instructs Claude
    to return the FULL merged content of every section it touches — "merge" means
    it merged against the existing bullets itself and removed duplicates, not
    that Notion should append to what is already there.

    Treating "merge" as append-only (the previous behaviour) wrote that full
    merged superset ON TOP of the bullets it already contained. Every run
    therefore duplicated the section, and because the next run reads that content
    straight back out (read_section_contents) and feeds it to the merge, the bloat
    compounded run over run and degraded every subsequent merge. Nothing errored —
    the page just grew.

    Sections not in `updates` are never touched.
    Returns (applied_count, skipped_paths).
    """
    applied, skipped = 0, []

    for upd in updates:
        path    = upd.get("path", "").strip()
        bullets = [b for b in upd.get("bullets", []) if b and b.strip()]
        if not path or not bullets:
            continue

        # Match the path to a known block id (tolerate spacing around '>')
        block_id = _resolve_path(path, block_map)
        if not block_id:
            skipped.append(path)
            continue

        # APPEND FIRST, DELETE AFTER — see page_lock.py. Notion has no
        # transactions, so clearing before the append means a failed append
        # leaves the section permanently empty with nothing to restore from.
        # Snapshot what to remove, write the replacement, and only delete once
        # the replacement is committed.
        existing, err = get_children(block_id)
        if err:
            skipped.append(f"{path} ({err})")
            continue

        # Only leaf content is replaceable — nested toggle headings are structure.
        stale_ids = [b["id"] for b in existing
                     if not b.get("type", "").startswith("heading_") and b.get("id")]

        _, err = append_children(block_id, [_bullet(b) for b in bullets])
        if err:
            # Nothing deleted — the section still holds its previous content.
            skipped.append(f"{path} ({err})")
            continue

        for block_id_to_drop in stale_ids:
            delete_block(block_id_to_drop)
        applied += 1

    return applied, skipped


def _path_key(path: str) -> str:
    """The comparable form of a path.

    Spacing around '>' and letter case are not meaningful — Claude copies paths
    back from a prompt and does not always reproduce them byte for byte. One
    function so that "does this path resolve to a block?" and "did the merge
    return this path?" cannot answer differently for the same pair of strings,
    which would put a section in two buckets of the report or none.
    """
    return ">".join(p.strip() for p in (path or "").split(">")).lower()


def _resolve_path(path: str, block_map: dict):
    """Find a block id for a path, tolerant to spacing and case differences."""
    key = _path_key(path)
    for known, block_id in block_map.items():
        if _path_key(known) == key:
            return block_id
    return None


# ─── 5. NOTION PAGE / SKELETON SETUP ───────────────────────────────────────────

def find_or_create_diet_page():
    """Find the 'Diet' page in DIET_ID, or create it with the full skeleton.
    Returns (page_id, was_created, error)."""
    page, _ = search_page_in_db(DIET_ID, "Diet", exact=True)
    if page:
        return page["id"], False, None

    # Create new empty page first (skeleton appended in a second pass because
    # Notion only nests 2 levels deep per create request).
    page_id, err = create_page(
        DIET_ID,
        {"Name": {"title": [{"text": {"content": "Diet"}}]}},
        icon="🥗",
    )
    if not page_id:
        return None, False, err

    err = _append_skeleton_deep(page_id)
    return page_id, True, err


def _append_skeleton_deep(page_id: str):
    """Append the skeleton respecting Notion's 2-level nesting-per-request limit.
    Creates H1 (with H2 children), then appends H3 toggles into each H2 afterward."""
    for h1_name, spec in DIET_STRUCTURE.items():
        attrs = spec["attributes"]

        # Build H2 children (without H3 yet — H3 added in a second pass)
        h2_children = [_toggle_heading(row, 2) for row in spec["rows"]]
        h1_block = _toggle_heading(h1_name, 1, h2_children)

        created, err = append_children(page_id, [h1_block])
        if err:
            return err
        if not attrs:
            continue  # leaf rows, no H3 needed

        # Find the created H1, read its H2 children, append H3 into each
        h1_id = created[0]["id"]
        h2_blocks, _ = get_children(h1_id)
        for h2 in h2_blocks:
            if not h2.get("type", "").startswith("heading_2"):
                continue
            h3_children = []
            for attr in attrs:
                ev_children = [_bullet(f"{f}: ") for f in EVIDENCE_FIELDS] if attr == "Evidence" else None
                h3_children.append(_toggle_heading(attr, 3, ev_children))
            _, err = append_children(h2["id"], h3_children)
            if err:
                return err
    return None


# ─── 6. MAIN HANDLER ───────────────────────────────────────────────────────────

async def handle_implement_diet(update, summary_name: str):
    """
    Called by implement.handle_implement when the target area is "Diet".
    Command format:  Implement [Summary Name] - Diet

    Flow:
      A) Find [Summary Name] in LEARN_ID
      B) Find or build the Diet page (full skeleton on first run)
      C) Read the section taxonomy — paths only, no content
      D) ROUTE: which sections does this summary inform?
      E) Read the current content of ONLY those sections
      F) MERGE: their merged content
      G) Apply surgical updates; report what happened to every routed path
      H) Tick the source page's 'Implemented' checkbox (feeds the Learn-nudge job)

    EVERY ROUTED PATH IS ACCOUNTED FOR. A path can fall out at three points — it
    may not resolve to a block, its content read may fail, or the merge may
    decline to return it — and each of those is reported rather than dropped. A
    section quietly missing from the write is indistinguishable from a section
    the summary had nothing to say about, and only one of those is fine.
    """

    summary_name = summary_name.strip()

    if not DIET_ID:
        await reply(update, "❌ `DIET_ID` is not set in your Railway environment variables.")
        return

    # ── Step A: find the summary in Learn DB ───────────────────────────────────
    await reply(update, f"🔍 Searching for *{escape_md(summary_name)}* in Learn database…")
    summary_page, err = await asyncio.to_thread(search_page_in_db, LEARN_ID, summary_name)
    if err:
        await reply(
            update,
            f"❌ Could not find *{escape_md(summary_name)}* in your Learn database.\n"
            "Make sure you used `Learn` to save it and the title matches.",
        )
        return

    summary_id = summary_page["id"]
    summary_title = extract_rich_text(
        next((p.get("title", []) for p in summary_page.get("properties", {}).values()
              if p.get("type") == "title"), [])
    )

    summary_blocks, err = await asyncio.to_thread(get_children, summary_id)
    if err:
        await update.message.reply_text(f"❌ Could not read the summary: {err}")
        return
    summary_text = blocks_to_text(summary_blocks)
    if not summary_text.strip():
        await update.message.reply_text("❌ The summary page appears to be empty.")
        return

    # ── Steps B–E run under a lock on the Diet DATABASE ────────────────────────
    # Keyed on DIET_ID, not on the Diet page's own id, and taken BEFORE the
    # find-or-create rather than after it. Both matter:
    #
    #   - find_or_create_diet_page is a check-then-act. Outside the lock, two
    #     overlapping runs both find nothing and both create a "Diet" page with a
    #     full skeleton. Every later run then picks one of them arbitrarily, so
    #     half the knowledge lands in a page nobody reads. Nothing errors.
    #   - the page id is not known until that call returns, so a lock keyed on it
    #     could not have covered the call that produces it.
    #
    # It is also taken before read_diet_structure: Claude routes and merges from
    # the structure and content it was given, so those decisions are only valid
    # while nobody else is mutating them.
    try:
        async with page_lock(DIET_ID):
            # ── Step B: find or create the Diet page ───────────────────────────────────
            # On a first run this also builds the whole skeleton — dozens of writes.
            page_id, was_created, err = await asyncio.to_thread(find_or_create_diet_page)
            if err:
                await update.message.reply_text(f"❌ Could not prepare the Diet page: {err}")
                return
            if was_created:
                await update.message.reply_text(
                    "🥗 First run — built the full Diet structure in Notion.")

            # ── Step C: read the taxonomy — paths only, no content ─────────────────────
            await update.message.reply_text("📂 Reading current Diet structure…")
            tree, block_map, err = await asyncio.to_thread(read_diet_structure, page_id)
            if err:
                await update.message.reply_text(f"❌ Could not read the Diet structure: {err}")
                return

            paths = content_paths(tree)
            if not paths:
                await update.message.reply_text(
                    "❌ The Diet page has no sections to update. Delete the page and "
                    "re-run to rebuild the structure."
                )
                return

            # ── Step D: ROUTE — which sections does this inform? ───────────────────────
            await update.message.reply_text(
                f"🧭 Checking which of the {len(paths)} sections this affects…")
            # Safe to time out: nothing has been written yet, and the source page
            # is not yet marked implemented.
            try:
                routing, err = await asyncio.wait_for(
                    asyncio.to_thread(route_sections, paths, summary_text, summary_title),
                    timeout=ANTHROPIC_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await update.message.reply_text(
                    f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
                    "Your Diet page is unchanged — nothing was written."
                )
                return
            if err:
                await update.message.reply_text(f"❌ Routing failed: {err}")
                return

            affected = [a for a in routing.get("affected", []) if a.get("path")]
            if not affected:
                await reply(
                    update,
                    f"ℹ️ *{escape_md(summary_title)}* doesn't map to anything on the "
                    f"Diet page — nothing was changed.",
                )
                await asyncio.to_thread(update_page, summary_id,
                                        {"Implemented": {"checkbox": True}})
                return

            # Resolve every routed path NOW, so one the model invented is reported
            # here rather than disappearing between the two calls.
            targets, unresolved = {}, []
            for item in affected:
                path = item["path"].strip()
                block_id = _resolve_path(path, block_map)
                if block_id:
                    targets[path] = block_id
                else:
                    unresolved.append(path)

            await reply(update, _format_plan(affected, unresolved, len(paths), summary_title))

            if not targets:
                await update.message.reply_text(
                    "⚠️ Claude named sections I couldn't find on the page — nothing was changed."
                )
                return

            # ── Step E: read the content of ONLY those sections ────────────────────────
            contents, err = await asyncio.to_thread(read_section_contents, targets)
            if err:
                await update.message.reply_text(
                    f"❌ Could not read the sections to update: {err}\n"
                    "Nothing was written."
                )
                return

            # ── Step F: MERGE — only the routed sections ───────────────────────────────
            await update.message.reply_text("🧠 Claude is merging the summary in…")
            try:
                result, err = await asyncio.wait_for(
                    asyncio.to_thread(merge_sections, contents, summary_text, summary_title),
                    timeout=ANTHROPIC_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await update.message.reply_text(
                    f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
                    "Your Diet page is unchanged — nothing was written."
                )
                return
            if err:
                await update.message.reply_text(f"❌ Merge failed: {err}")
                return

            updates = result.get("updates", [])

            # ── Step H: Mark the source Learn page as implemented (best-effort) ────────
            # The act of running Implement marks it processed, so the Learn-nudge job
            # stops surfacing it regardless of how many Diet sections matched.
            await asyncio.to_thread(update_page, summary_id, {"Implemented": {"checkbox": True}})

            # ── Step G: apply surgically ───────────────────────────────────────────────
            await update.message.reply_text("📝 Applying updates to Notion…")
            applied, skipped = await asyncio.to_thread(apply_updates, updates, block_map)

            # THE ACCOUNTING. Every path routing named ends in exactly one bucket,
            # and each bucket is printed. A routed section that merged into
            # nothing, failed to resolve, or failed to write must not read the
            # same as a section the summary never mentioned.
            returned  = {_path_key(u.get("path", "")) for u in updates}
            unchanged = [p for p in targets if _path_key(p) not in returned]

            lines = [f"✅ Diet page updated — *{applied}* of {len(targets)} "
                     f"routed section(s) modified."]
            # skipped entries are "path" or "path (notion error)" — Claude's text
            # either way, so both halves need escaping.
            lines += _report_lines("Skipped — unchanged", skipped, "⚠️")
            lines += _report_lines("Routed but left unchanged by the merge", unchanged, "➖")
            lines += _report_lines("Not found on the page", unresolved, "❓")
            for conflict in result.get("conflicts", [])[:_REPORT_LIMIT]:
                lines.append(f"\n⚖️ {escape_md(conflict)}")
            await reply(update, "\n".join(lines))
    except PageBusy:
        await update.message.reply_text(
            "⏳ An update to the Diet Manual is already in progress.\n"
            "Wait for it to finish, then try again."
        )
        return


# How many paths a report lists before it summarises the rest. The number is
# less important than the fact that the TOTAL is always printed: the old report
# sliced to eight with no count, so on a summary touching a dozen sections the
# rest were invisible, and a section dropped from the write looked exactly like a
# section that was never routed in the first place.
_REPORT_LIMIT = 8


def _report_lines(label: str, paths: list, emoji: str) -> list:
    """A bounded list of paths that still says how many there were."""
    if not paths:
        return []
    lines = ["", f"{emoji} {label} ({len(paths)}):"]
    lines += [f"• {escape_md(p)}" for p in paths[:_REPORT_LIMIT]]
    if len(paths) > _REPORT_LIMIT:
        lines.append(f"…and {len(paths) - _REPORT_LIMIT} more")
    return lines


def _format_plan(affected: list, unresolved: list, total: int, title: str) -> str:
    """What is about to change, sent before anything is written.

    Built from the ROUTING call, so it names the sections that will be read and
    merged — and it is sent before the merge call, so you see the plan before the
    expensive half runs rather than after it.

    Every interpolated value is Claude's: the paths it chose and the free-form
    `why` it wrote for each. Escaped here rather than at the send site so the
    function stays safe wherever it is sent from.
    """
    lines = [f"📋 *Plan* — {len(affected)} of {total} sections — _{escape_md(title)}_", ""]
    for item in affected:
        why = item.get("why", "")
        lines.append(f"♻️ {escape_md(item['path'])}"
                     + (f" — _{escape_md(why)}_" if why else ""))
    lines += _report_lines("Named but not found on the page — will be skipped",
                           unresolved, "⚠️")
    return "\n".join(lines).strip()
