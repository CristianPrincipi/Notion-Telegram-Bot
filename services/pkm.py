"""
PKM retrieval for David — read-only, LLM-free lookups into Notion manuals.

Command:  Get [Argument] - [Area]
Examples: Get Perfect Process - Brain
          Get Active Recall - Brain
          Get Seasonality - Diet
          Get ? - Brain              (list every topic in the area)

Notion is the single source of truth. Every lookup builds a FRESH in-memory index
of the manual's headings (Section = H2, Subsection = H3), so edits made in Notion
show up immediately. The argument is resolved against the index (exact → contains
→ fuzzy) and ONLY that heading's own direct content is returned — never the whole
subtree. Works for both manual layouts:

  • flat plain-heading manuals (implement.py)
        section content = the sibling blocks following the heading, until the next
        heading of any level.
  • nested toggle-heading pages (implement_diet.py)
        section content = the toggle's own non-heading children.

No Claude / Anthropic API call is ever made here.

THE ANSWER GOES OUT ON THE PLAIN CHANNEL. A rendered section is arbitrary Notion
content — someone else's asterisks and underscores — so it cannot be trusted to
survive Markdown, and escaping it would put backslashes into the notes you are
reading back. David's own framing (the usage message, the area names) uses
notify_md; the section body does not. Splitting a long one to fit Telegram is the
adapter's job: see bot/long_messages.py.
"""

import asyncio
import re
from difflib import SequenceMatcher

from clients.notion_client import get_children, search_page_in_db, extract_rich_text
from services.implement import get_area_db_id
from telegram_text import escape_md


# ─── COMMAND PARSING ───────────────────────────────────────────────────────────
# Separator is a SPACE-hyphen-SPACE so intra-word hyphens (e.g. "Step-by-Step")
# are never mistaken for the Argument / Area divider.
GET_PATTERN = r"(?i)^get\s+(.+?)\s+-\s+(.+)$"

# Arguments that mean "list everything in this area" instead of a topic lookup.
_DISCOVERY_WORDS = {"?", "list", "topics", "index", "all"}

# Areas whose manual page is NOT titled "Manual". Extend here as you add manuals.
_AREA_PAGE_TITLE = {"diet": "Diet"}

# Score thresholds for the resolver.
_STRONG = 0.93   # confident match
_FLOOR  = 0.60   # below this we treat as "no match"


def _manual_title_for(area_name: str) -> str:
    return _AREA_PAGE_TITLE.get(area_name.strip().lower(), "Manual")


# ─── TEXT NORMALISATION & MATCHING ─────────────────────────────────────────────

def _normalize(s: str) -> str:
    """Lowercase, strip emoji/arrows/punctuation, collapse whitespace.
    '⚙️ Perfect Process' → 'perfect process'; '→ Active Recall Session' → 'active recall session'."""
    s = (s or "").lower()
    s = re.sub(r"[^\w\s]", " ", s, flags=re.UNICODE)   # drops emoji, →, hyphens, punctuation
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _score(q: str, t: str) -> float:
    """Similarity in 0..1. Exact = 1.0, substring ≈ 0.9+, otherwise fuzzy ratio."""
    if not t:
        return 0.0
    if q == t:
        return 1.0
    # Substring match (only for reasonably specific queries, to avoid 1-2 char noise)
    if len(q) >= 3 and len(t) >= 3 and (q in t or t in q):
        shorter, longer = sorted((len(q), len(t)))
        return 0.90 + 0.09 * (shorter / longer if longer else 0)
    return SequenceMatcher(None, q, t).ratio()


# ─── BLOCK HELPERS ─────────────────────────────────────────────────────────────

def _heading_level(block: dict):
    return {"heading_1": 1, "heading_2": 2, "heading_3": 3}.get(block.get("type", ""))


def _title_of(block: dict) -> str:
    t = block.get("type", "")
    return extract_rich_text(block.get(t, {}).get("rich_text", []))


def _clean_title(title: str) -> str:
    """Strip leading decorative emoji/arrows the manuals bake into headings
    ('⚙️ Perfect Process' → 'Perfect Process', '→ Active Recall' → 'Active Recall').
    Internal characters (e.g. hyphens in 'Step-by-Step') are preserved."""
    return re.sub(r"^[^\w]+", "", (title or "").strip()) or (title or "").strip()


# ─── INDEX BUILDER ─────────────────────────────────────────────────────────────
# Depth-first walk → ordered list of sections in document order:
#   {"level", "title", "norm", "content": [blocks]}
# "content" is the heading's OWN direct content only (sub-headings excluded).
#
# BLOCKING, and not by a little: Notion has no recursive block read, so a toggle
# manual costs one request per heading that has children — the Diet page is ~67.
# run_get runs it via asyncio.to_thread. Like read_diet_structure it takes no outer
# wait_for: every individual request is already bounded by notion_request's
# timeout and its bounded retries.

def build_index(page_id: str):
    top, err = get_children(page_id)
    if err:
        return None, err
    index: list = []
    _walk_blocks(top, index)
    return index, None


def _walk_blocks(blocks: list, index: list):
    i, n = 0, len(blocks)
    while i < n:
        b = blocks[i]
        lvl = _heading_level(b)
        if lvl:
            if b.get("has_children"):
                # Toggle heading (Diet): own content = non-heading children;
                # sub-headings are recursed into so they keep document order.
                kids, _ = get_children(b["id"])
                content = [k for k in kids if not _heading_level(k)]
                index.append(_record(b, lvl, content))
                _walk_blocks(kids, index)
            else:
                # Flat heading: own content = following siblings until the next heading.
                j = i + 1
                content = []
                while j < n and not _heading_level(blocks[j]):
                    content.append(blocks[j])
                    j += 1
                index.append(_record(b, lvl, content))
        elif b.get("has_children"):
            # Non-heading container — recurse to surface any nested headings.
            kids, _ = get_children(b["id"])
            _walk_blocks(kids, index)
        i += 1


def _record(block: dict, level: int, content: list) -> dict:
    title = _title_of(block)
    return {"level": level, "title": title, "norm": _normalize(title), "content": content}


def _subheadings(index: list, pos: int) -> list:
    """Immediate child headings (one level deeper) of the section at index[pos]."""
    parent_lvl = index[pos]["level"]
    out = []
    for entry in index[pos + 1:]:
        if entry["level"] <= parent_lvl:
            break
        if entry["level"] == parent_lvl + 1:
            out.append(entry["title"])
    return out


# ─── RENDERING (plain text — robust against Telegram Markdown parse errors) ─────

def _render(blocks: list, indent: int = 0, depth: int = 0) -> str:
    lines, num = [], 0
    pad = "   " * indent
    for b in blocks:
        t = b.get("type", "")
        data = b.get(t, {})
        text = extract_rich_text(data.get("rich_text", [])) if isinstance(data, dict) else ""

        if t == "divider":
            num = 0
            continue
        if t == "numbered_list_item":
            num += 1
            lines.append(f"{pad}{num}. {text}")
        else:
            num = 0
            if t == "bulleted_list_item":
                lines.append(f"{pad}• {text}")
            elif t == "to_do":
                lines.append(f"{pad}{'☑' if data.get('checked') else '☐'} {text}")
            elif t in ("heading_1", "heading_2", "heading_3"):
                lines.append(f"{pad}{text}")
            elif t == "callout":
                emoji = (data.get("icon") or {}).get("emoji", "💡")
                lines.append(f"{pad}{emoji} {text}")
            elif t == "quote":
                lines.append(f"{pad}“{text}”")
            elif text:
                lines.append(f"{pad}{text}")
            else:
                lines.append("")

        # Recurse into nested children (bounded), e.g. sub-bullets.
        if b.get("has_children") and depth < 2 and not _heading_level(b):
            kids, _ = get_children(b["id"])
            child = _render(kids, indent + 1, depth + 1)
            if child:
                lines.append(child)

    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _build_topic_tree(index: list, area: str) -> str:
    """Readable indented list of every section / subsection in the manual."""
    if not index:
        return f"The {area} manual has no sections yet."
    min_lvl = min(e["level"] for e in index)
    markers = {0: "• ", 1: "→ ", 2: "· "}
    lines = [f"📖 {area} manual — topics:", ""]
    for e in index:
        d = e["level"] - min_lvl
        lines.append("   " * d + markers.get(d, "· ") + _clean_title(e["title"]))
    lines += ["", f"Retrieve one with:  Get [topic] - {area}"]
    return "\n".join(lines)


# ─── THE COMMAND ───────────────────────────────────────────────────────────────

async def run_get(user_text: str, *, notify, notify_md=None) -> None:
    """`Get [Argument] - [Area]` — resolve a heading and read its content back."""
    notify_md = notify_md or notify

    m = re.match(GET_PATTERN, user_text.strip())
    if not m:
        await notify_md(
            "🔎 *Get usage:*\n"
            "`Get [Topic] - [Area]`\n\n"
            "Examples:\n"
            "`Get Perfect Process - Brain`\n"
            "`Get Active Recall - Brain`\n"
            "`Get ? - Brain`  _(list all topics)_",
        )
        return

    argument = m.group(1).strip()
    area     = m.group(2).strip()

    # Resolve the area → Notion database (reuses David's {AREA}_ID env convention)
    db_id = get_area_db_id(area)
    if not db_id:
        env_key = f"{area.upper().replace(' ', '_')}_ID"
        await notify_md(
            f"❌ Area *{escape_md(area)}* isn't configured.\n"
            f"Set `{env_key}` on Railway to that area's Notion database ID.",
        )
        return

    # Locate the manual page inside that database
    # Every Notion call below runs on a worker thread — see build_index. On the
    # event loop they would freeze every other command for the whole walk.
    page_title = _manual_title_for(area)
    page, perr = await asyncio.to_thread(search_page_in_db, db_id, page_title, True)
    if not page:
        await notify_md(
            f"❌ No *{escape_md(page_title)}* page found in the *{escape_md(area)}* database.",
        )
        # The Notion detail goes out as a SEPARATE plain message: it used to sit in
        # a `code span`, where Markdown v1 ignores backslash escapes, so escaping
        # cannot make it safe — only sending it unformatted can.
        if perr and "No page found" not in perr:
            await notify(perr)
        return

    await notify_md(f"🔎 Searching the *{escape_md(area)}* manual…")

    index, err = await asyncio.to_thread(build_index, page["id"])
    if err:
        await notify(f"❌ Could not read the manual: {err}")
        return
    if not index:
        await notify_md(f"ℹ️ The *{escape_md(area)}* manual has no sections yet.")
        return

    # Discovery mode
    if argument.lower() in _DISCOVERY_WORDS:
        await notify(_build_topic_tree(index, area))
        return

    # Resolve the argument against the index
    q = _normalize(argument)
    scored = sorted(
        ({"score": _score(q, e["norm"]), **e} for e in index),
        key=lambda e: e["score"], reverse=True,
    )
    strong   = [e for e in scored if e["score"] >= _STRONG]
    moderate = [e for e in scored if _FLOOR <= e["score"] < _STRONG]

    # Exactly one confident hit → return it
    if len(strong) == 1:
        await _deliver(strong[0], index, area, notify=notify)
        return

    # Multiple confident hits, or several moderate → disambiguation list
    candidates = strong or moderate
    if len(candidates) > 1:
        lines = [f"🤔 Multiple matches for *{escape_md(argument)}* in *{escape_md(area)}*:", ""]
        for e in candidates[:8]:
            kind = "Section" if e["level"] <= 2 else "Subsection"
            lines.append(f"• {escape_md(_clean_title(e['title']))}  ({kind})")
        lines += ["", "Repeat with the exact name, e.g.:",
                  f"`Get {_clean_title(candidates[0]['title'])} - {area}`"]
        await notify_md("\n".join(lines))
        return

    # A single moderate match → return it
    if len(moderate) == 1:
        await _deliver(moderate[0], index, area, notify=notify)
        return

    # Nothing close → not found + the full topic tree to help
    await notify_md(f"❌ No topic matching *{escape_md(argument)}* in the *{escape_md(area)}* manual.")
    await notify(_build_topic_tree(index, area))


async def _deliver(entry: dict, index: list, area: str, *, notify):
    """Render and send a resolved section, or suggest subsections if it has no own content."""
    pos = next((i for i, e in enumerate(index)
                if e["title"] == entry["title"] and e["level"] == entry["level"]), None)
    # _render recurses into nested children, so it can issue Notion reads of its
    # own — blocking, and off the loop like the rest.
    body = await asyncio.to_thread(_render, entry["content"])

    kind = "Section" if entry["level"] <= 2 else "Subsection"
    header = f"📖 {_clean_title(entry['title'])}  —  {area} ({kind})\n" + "─" * 24

    if body.strip():
        await notify(f"{header}\n{body}")
        return

    subs = _subheadings(index, pos) if pos is not None else []
    if subs:
        lines = [header, "", "This section has no direct text. It contains:", ""]
        lines += [f"   → {_clean_title(s)}" for s in subs]
        lines += ["", f"Open one with:  Get [name] - {area}"]
        await notify("\n".join(lines))
    else:
        await notify(f"{header}\n(empty)")
