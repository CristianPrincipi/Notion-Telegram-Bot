import os
import re
import json
import requests

from notion_client import (
    NOTION_BASE, notion_request, search_page_in_db, get_children,
    append_children, delete_block, create_page, extract_rich_text, rich,
    bullet as _bullet, paragraph as _paragraph,
)

# ─── ENV ───────────────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY")
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

def read_diet_tree(page_id: str):
    """Read the full H1>H2>H3 tree into a nested dict for Claude.

    Returns (tree, block_map, error) where:
      tree      = {h1: {h2: {h3: "text content", ...}, ...}, ...}
      block_map = {"h1>h2>h3": block_id}  — used later to locate sections to update
    """
    tree, block_map = {}, {}

    h1_blocks, err = get_children(page_id)
    if err:
        return {}, {}, err

    for h1 in h1_blocks:
        if not h1.get("type", "").startswith("heading_1"):
            continue
        h1_name = extract_rich_text(h1["heading_1"]["rich_text"])
        if not h1_name:
            continue
        tree[h1_name] = {}
        block_map[h1_name] = h1["id"]

        if not h1.get("has_children"):
            continue
        h2_blocks, _ = get_children(h1["id"])

        for h2 in h2_blocks:
            if not h2.get("type", "").startswith("heading_2"):
                continue
            h2_name = extract_rich_text(h2["heading_2"]["rich_text"])
            if not h2_name:
                continue
            key2 = f"{h1_name}>{h2_name}"
            block_map[key2] = h2["id"]

            if not h2.get("has_children"):
                tree[h1_name][h2_name] = ""
                continue
            h3_blocks, _ = get_children(h2["id"])

            # Are there H3 toggles, or just content directly under H2?
            has_h3 = any(b.get("type", "").startswith("heading_3") for b in h3_blocks)
            if has_h3:
                tree[h1_name][h2_name] = {}
                for h3 in h3_blocks:
                    if not h3.get("type", "").startswith("heading_3"):
                        continue
                    h3_name = extract_rich_text(h3["heading_3"]["rich_text"])
                    if not h3_name:
                        continue
                    key3 = f"{h1_name}>{h2_name}>{h3_name}"
                    block_map[key3] = h3["id"]
                    content_blocks, _ = (get_children(h3["id"]) if h3.get("has_children") else ([], None))
                    tree[h1_name][h2_name][h3_name] = _content_to_text(content_blocks)
            else:
                tree[h1_name][h2_name] = _content_to_text(h3_blocks)

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

Return ONLY valid JSON — no markdown fences, no preamble:
{
  "plan": {
    "new_sections":      ["path > of > section newly populated"],
    "updated_sections":  ["path > of > section merged with existing"],
    "evidence_added":    ["path > of > Evidence section"],
    "conflicts":         ["short description of any contradiction found and how resolved"]
  },
  "updates": [
    {
      "path": "H1 > H2 > H3",
      "mode": "merge" | "replace",
      "bullets": ["actionable line 1", "actionable line 2"]
    }
  ]
}

Rules:
- "path" MUST exactly match an existing section path from CURRENT_TREE (same names, same '>' format).
- Only output sections the SUMMARY genuinely informs. If the summary says nothing about a section, omit it.
- Store ACTIONABLE information, not raw notes. Each bullet is a concrete, standalone statement.
- mode "merge": combine your bullets with existing content, removing duplicates and keeping the stronger version.
- mode "replace": existing content is outdated/wrong and the summary supersedes it.
- For Evidence sections, structure bullets as "Question: …", "Result: …", "Limits: …", "Practical Conclusion: …" when the summary provides them.
- Do NOT invent content. Do NOT infer beyond the summary.
- If the summary affects nothing in the structure, return empty "updates": [].
- Return raw JSON only."""


def decide_updates(tree: dict, summary_text: str, summary_title: str):
    """Ask Claude which sections to update. Returns (result_dict, error)."""
    if not ANTHROPIC_KEY:
        return None, "ANTHROPIC_API_KEY not set."

    user_msg = (
        f"CURRENT_TREE:\n{json.dumps(tree, ensure_ascii=False, indent=1)[:30000]}\n\n"
        f"=== SUMMARY: {summary_title} ===\n{summary_text[:50000]}"
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
                "system":     _DIET_SYSTEM,
                "messages":   [{"role": "user", "content": user_msg}],
            },
            timeout=(10, 300),
        )
        resp.raise_for_status()
        raw = resp.json()["content"][0]["text"].strip()
        json_match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not json_match:
            return None, f"No JSON in Claude response: {raw[:300]}"
        return json.loads(json_match.group(0)), None
    except json.JSONDecodeError as e:
        return None, f"JSON parse error: {e}"
    except Exception as e:
        return None, str(e)


# ─── 4. APPLY UPDATES SURGICALLY ───────────────────────────────────────────────

def apply_updates(updates: list, block_map: dict):
    """For each update, locate the target section block and refresh its content.

    'replace' → delete existing leaf children, append new bullets.
    'merge'   → append new bullets (Claude already merged & deduped against existing).
    Sections not in `updates` are never touched.
    Returns (applied_count, skipped_paths).
    """
    applied, skipped = 0, []

    for upd in updates:
        path    = upd.get("path", "").strip()
        mode    = upd.get("mode", "merge")
        bullets = [b for b in upd.get("bullets", []) if b and b.strip()]
        if not path or not bullets:
            continue

        # Match the path to a known block id (tolerate spacing around '>')
        block_id = _resolve_path(path, block_map)
        if not block_id:
            skipped.append(path)
            continue

        # replace → clear current leaf children first
        if mode == "replace":
            existing, _ = get_children(block_id)
            for b in existing:
                # only delete leaf content, never nested toggle headings
                if not b.get("type", "").startswith("heading_"):
                    delete_block(b["id"])

        new_blocks = [_bullet(b) for b in bullets]
        _, err = append_children(block_id, new_blocks)
        if err:
            skipped.append(f"{path} ({err})")
        else:
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
    """

    summary_name = summary_name.strip()

    if not DIET_ID:
        await update.message.reply_text(
            "❌ `DIET_ID` is not set in your Railway environment variables.",
            parse_mode="Markdown",
        )
        return

    # ── Step A: find the summary in Learn DB ───────────────────────────────────
    await update.message.reply_text(
        f"🔍 Searching for *{summary_name}* in Learn database…", parse_mode="Markdown"
    )
    summary_page, err = search_page_in_db(LEARN_ID, summary_name)
    if err:
        await update.message.reply_text(
            f"❌ Could not find *{summary_name}* in your Learn database.\n"
            "Make sure you used `Learn` to save it and the title matches.",
            parse_mode="Markdown",
        )
        return

    summary_id = summary_page["id"]
    summary_title = extract_rich_text(
        next((p.get("title", []) for p in summary_page.get("properties", {}).values()
              if p.get("type") == "title"), [])
    )

    summary_blocks, err = get_children(summary_id)
    if err:
        await update.message.reply_text(f"❌ Could not read the summary: {err}")
        return
    summary_text = _content_to_text_deep(summary_blocks)
    if not summary_text.strip():
        await update.message.reply_text("❌ The summary page appears to be empty.")
        return

    # ── Step B: find or create the Diet page ───────────────────────────────────
    page_id, was_created, err = find_or_create_diet_page()
    if err:
        await update.message.reply_text(f"❌ Could not prepare the Diet page: {err}")
        return
    if was_created:
        await update.message.reply_text("🥗 First run — built the full Diet structure in Notion.")

    # ── Step C: read the current tree ──────────────────────────────────────────
    await update.message.reply_text("📂 Reading current Diet structure…")
    tree, block_map, err = read_diet_tree(page_id)
    if err:
        await update.message.reply_text(f"❌ Could not read the Diet tree: {err}")
        return

    # ── Step D: Claude decides what to update ──────────────────────────────────
    await update.message.reply_text("🧠 Claude is analysing the summary…")
    result, err = decide_updates(tree, summary_text, summary_title)
    if err:
        await update.message.reply_text(f"❌ Analysis failed: {err}")
        return

    plan    = result.get("plan", {})
    updates = result.get("updates", [])

    # ── Send the implementation plan BEFORE applying (per spec) ────────────────
    await update.message.reply_text(_format_plan(plan, summary_title), parse_mode="Markdown")

    if not updates:
        await update.message.reply_text(
            "ℹ️ The summary didn't map to any Diet section — nothing was changed."
        )
        return

    # ── Step E: apply surgically ───────────────────────────────────────────────
    await update.message.reply_text("📝 Applying updates to Notion…")
    applied, skipped = apply_updates(updates, block_map)

    msg = f"✅ Diet page updated — *{applied}* section(s) modified."
    if skipped:
        msg += "\n\n⚠️ Skipped (path not found):\n" + "\n".join(f"• {s}" for s in skipped[:8])
    await update.message.reply_text(msg, parse_mode="Markdown")


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
    """Render Claude's implementation plan as a Telegram message."""
    lines = [f"📋 *Implementation Plan* — _{title}_\n"]

    def section(label, items, emoji):
        if items:
            lines.append(f"{emoji} *{label}:*")
            lines.extend(f"  • {i}" for i in items[:10])
            lines.append("")

    section("New sections",     plan.get("new_sections", []),     "🆕")
    section("Updated sections", plan.get("updated_sections", []), "♻️")
    section("Evidence added",   plan.get("evidence_added", []),   "🔬")
    section("Conflicts",        plan.get("conflicts", []),        "⚠️")

    if len(lines) == 1:
        lines.append("_No structural changes detected._")
    return "\n".join(lines).strip()
