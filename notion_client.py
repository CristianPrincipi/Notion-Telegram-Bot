"""
Shared Notion API client for David.

Consolidates the headers, request helpers, retry logic, and block/rich-text
utilities that were previously copy-pasted across learn.py, implement.py, and
implement_diet.py. Import from here instead of redefining.
"""

import os
import threading
import time
import requests

NOTION_KEY = os.environ.get("NOTION_KEY")

HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

NOTION_BASE = "https://api.notion.com/v1"


# ─── CONNECTION POOLING ────────────────────────────────────────────────────────
# requests.request() builds a throwaway Session per call, so every Notion request
# paid a fresh TCP connect plus a full TLS handshake. One Implement run makes
# dozens of requests — read_diet_tree alone walks the whole H1>H2>H3 tree one
# request at a time — so that handshake was a large share of the wall clock.
# A Session keeps the connection alive and reuses it.
#
# ONE SESSION PER THREAD, not one shared. These calls now run inside
# asyncio.to_thread workers, and requests.Session is not thread-safe: its
# connection pool can hand the same socket to two threads at once, which corrupts
# both responses. threading.local() gives each worker thread its own Session and
# its own pool, which is the supported way to use requests across threads.
#
# Sessions are never closed. asyncio.to_thread runs on a bounded default
# ThreadPoolExecutor (min(32, cpu_count + 4) workers), so the table is bounded by
# the pool size, and David is a long-lived process that wants those connections
# kept warm anyway.

_thread_local = threading.local()


def _session() -> requests.Session:
    """The calling thread's Session, created on first use.

    Headers live on the Session rather than being passed per call, so there is
    one place they are set. A caller can still override them with a `headers=`
    kwarg — requests merges request-level headers over the Session's.
    """
    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(HEADERS)
        _thread_local.session = session
    return session


# ─── RETRY WRAPPER ─────────────────────────────────────────────────────────────

def notion_request(method: str, url: str, *, max_retries: int = 3, **kwargs):
    """Make a Notion API request with automatic retry + exponential backoff.

    Retries on network errors and on 429/5xx responses (transient). Does NOT
    retry on 4xx client errors (400/401/404) — those won't fix themselves.

    Returns the requests.Response (caller checks status_code), or raises the
    final exception if every attempt failed at the network level.
    """
    kwargs.setdefault("timeout", 15)
    session = _session()

    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = session.request(method, url, **kwargs)
            # Retry only on transient server-side conditions
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(wait)
                continue
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
    # Should not reach here, but just in case
    if last_exc:
        raise last_exc


# ─── RICH TEXT HELPERS ─────────────────────────────────────────────────────────

def rich(text: str) -> list:
    """Build a Notion rich_text array from a plain string (truncated to 2000)."""
    return [{"type": "text", "text": {"content": (text or "")[:2000]}}]


def extract_rich_text(rich_text_list: list) -> str:
    """Flatten a Notion rich_text array back into a plain string."""
    return "".join(rt.get("plain_text", "") for rt in (rich_text_list or []))


def get_page_title(page: dict) -> str:
    """Extract the title from any Notion page object, whatever the title prop is named."""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return extract_rich_text(prop.get("title", []))
    return "Untitled"


# ─── QUERY / READ ──────────────────────────────────────────────────────────────

def search_page_in_db(db_id: str, query: str, exact: bool = False):
    """Search a Notion database for a page by title. Returns (page_object, error)."""
    try:
        filter_type = "equals" if exact else "contains"
        resp = notion_request(
            "POST",
            f"{NOTION_BASE}/databases/{db_id}/query",
            json={"filter": {"property": "Name", "title": {filter_type: query}}},
        )
        if resp.status_code != 200:
            return None, f"Notion {resp.status_code}: {resp.text[:200]}"
        results = resp.json().get("results", [])
        if not results:
            return None, f"No page found matching '{query}'"
        return results[0], None
    except Exception as e:
        return None, str(e)


def get_database(db_id: str):
    """GET a database object by ID — its title, and the schema of every column.

    Returns (db_object, error). Used to read a relation column's target database
    (month.py) and to check the Expenses schema (notion_ids.py).
    """
    if not db_id:
        return None, "No database ID provided."
    try:
        resp = notion_request("GET", f"{NOTION_BASE}/databases/{db_id}")
        if resp.status_code != 200:
            return None, f"Notion {resp.status_code}: {resp.text[:200]}"
        return resp.json(), None
    except Exception as e:
        return None, str(e)


def query_database(db_id: str, filter_obj: dict = None, sorts: list = None, page_size: int = 100):
    """Query a Notion database with an optional filter and sort, following pagination.

    Generic counterpart to search_page_in_db (which only filters by Name). Used by
    the Learn-nudge and Takeaway jobs. `filter_obj` and `sorts` are passed straight
    through to the Notion query API. Returns (pages, error).
    """
    pages, cursor = [], None
    try:
        while True:
            body = {"page_size": page_size}
            if filter_obj:
                body["filter"] = filter_obj
            if sorts:
                body["sorts"] = sorts
            if cursor:
                body["start_cursor"] = cursor
            resp = notion_request(
                "POST",
                f"{NOTION_BASE}/databases/{db_id}/query",
                json=body,
            )
            if resp.status_code != 200:
                return [], f"Notion {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return pages, None
    except Exception as e:
        return [], str(e)


def get_children(block_id: str):
    """Get direct children of a block/page (handles pagination). Returns (blocks, error)."""
    blocks, cursor = [], None
    try:
        while True:
            params = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = notion_request(
                "GET",
                f"{NOTION_BASE}/blocks/{block_id}/children",
                params=params,
            )
            if resp.status_code != 200:
                return [], f"Notion {resp.status_code}: {resp.text[:200]}"
            data = resp.json()
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return blocks, None
    except Exception as e:
        return [], str(e)


# ─── WRITE ─────────────────────────────────────────────────────────────────────

def append_children(block_id: str, blocks: list, after: str | None = None):
    """Append children to a block in batches of 100. Returns (created_blocks, error).

    `after` inserts the new blocks immediately after an existing sibling instead
    of at the end of the parent. That is what makes a section rewrite possible on
    a flat page: the replacement lands directly under its heading, so the page
    keeps its order once the stale blocks are deleted.

    Each batch after the first is anchored to the LAST block the previous batch
    created — anchoring every batch to the same `after` would insert them in
    reverse.
    """
    created = []
    try:
        remaining, anchor = blocks, after
        while remaining:
            batch, remaining = remaining[:100], remaining[100:]
            body = {"children": batch}
            if anchor:
                body["after"] = anchor
            resp = notion_request(
                "PATCH",
                f"{NOTION_BASE}/blocks/{block_id}/children",
                json=body,
                timeout=20,
            )
            if resp.status_code != 200:
                return created, f"Notion {resp.status_code}: {resp.text[:200]}"
            batch_created = resp.json().get("results", [])
            created.extend(batch_created)
            if anchor and batch_created:
                anchor = batch_created[-1].get("id") or anchor
        return created, None
    except Exception as e:
        return created, str(e)


def delete_block(block_id: str):
    """Archive (delete) a single block. Best-effort, no error raised."""
    try:
        notion_request("DELETE", f"{NOTION_BASE}/blocks/{block_id}", timeout=10)
    except Exception:
        pass


def create_page(parent_db_id: str, properties: dict, children: list = None, icon: str = None):
    """Create a Notion page. Returns (page_id, error). Appends >100 children in batches."""
    body = {
        "parent": {"database_id": parent_db_id},
        "properties": properties,
    }
    if icon:
        body["icon"] = {"emoji": icon}
    if children:
        body["children"] = children[:100]

    try:
        resp = notion_request("POST", f"{NOTION_BASE}/pages", json=body)
        if resp.status_code != 200:
            return None, f"Notion {resp.status_code}: {resp.text[:300]}"
        page_id = resp.json()["id"]
        if children and len(children) > 100:
            _, err = append_children(page_id, children[100:])
            if err:
                return page_id, err
        return page_id, None
    except Exception as e:
        return None, str(e)


def update_page(page_id: str, properties: dict):
    """Patch a page's properties (e.g. tick a checkbox). Returns (ok, error)."""
    try:
        resp = notion_request(
            "PATCH",
            f"{NOTION_BASE}/pages/{page_id}",
            json={"properties": properties},
        )
        if resp.status_code != 200:
            return False, f"Notion {resp.status_code}: {resp.text[:200]}"
        return True, None
    except Exception as e:
        return False, str(e)


# ─── BLOCK BUILDERS ────────────────────────────────────────────────────────────

def paragraph(text: str) -> dict:
    return {"object": "block", "type": "paragraph",
            "paragraph": {"rich_text": rich(text)}}


def heading2(text: str) -> dict:
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": rich(text)}}


def heading3(text: str) -> dict:
    return {"object": "block", "type": "heading_3",
            "heading_3": {"rich_text": rich(text)}}


def callout(text: str, emoji: str = "💡", color: str = "blue_background") -> dict:
    return {"object": "block", "type": "callout",
            "callout": {"rich_text": rich(text), "icon": {"emoji": emoji}, "color": color}}


def quote(text: str) -> dict:
    return {"object": "block", "type": "quote",
            "quote": {"rich_text": rich(text)}}


def bullet(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": rich(text)}}


def numbered(text: str) -> dict:
    return {"object": "block", "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": rich(text)}}


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def blocks_to_text(blocks: list) -> str:
    """Convert a list of Notion blocks to readable plain text for Claude."""
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        content = block.get(btype, {})
        text = extract_rich_text(content.get("rich_text", []))
        if btype == "paragraph"            and text: lines.append(text)
        elif btype == "heading_1"          and text: lines.append(f"# {text}")
        elif btype == "heading_2"          and text: lines.append(f"## {text}")
        elif btype == "heading_3"          and text: lines.append(f"### {text}")
        elif btype == "callout"            and text: lines.append(f"> 💡 {text}")
        elif btype == "quote"              and text: lines.append(f'> "{text}"')
        elif btype == "bulleted_list_item" and text: lines.append(f"• {text}")
        elif btype == "numbered_list_item" and text: lines.append(f"- {text}")
        elif btype == "divider":                     lines.append("---")
    return "\n".join(lines)
