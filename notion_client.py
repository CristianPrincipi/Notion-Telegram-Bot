"""
Shared Notion API client for David.

Consolidates the headers, request helpers, retry logic, and block/rich-text
utilities that were previously copy-pasted across learn.py, implement.py, and
implement_diet.py. Import from here instead of redefining.
"""

import os
import time
import requests

NOTION_KEY = os.environ.get("NOTION_KEY")

HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

NOTION_BASE = "https://api.notion.com/v1"


# ─── RETRY WRAPPER ─────────────────────────────────────────────────────────────

def notion_request(method: str, url: str, *, max_retries: int = 3, **kwargs):
    """Make a Notion API request with automatic retry + exponential backoff.

    Retries on network errors and on 429/5xx responses (transient). Does NOT
    retry on 4xx client errors (400/401/404) — those won't fix themselves.

    Returns the requests.Response (caller checks status_code), or raises the
    final exception if every attempt failed at the network level.
    """
    kwargs.setdefault("headers", HEADERS)
    kwargs.setdefault("timeout", 15)

    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = requests.request(method, url, **kwargs)
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

def append_children(block_id: str, blocks: list):
    """Append children to a block in batches of 100. Returns (created_blocks, error)."""
    created = []
    try:
        remaining = blocks
        while remaining:
            batch, remaining = remaining[:100], remaining[100:]
            resp = notion_request(
                "PATCH",
                f"{NOTION_BASE}/blocks/{block_id}/children",
                json={"children": batch},
                timeout=20,
            )
            if resp.status_code != 200:
                return created, f"Notion {resp.status_code}: {resp.text[:200]}"
            created.extend(resp.json().get("results", []))
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
