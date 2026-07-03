"""
Notion ID diagnostic + lookup helpers for David.

WHY THIS EXISTS
---------------
When `Add e …` or `B` fail with "Could not connect to Notion", the cause is
almost always one of:
  • EXPENSES_ID or MONTH_ID is wrong / malformed in Railway
  • the database isn't shared with your Notion integration
  • a column was renamed (e.g. "Account" → "Conto"), so the API rejects the write

None of these are visible from the generic error message. This module asks
Notion directly and reports the real IDs + schema back to Telegram, so you can
copy the correct values straight into Railway's environment variables.

COMMANDS (wired in david.py)
----------------------------
  Diag           full diagnostic: EXPENSES_ID, its schema, and MONTH_ID
  Find [query]   search every page/database the integration can see → return IDs
  DBs            list every database the integration can access → name + ID

Everything here is READ-ONLY. Nothing writes to or deletes from Notion.

It also runs standalone (prints to logs) if you ever prefer that route:
  temporarily set the Railway start command to `python notion_ids.py`.
"""

import os
import re
import asyncio

from notion_client import (
    NOTION_BASE, notion_request, extract_rich_text, get_page_title,
)

EXPENSES_ID = os.environ.get("EXPENSES_ID")
MONTH_ID    = os.environ.get("MONTH_ID")
NOTION_KEY  = os.environ.get("NOTION_KEY")

# The columns the expense + budget code depends on, and the type each MUST be.
EXPECTED_EXPENSE_PROPS = {
    "Name":     "title",
    "Amount":   "number",
    "Date":     "date",
    "Category": "multi_select",
    "Account":  "relation",
}


# ─── SMALL HELPERS ─────────────────────────────────────────────────────────────

def _short(obj_id: str) -> str:
    """Notion IDs come with or without dashes; normalise to the 32-char URL form."""
    return (obj_id or "").replace("-", "")


def _esc(text: str) -> str:
    """Escape characters that would break Telegram legacy Markdown."""
    return re.sub(r"([_*`\[])", r"\\\1", text or "")


def db_title(db: dict) -> str:
    return extract_rich_text(db.get("title", [])) or "(untitled database)"


# ─── READ-ONLY NOTION CALLS ────────────────────────────────────────────────────

def search_all(query: str = "", only: str | None = None):
    """Search every object the integration can access.

    query : optional text filter ("" = everything)
    only  : "database" or "page" to restrict, or None for both
    Returns (results, error) — results is the raw list of Notion objects.
    """
    results, cursor = [], None
    base = {"page_size": 100}
    if query:
        base["query"] = query
    if only:
        base["filter"] = {"value": only, "property": "object"}

    try:
        while True:
            body = dict(base)
            if cursor:
                body["start_cursor"] = cursor
            resp = notion_request("POST", f"{NOTION_BASE}/search", json=body)
            if resp.status_code != 200:
                return [], f"Notion {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return results, None
    except Exception as e:
        return [], str(e)


def get_database(db_id: str):
    """GET a database object by ID. Returns (db_object, error)."""
    if not db_id:
        return None, "No database ID provided."
    try:
        resp = notion_request("GET", f"{NOTION_BASE}/databases/{db_id}")
        if resp.status_code != 200:
            return None, f"Notion {resp.status_code}: {resp.text[:200]}"
        return resp.json(), None
    except Exception as e:
        return None, str(e)


def list_db_pages(db_id: str, limit: int = 40):
    """List pages (rows) in a database. Returns (pages, error)."""
    try:
        resp = notion_request(
            "POST",
            f"{NOTION_BASE}/databases/{db_id}/query",
            json={"page_size": min(max(limit, 1), 100)},
        )
        if resp.status_code != 200:
            return [], f"Notion {resp.status_code}: {resp.text[:200]}"
        return resp.json().get("results", []), None
    except Exception as e:
        return [], str(e)


# ─── DIAGNOSTIC REPORT (pure — no Telegram, so it works from logs too) ─────────

def build_diagnostic_report() -> list:
    """Run all checks and return a list of Telegram-ready message blocks."""
    blocks = []

    if not NOTION_KEY:
        return ["❌ NOTION_KEY is not set in Railway. Nothing can work without it."]

    # 1) Databases the integration can access ───────────────────────────────────
    dbs, err = search_all(only="database")
    if err:
        return [
            f"❌ Couldn't reach Notion at all: {err}\n\n"
            "This usually means NOTION_KEY is wrong, expired, or revoked."
        ]

    db_block = ["🗂️ *Databases your integration can access*", ""]
    if dbs:
        for db in dbs:
            db_block.append(f"• {_esc(db_title(db))}")
            db_block.append(f"  `{_short(db.get('id', ''))}`")
    else:
        db_block.append(
            "⚠️ None visible. In Notion, open each database → ••• menu → "
            "Connections → add your integration."
        )
    blocks.append("\n".join(db_block))

    # 2) Inspect the configured EXPENSES_ID ─────────────────────────────────────
    if not EXPENSES_ID:
        blocks.append(
            "❌ *EXPENSES_ID* is not set in Railway.\n"
            "Set it to your Expenses database ID (pick it from the list above)."
        )
        return blocks

    db, err = get_database(EXPENSES_ID)
    if err:
        hint = ""
        if "404" in err:
            hint = (
                "\n\n→ Either EXPENSES_ID is wrong, or the Expenses database isn't "
                "shared with your integration.\nFix: open the Expenses DB in Notion → "
                "••• → Connections → add your integration, then copy its ID from the "
                "list above."
            )
        elif "400" in err:
            hint = "\n\n→ EXPENSES_ID looks malformed. Copy the 32-character ID exactly."
        elif "401" in err:
            hint = "\n\n→ NOTION_KEY is invalid or expired."
        blocks.append(
            f"❌ Configured *EXPENSES_ID* (`{_short(EXPENSES_ID)}`) failed:\n{err}{hint}"
        )
        return blocks

    # EXPENSES_ID resolves — verify the schema the code relies on
    props = db.get("properties", {})
    schema = [
        f"✅ *EXPENSES_ID* → “{_esc(db_title(db))}”",
        f"`{_short(EXPENSES_ID)}`",
        "",
        "*Required columns* (expense + budget code):",
    ]
    account_rel_db = None
    for name, want in EXPECTED_EXPENSE_PROPS.items():
        prop = props.get(name)
        if not prop:
            schema.append(f"❌ {name} — MISSING (needs type: {want})")
            continue
        got = prop.get("type", "?")
        ok = (got == want)
        schema.append(
            f"{'✅' if ok else '⚠️'} {name} — {got}"
            + ("" if ok else f"  (code expects {want})")
        )
        if name == "Account" and got == "relation":
            account_rel_db = prop.get("relation", {}).get("database_id")

    extras = [n for n in props if n not in EXPECTED_EXPENSE_PROPS]
    if extras:
        schema.append("")
        schema.append("Other columns present: " + ", ".join(_esc(n) for n in extras))
        schema.append(
            "_(If a required column shows MISSING but a similar name appears here, it "
            "was renamed — rename it back in Notion, or tell me and I'll adapt the code.)_"
        )
    blocks.append("\n".join(schema))

    # 3) MONTH_ID — list the pages the Account relation points to ────────────────
    if not account_rel_db:
        blocks.append(
            "⚠️ No working *Account* relation found, so I can't auto-list month pages.\n"
            "Once Account is a relation column, re-run `Diag`."
        )
        return blocks

    pages, err = list_db_pages(account_rel_db, limit=40)
    if err:
        blocks.append(f"⚠️ Couldn't list the month/account pages: {err}")
        return blocks

    month = [
        "🗓️ *MONTH_ID must point to one of these pages*",
        f"_(the Account relation targets database_ `{_short(account_rel_db)}`_)_",
        "",
    ]
    matched = False
    for pg in pages:
        pid = _short(pg.get("id", ""))
        title = _esc(get_page_title(pg))
        here = bool(MONTH_ID and _short(MONTH_ID) == pid)
        matched = matched or here
        month.append(f"{'👉' if here else '•'} {title}")
        month.append(f"  `{pid}`")

    month.append("")
    if not MONTH_ID:
        month.append("❌ *MONTH_ID* is not set. Copy the CURRENT month's ID above into Railway.")
    elif matched:
        month.append("✅ Your MONTH_ID matches the page marked 👉.")
        month.append(
            "If that isn't the *current* month, update it — this is a manual monthly "
            "step for now (I can automate it if you want)."
        )
    else:
        month.append(
            f"⚠️ Your MONTH_ID (`{_short(MONTH_ID)}`) matches none of these pages — "
            "that alone breaks expenses. Set it to the current month's ID above."
        )
    blocks.append("\n".join(month))

    blocks.append(
        "✅ *Diagnostic complete.*\n"
        "Update any wrong variable in Railway, wait for the redeploy to finish, "
        "then retry `Add e Test 1` and `B`."
    )
    return blocks


# ─── TELEGRAM HANDLERS ─────────────────────────────────────────────────────────

async def _send_long(update, text: str, parse_mode: str = "Markdown"):
    """Send text, splitting on newlines to stay under Telegram's 4096-char limit."""
    LIMIT = 3800
    if len(text) <= LIMIT:
        await update.message.reply_text(text, parse_mode=parse_mode)
        return
    chunk = ""
    for line in text.split("\n"):
        if len(chunk) + len(line) + 1 > LIMIT and chunk:
            await update.message.reply_text(chunk.rstrip("\n"), parse_mode=parse_mode)
            chunk = ""
        chunk += line + "\n"
    if chunk.strip():
        await update.message.reply_text(chunk.rstrip("\n"), parse_mode=parse_mode)


async def handle_diag(update):
    """`Diag` — full read-only diagnostic of the expense/budget Notion wiring."""
    await update.message.reply_text("🩺 Running Notion diagnostic — a few API calls, one moment…")
    try:
        blocks = await asyncio.to_thread(build_diagnostic_report)
    except Exception as e:
        await update.message.reply_text(f"❌ Diagnostic crashed: {type(e).__name__}: {e}")
        return
    for block in blocks:
        await _send_long(update, block)


async def handle_find(update, query: str):
    """`Find [query]` — search pages + databases by name, return their IDs."""
    query = (query or "").strip()
    if not query:
        await update.message.reply_text(
            "Usage: `Find [name]`\ne.g. `Find July` or `Find Expenses`",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(f"🔍 Searching Notion for “{query}”…")
    results, err = await asyncio.to_thread(search_all, query)
    if err:
        await update.message.reply_text(f"❌ Search failed: {err}")
        return
    if not results:
        await update.message.reply_text(
            f"No page or database matching “{query}”.\n"
            "If you expected one, it may not be shared with your integration."
        )
        return

    lines = [f"🔎 *Matches for “{_esc(query)}”*", ""]
    for obj in results[:40]:
        oid = _short(obj.get("id", ""))
        if obj.get("object") == "database":
            lines.append(f"🗂️ {_esc(db_title(obj))}  _(database)_")
        else:
            lines.append(f"📄 {_esc(get_page_title(obj))}  _(page)_")
        lines.append(f"  `{oid}`")
    await _send_long(update, "\n".join(lines))


async def handle_dbs(update):
    """`DBs` — list every database the integration can access, with IDs."""
    await update.message.reply_text("🔍 Listing databases your integration can access…")
    dbs, err = await asyncio.to_thread(search_all, "", "database")
    if err:
        await update.message.reply_text(f"❌ Search failed: {err}")
        return
    if not dbs:
        await update.message.reply_text(
            "⚠️ Your integration can't see any databases.\n"
            "In Notion, open each database → ••• → Connections → add your integration."
        )
        return

    lines = ["🗂️ *Accessible databases*", ""]
    for db in dbs:
        lines.append(f"• {_esc(db_title(db))}")
        lines.append(f"  `{_short(db.get('id', ''))}`")
    await _send_long(update, "\n".join(lines))


# ─── STANDALONE FALLBACK (prints to Railway logs) ─────────────────────────────

if __name__ == "__main__":
    for _block in build_diagnostic_report():
        print(_block.replace("*", "").replace("`", "").replace("_", ""))
        print("─" * 50)
