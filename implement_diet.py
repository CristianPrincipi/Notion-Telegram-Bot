import asyncio
import os
import json
from concurrent.futures import ThreadPoolExecutor

from anthropic_client import complete_json
from config import ANTHROPIC_TIMEOUT
from page_lock import PageBusy, page_lock
from telegram_text import escape_md, reply
from notion_client import (
    search_page_in_db, get_children,
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
# Siblings at a level are independent, so read_diet_tree walks BREADTH-first and
# fetches each level concurrently instead.
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


def read_diet_tree(page_id: str):
    """Read the full H1>H2>H3 tree into a nested dict for Claude.

    BLOCKING — dozens of Notion requests, four at a time. handle_implement_diet
    runs it via asyncio.to_thread.

    Returns (tree, block_map, error) where:
      tree      = {h1: {h2: {h3: "text content", ...}, ...}, ...}
      block_map = {"h1>h2>h3": block_id}  — used later to locate sections to update

    A section with no content reads as "" rather than being left out: Claude has
    to see that it exists and is empty in order to fill it.
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

    h3s = []                                   # [(h1_name, h2_name, h3_name, h3_block), ...]
    for h1_name, h2_name, h2 in h2s:
        if not h2.get("has_children"):
            tree[h1_name][h2_name] = ""
            continue

        blocks = h3_children.get(h2["id"], [])
        # An H2 either holds H3 toggles, or leaf content directly.
        if not any(b.get("type", "").startswith("heading_3") for b in blocks):
            tree[h1_name][h2_name] = _content_to_text(blocks)
            continue

        tree[h1_name][h2_name] = {}
        for h3 in blocks:
            h3_name = _heading_name(h3, 3)
            if not h3_name:
                continue
            block_map[f"{h1_name}>{h2_name}>{h3_name}"] = h3["id"]
            h3s.append((h1_name, h2_name, h3_name, h3))

    # ── Level 4: the leaf content under every H3, in one pass ──────────────────
    leaf_children, err = _children_of_many(
        [h3["id"] for *_, h3 in h3s if h3.get("has_children")])
    if err:
        return {}, {}, err

    for h1_name, h2_name, h3_name, h3 in h3s:
        tree[h1_name][h2_name][h3_name] = _content_to_text(
            leaf_children.get(h3["id"], []))

    return tree, block_map, None


def _content_to_text(blocks: list) -> str:
    """Flatten a section's leaf content (bullets/paragraphs) into text."""
    out = []
    for b in blocks:
        btype = b.get("type", "")
        rt = b.get(btype, {}).get("rich_text", [])
        txt = extract_rich_text(rt)
        if txt:
            out.append(txt)
    return "\n".join(out)


# ─── 3. CLAUDE: DECIDE WHICH SECTIONS TO UPDATE ────────────────────────────────

_DIET_SYSTEM = """You maintain a structured personal DIET knowledge page in Notion.

The page has a fixed hierarchy: H1 categories > H2 rows > H3 attributes.
You receive:
- CURRENT_TREE: the existing page as nested JSON (section path → current text content)
- SUMMARY: newly learned content (article/video/book) to integrate

Your job: decide which H3 attribute sections (or H2 leaf sections) the SUMMARY actually
affects, and return their FULL merged content. Touch ONLY sections the summary informs.

Rules:
- "path" MUST exactly match an existing section path from CURRENT_TREE (same names, same '>' format).
- Only output sections the SUMMARY genuinely informs. If the summary says nothing about a section, omit it.
- Store ACTIONABLE information, not raw notes. Each bullet is a concrete, standalone statement.
- mode "merge": combine your bullets with existing content, removing duplicates and keeping the stronger version.
- mode "replace": existing content is outdated/wrong and the summary supersedes it.
- For Evidence sections, structure bullets as "Question: …", "Result: …", "Limits: …", "Practical Conclusion: …" when the summary provides them.
- Do NOT invent content. Do NOT infer beyond the summary.
- If the summary affects nothing in the structure, return an empty "updates" list."""

_DIET_SCHEMA = {
    "type": "object",
    "properties": {
        "plan": {
            "type": "object",
            "description": "What you are about to change, for the user to read before it happens.",
            "properties": {
                "new_sections":     {"type": "array", "items": {"type": "string"}},
                "updated_sections": {"type": "array", "items": {"type": "string"}},
                "evidence_added":   {"type": "array", "items": {"type": "string"}},
                "conflicts":        {"type": "array", "items": {"type": "string"},
                                     "description": "Contradictions found, and how resolved."},
            },
        },
        "updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path":    {"type": "string",
                                "description": "Exactly matching a section path from CURRENT_TREE."},
                    "mode":    {"type": "string", "enum": ["merge", "replace"]},
                    "bullets": {"type": "array", "items": {"type": "string"},
                                "description": "The FULL merged content for this section."},
                },
                "required": ["path", "mode", "bullets"],
            },
        },
    },
    "required": ["updates"],
}


def decide_updates(tree: dict, summary_text: str, summary_title: str):
    """Ask Claude which sections to update. Returns (result_dict, error)."""
    user_msg = (
        f"CURRENT_TREE:\n{json.dumps(tree, ensure_ascii=False, indent=1)[:30000]}\n\n"
        f"=== SUMMARY: {summary_title} ===\n{summary_text[:50000]}"
    )
    return complete_json(_DIET_SYSTEM, user_msg, _DIET_SCHEMA)


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
    therefore duplicated the section, and because read_diet_tree feeds the tree
    back into the next prompt, the bloat compounded run over run and degraded
    each subsequent merge. Nothing errored — the page just grew.

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


def _resolve_path(path: str, block_map: dict):
    """Find a block id for a path, tolerant to spacing differences around '>'."""
    norm = ">".join(p.strip() for p in path.split(">"))
    if norm in block_map:
        return block_map[norm]
    # Case-insensitive fallback
    low = norm.lower()
    for k, v in block_map.items():
        if k.lower() == low:
            return v
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
      C) Read the current tree
      D) Claude decides which sections to update
      E) Apply surgical updates; report the plan
      F) Tick the source page's 'Implemented' checkbox (feeds the Learn-nudge job)
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
    summary_text = _content_to_text_deep(summary_blocks)
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
    # It is also taken before read_diet_tree: Claude decides which sections to
    # update from the tree it was given, so that decision is only valid while
    # nobody else is mutating it.
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

            # ── Step C: read the current tree ──────────────────────────────────────────
            await update.message.reply_text("📂 Reading current Diet structure…")
            tree, block_map, err = await asyncio.to_thread(read_diet_tree, page_id)
            if err:
                await update.message.reply_text(f"❌ Could not read the Diet tree: {err}")
                return

            # ── Step D: Claude decides what to update ──────────────────────────────────
            await update.message.reply_text("🧠 Claude is analysing the summary…")
            # Safe to time out: nothing has been written yet, and the source page
            # is not yet marked implemented.
            try:
                result, err = await asyncio.wait_for(
                    asyncio.to_thread(decide_updates, tree, summary_text, summary_title),
                    timeout=ANTHROPIC_TIMEOUT,
                )
            except asyncio.TimeoutError:
                await update.message.reply_text(
                    f"❌ Claude took longer than {ANTHROPIC_TIMEOUT}s and I gave up.\n"
                    "Your Diet page is unchanged — nothing was written."
                )
                return
            if err:
                await update.message.reply_text(f"❌ Analysis failed: {err}")
                return

            plan    = result.get("plan", {})
            updates = result.get("updates", [])

            # ── Send the implementation plan BEFORE applying (per spec) ────────────────
            await reply(update, _format_plan(plan, summary_title))

            # ── Step F: Mark the source Learn page as implemented (best-effort) ─────────
            # The act of running Implement marks it processed, so the Learn-nudge job
            # stops surfacing it regardless of how many Diet sections matched.
            await asyncio.to_thread(update_page, summary_id, {"Implemented": {"checkbox": True}})

            if not updates:
                await update.message.reply_text(
                    "ℹ️ The summary didn't map to any Diet section — nothing was changed."
                )
                return

            # ── Step E: apply surgically ───────────────────────────────────────────────
            await update.message.reply_text("📝 Applying updates to Notion…")
            applied, skipped = await asyncio.to_thread(apply_updates, updates, block_map)

            msg = f"✅ Diet page updated — *{applied}* section(s) modified."
            if skipped:
                # skipped entries are the section paths Claude named — its text.
                msg += ("\n\n⚠️ Skipped (path not found):\n"
                        + "\n".join(f"• {escape_md(s)}" for s in skipped[:8]))
            await reply(update, msg)
    except PageBusy:
        await update.message.reply_text(
            "⏳ An update to the Diet Manual is already in progress.\n"
            "Wait for it to finish, then try again."
        )
        return


def _content_to_text_deep(blocks: list) -> str:
    """Flatten Learn-summary blocks (headings, callouts, quotes, bullets) into text."""
    out = []
    for b in blocks:
        btype = b.get("type", "")
        rt = b.get(btype, {}).get("rich_text", [])
        txt = extract_rich_text(rt)
        if not txt:
            continue
        if btype == "heading_1":   out.append(f"# {txt}")
        elif btype == "heading_2": out.append(f"## {txt}")
        elif btype == "heading_3": out.append(f"### {txt}")
        elif btype == "callout":   out.append(f"💡 {txt}")
        elif btype == "quote":     out.append(f'"{txt}"')
        elif btype == "bulleted_list_item": out.append(f"• {txt}")
        elif btype == "numbered_list_item": out.append(f"- {txt}")
        else: out.append(txt)
    return "\n".join(out)


def _format_plan(plan: dict, title: str) -> str:
    """Render Claude's implementation plan as a Telegram message.

    `title` is a Notion page title and every item is Claude's own text — the
    `conflicts` list in particular is free-form prose. All escaped here so the
    function stays safe wherever it is sent from.
    """
    lines = [f"📋 *Implementation Plan* — _{escape_md(title)}_\n"]

    def section(label, items, emoji):
        if items:
            lines.append(f"{emoji} *{label}:*")
            lines.extend(f"  • {escape_md(i)}" for i in items[:10])
            lines.append("")

    section("New sections",     plan.get("new_sections", []),     "🆕")
    section("Updated sections", plan.get("updated_sections", []), "♻️")
    section("Evidence added",   plan.get("evidence_added", []),   "🔬")
    section("Conflicts",        plan.get("conflicts", []),        "⚠️")

    if len(lines) == 1:
        lines.append("_No structural changes detected._")
    return "\n".join(lines).strip()
