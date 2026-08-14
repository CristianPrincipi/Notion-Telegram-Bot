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
# dozens of requests — read_diet_structure alone walks the H1>H2>H3 tree one
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


# The excerpt length every error string in this module already uses. Named so the
# log lines and the returned errors cannot drift apart.
BODY_EXCERPT_CHARS = 200


def body_excerpt(response) -> str:
    """A bounded, always-safe excerpt of a response body, for logs and errors.

    resp.text, never resp.json(). This is only ever called on a FAILURE path, and
    Notion answers a 502 with Cloudflare's HTML — .json() raises there, so the
    error handler would die on the error it was reporting and surface a
    JSONDecodeError instead of the actual status. That happened in four places in
    david.py.

    Bounded because an unbounded body in the Railway log is both unreadable and a
    way to leak far more page content than the failure warrants.
    """
    try:
        return (response.text or "")[:BODY_EXCERPT_CHARS]
    except Exception:
        return "<unreadable response body>"


# ─── QUERY / READ ──────────────────────────────────────────────────────────────

# The sort every "take the first match" lookup uses.
#
# Notion does not guarantee an order for query results, so a caller that reads
# results[0] out of an UNSORTED response is picking an arbitrary row and calling
# it "the first one". That is invisible while a search matches once and silently
# wrong the moment it matches twice — `D e Coffee` archived whichever Coffee the
# API felt like returning. Sorting newest-first gives "first" a definition that
# holds between two identical calls, and makes it the useful one: the row you
# most likely just created.
CREATED_DESC = [{"timestamp": "created_time", "direction": "descending"}]


# ─── TITLE PROPERTY DISCOVERY ──────────────────────────────────────────────────
# Every Notion database has exactly one property of type "title", but its NAME is
# whatever the person who made the database typed. search_page_in_db used to
# filter on a hard-coded "Name" — which Notion answers with a 400 when the column
# is called anything else, and the 400 was returned to the user as
# "No page found matching 'X'". A message pointing at your data for a bug in this
# line. get_page_title, twenty lines up, has always done this properly.
#
# Cached per database for the life of the process: the name of a column changes
# about never, and the alternative is an extra GET on every single lookup.
# Harmless to lose (Hard Rule 1) — a fresh container just asks Notion again, once
# per database.
#
# RLock, not asyncio.Lock: these functions run inside asyncio.to_thread workers,
# and an asyncio lock shared between two THREADS acquires without ever blocking,
# so it would read as protection and provide none. Same reasoning services/month.py
# documents for the same reason.
_title_props: dict[str, str] = {}
_title_props_lock = threading.RLock()


def title_property(db_id: str):
    """The name of `db_id`'s title column. Returns (property_name, error).

    THE CACHE IS POPULATED ONLY ON SUCCESS, and read before any network call. So
    a database whose schema was read once keeps working through a later Notion
    outage — the value cannot have gone stale in a way that matters — and only a
    database that has NEVER been read successfully fails. That asymmetry is the
    point: it buys the correctness of asking without making every lookup depend
    on Notion being up twice.
    """
    with _title_props_lock:
        cached = _title_props.get(db_id)
    if cached:
        return cached, None

    db, err = get_database(db_id)
    if err:
        return None, err

    for name, prop in (db or {}).get("properties", {}).items():
        if prop.get("type") == "title":
            with _title_props_lock:
                _title_props[db_id] = name
            return name, None

    # Every database has a title property, so reaching here means what came back
    # was not the database we asked about. Its own error, because "the schema
    # read fine and had no title column" needs a different fix from "the read
    # failed".
    return None, "no property of type 'title' — is that ID really a database?"


# The same question one level out: what TYPE is the column called `name`, and does
# it exist at all? Learn's duplicate check needs both — a filter names a property
# and its type ({"url": {...}} vs {"rich_text": {...}}), and Notion answers a 400
# for either mistake.
#
# Its own cache, alongside _title_props rather than merged into it, on the same
# terms: written only on success, read before any network call. Merging the two
# would save one GET per database per process and would rewrite the most
# carefully-reasoned function in this module to do it.
_db_schemas: dict[str, dict[str, str]] = {}
_db_schemas_lock = threading.RLock()


def database_property_type(db_id: str, property_name: str):
    """The type of `property_name` in `db_id`. Returns (type_or_None, error).

    THREE outcomes, and a caller has to tell them apart:

        ("url", None)   the property exists and this is its type
        (None,  None)   the schema read fine and there is no such property
        (None,  error)  the schema could not be read — this is NOT "no property"

    The middle and the last look identical to `if not prop_type`, which is the
    same collapse `(value, error)` exists to prevent everywhere else: "you have
    not added the column yet" and "Notion is down" want opposite handling.
    """
    with _db_schemas_lock:
        cached = _db_schemas.get(db_id)

    if cached is None:
        db, err = get_database(db_id)
        if err:
            return None, err
        cached = {name: prop.get("type", "")
                  for name, prop in (db or {}).get("properties", {}).items()}
        with _db_schemas_lock:
            _db_schemas[db_id] = cached

    return cached.get(property_name) or None, None


def search_page_in_db(db_id: str, query: str, exact: bool = False):
    """Search a Notion database for a page by title. Returns (page_object, error).

    Returns the most recently created match — see CREATED_DESC.
    """
    try:
        title_prop, prop_err = title_property(db_id)
        if prop_err:
            # REFUSED, not widened to "Name". Guessing here would restore exactly
            # the misleading "No page found" this removes, and would restore it
            # intermittently — which is materially harder to diagnose than a bug
            # that happens every time.
            return None, f"Could not read the schema of database {db_id}: {prop_err}"

        filter_type = "equals" if exact else "contains"
        resp = notion_request(
            "POST",
            f"{NOTION_BASE}/databases/{db_id}/query",
            json={"filter": {"property": title_prop, "title": {filter_type: query}},
                  "sorts": CREATED_DESC},
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
    (services/month.py) and to check the Expenses schema (services/notion_ids.py).
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

# Notion caps an append at 100 blocks, so anything longer is several requests and
# there is no transaction across them. Batch 3 of 5 failing leaves batches 1 and 2
# ON THE PAGE — and until now the caller was handed a flat error, so David said
# "could not save" while Notion held two fifths of the content. You then re-run,
# and the two fifths already there are appended a second time.
#
# A flat failure is not a smaller truth than a partial one, it is a different and
# wrong one. So the value side of (value, error) carries how far the write got.
#
# A LIST SUBCLASS, not a NamedTuple. Every existing caller already treats this
# value as "the blocks that were created" — indexes it, takes its length, reads
# created[0]["id"]. Changing the type would have broken each of those silently at
# a different call site; adding attributes to the list they already receive
# breaks none of them.

class Written(list):
    """The blocks a batched append actually created, and how far it got.

    A truthful `False` is still available to a caller that only wants one bit:
    an empty Written is falsy, exactly like the empty list it replaces.
    """

    def __init__(self, blocks=(), *, batches_done=0, batches_total=0, blocks_total=0):
        super().__init__(blocks)
        self.batches_done  = batches_done
        self.batches_total = batches_total
        self.blocks_total  = blocks_total

    @property
    def partial(self) -> bool:
        """True when SOME of the write landed and the rest did not.

        Deliberately not `bool(self) and self.batches_done < self.batches_total`:
        a batch that returned 200 with an empty `results` array still committed,
        so the batch tally is the authority on what is on the page, not the block
        count.
        """
        return 0 < self.batches_done < self.batches_total

    @property
    def summary(self) -> str:
        """'2 of 5 batches (200 of 430 blocks)' — for a message to a person."""
        return (f"{self.batches_done} of {self.batches_total} batches "
                f"({len(self)} of {self.blocks_total} blocks)")


def append_children(block_id: str, blocks: list, after: str | None = None):
    """Append children to a block in batches of 100. Returns (Written, error).

    `after` inserts the new blocks immediately after an existing sibling instead
    of at the end of the parent. That is what makes a section rewrite possible on
    a flat page: the replacement lands directly under its heading, so the page
    keeps its order once the stale blocks are deleted.

    Each batch after the first is anchored to the LAST block the previous batch
    created — anchoring every batch to the same `after` would insert them in
    reverse.

    Stops at the first failed batch, and the Written it returns says how many
    went in before that. Everything counted there is already committed and there
    is no way to take it back: Notion has no transactions, and deleting what
    landed would be a second write that can fail in the same way.
    """
    blocks = list(blocks or [])
    created = Written(batches_total=(len(blocks) + 99) // 100,
                      blocks_total=len(blocks))
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
            created.batches_done += 1
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
    """Create a Notion page. Returns (page_id, error). Appends >100 children in batches.

    THREE outcomes, not two, and the middle one is the one that used to lie:

        (page_id, None)   the page and all of its content exist
        (page_id, error)  THE PAGE EXISTS AND IS INCOMPLETE — the create call
                          succeeded, a later batch of content did not
        (None,    error)  nothing was created

    A caller that only checks `if not page_id` reports the middle case as a clean
    success, which is how a Learn page holding its first 100 blocks and nothing
    else came back as "✅ Saved to Notion!". The error string now opens with what
    landed, so a caller that DOES check it can say so without re-deriving it.
    """
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
            written, err = append_children(page_id, children[100:])
            if err:
                # The first 100 went in with the page itself, so they count.
                on_page = 100 + len(written)
                return page_id, (f"page created with {on_page} of {len(children)} "
                                 f"blocks — the rest failed: {err}")
        return page_id, None
    except Exception as e:
        return None, str(e)


def set_archived(page_id: str, archived: bool):
    """Archive or restore a page. Returns (ok, error).

    Notion has no hard delete for an integration — `D e` archives, which is what
    makes it reversible. Restoring is the same call with `False`, and it is a
    separate function from update_page because `archived` is a sibling of
    `properties` in the PATCH body, not one of them.
    """
    try:
        resp = notion_request(
            "PATCH",
            f"{NOTION_BASE}/pages/{page_id}",
            json={"archived": archived},
        )
        if resp.status_code != 200:
            return False, f"Notion {resp.status_code}: {body_excerpt(resp)}"
        return True, None
    except Exception as e:
        return False, str(e)


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


# ─── FLATTENING BLOCKS BACK TO TEXT ────────────────────────────────────────────
# One function, because there used to be three: this one, plus
# implement_diet._content_to_text and _content_to_text_deep. They differed in
# whether a heading kept its `##`, whether a divider became `---`, and whether a
# block type nobody had thought of was dropped or kept — three answers to the
# same question, each fixed in one place and none of them documented as a choice.
#
# The one that mattered was the last. Dropping unknown types made a block INVISIBLE
# to Claude while leaving it very much visible to the write-back: a `to_do` in a
# Manual section is in Section.content_ids, so apply_section_updates deletes it,
# but it was never in Section.text, so the merged content that replaced it was
# computed as though it had never existed. Anything with text now flattens, prefix
# or no prefix.

_MARKDOWN_PREFIXES = {
    "heading_1":          "# {}",
    "heading_2":          "## {}",
    "heading_3":          "### {}",
    "callout":            "> 💡 {}",
    "quote":              '> "{}"',
    "bulleted_list_item": "• {}",
    "numbered_list_item": "- {}",
}


def blocks_to_text(blocks: list, style: str = "markdown") -> str:
    """Flatten Notion blocks into readable text for Claude.

    style="markdown" keeps the structure: headings marked with #, list items
    bulleted, dividers as ---. Use it whenever the shape of a page is part of
    what the model needs to read.

    style="plain" is the text alone, no prefixes and no dividers — for the leaf
    content of one already-addressed section, where the surrounding structure is
    the caller's (implement_diet reads a section it located by path, so repeating
    the heading inside the value would just be noise in the tree it builds).
    """
    markdown = style == "markdown"
    lines = []
    for block in blocks:
        btype = block.get("type", "")
        text = extract_rich_text(block.get(btype, {}).get("rich_text", []))
        if btype == "divider":
            if markdown:
                lines.append("---")
            continue
        if not text:
            continue
        lines.append(_MARKDOWN_PREFIXES.get(btype, "{}").format(text) if markdown else text)
    return "\n".join(lines)
