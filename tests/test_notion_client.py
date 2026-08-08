"""notion_client tests — pagination, batching and retry, all mocked offline.

Pagination is the highest-value thing here: Notion caps a query at 100 rows and
signals more with has_more/next_cursor. A helper that forgets to follow the
cursor looks perfectly healthy in production right up to the month you record
your 101st expense, then silently drops everything past the first page.
"""

import pytest
import responses

from clients import notion_client
from conftest import NOTION_BASE
from clients.notion_client import (
    append_children,
    get_children,
    notion_request,
    query_database,
    search_page_in_db,
)

DB_ID    = "db-under-test"
BLOCK_ID = "block-under-test"

QUERY_URL    = f"{NOTION_BASE}/databases/{DB_ID}/query"
CHILDREN_URL = f"{NOTION_BASE}/blocks/{BLOCK_ID}/children"


@pytest.fixture(autouse=True)
def no_real_sleeping(monkeypatch):
    """Retry backoff sleeps 1s/2s/4s — skip it so the suite stays fast."""
    slept = []
    monkeypatch.setattr(notion_client.time, "sleep", slept.append)
    return slept


def page(index):
    return {"id": f"page-{index}", "properties": {}}


def request_bodies():
    """JSON body of every intercepted request, in order."""
    import json
    bodies = []
    for call in responses.calls:
        body = call.request.body
        if body is None:
            bodies.append(None)
        else:
            bodies.append(json.loads(body.decode() if isinstance(body, bytes) else body))
    return bodies


# ─── QUERY PAGINATION ──────────────────────────────────────────────────────────

@responses.activate
def test_query_database_follows_every_page():
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(i) for i in range(100)],
                        "has_more": True, "next_cursor": "cursor-1"})
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(i) for i in range(100, 200)],
                        "has_more": True, "next_cursor": "cursor-2"})
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(i) for i in range(200, 250)],
                        "has_more": False, "next_cursor": None})

    pages, err = query_database(DB_ID)

    assert err is None
    assert len(pages) == 250, "pagination stopped early — later pages were dropped"
    assert [p["id"] for p in pages] == [f"page-{i}" for i in range(250)]
    assert len(responses.calls) == 3


@responses.activate
def test_query_database_sends_the_cursor_it_was_given():
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(0)], "has_more": True, "next_cursor": "cursor-1"})
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(1)], "has_more": False, "next_cursor": None})

    query_database(DB_ID)

    first, second = request_bodies()
    assert "start_cursor" not in first, "the first request must not send a cursor"
    assert second["start_cursor"] == "cursor-1"


@responses.activate
def test_query_database_stops_when_has_more_is_false_even_with_a_cursor():
    """Notion can return a stale next_cursor alongside has_more=false."""
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(0)], "has_more": False, "next_cursor": "cursor-1"})

    pages, err = query_database(DB_ID)

    assert err is None
    assert len(pages) == 1
    assert len(responses.calls) == 1, "followed a cursor despite has_more=false — infinite loop risk"


@responses.activate
def test_query_database_passes_filter_sorts_and_page_size():
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [], "has_more": False})
    filter_obj = {"property": "Done", "checkbox": {"equals": False}}
    sorts = [{"property": "Date", "direction": "descending"}]

    query_database(DB_ID, filter_obj=filter_obj, sorts=sorts, page_size=25)

    body = request_bodies()[0]
    assert body["filter"] == filter_obj
    assert body["sorts"] == sorts
    assert body["page_size"] == 25


@responses.activate
def test_query_database_reports_an_error_mid_pagination():
    """A failure on page 2 must not be reported as a successful short result."""
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(0)], "has_more": True, "next_cursor": "cursor-1"})
    responses.add(responses.POST, QUERY_URL, status=502, body="upstream boom")

    pages, err = query_database(DB_ID)

    assert pages == []
    assert err is not None
    assert "502" in err


# ─── BLOCK-CHILDREN PAGINATION ─────────────────────────────────────────────────

@responses.activate
def test_get_children_follows_every_page():
    responses.add(responses.GET, CHILDREN_URL, status=200,
                  json={"results": [page(0), page(1)], "has_more": True, "next_cursor": "cursor-1"})
    responses.add(responses.GET, CHILDREN_URL, status=200,
                  json={"results": [page(2)], "has_more": False, "next_cursor": None})

    blocks, err = get_children(BLOCK_ID)

    assert err is None
    assert [b["id"] for b in blocks] == ["page-0", "page-1", "page-2"]
    assert "start_cursor=cursor-1" in responses.calls[1].request.url


@responses.activate
def test_get_children_reports_an_error():
    responses.add(responses.GET, CHILDREN_URL, status=404, body="not found")

    blocks, err = get_children(BLOCK_ID)

    assert blocks == []
    assert "404" in err


# ─── WRITE BATCHING ────────────────────────────────────────────────────────────

@responses.activate
def test_append_children_batches_at_the_100_block_limit():
    responses.add(responses.PATCH, CHILDREN_URL, status=200, json={"results": [page(0)]})
    responses.add(responses.PATCH, CHILDREN_URL, status=200, json={"results": [page(1)]})
    responses.add(responses.PATCH, CHILDREN_URL, status=200, json={"results": [page(2)]})

    created, err = append_children(BLOCK_ID, [page(i) for i in range(250)])

    assert err is None
    assert len(responses.calls) == 3
    sizes = [len(body["children"]) for body in request_bodies()]
    assert sizes == [100, 100, 50], "a batch exceeded Notion's 100-block limit"
    assert len(created) == 3


@responses.activate
def test_append_children_stops_and_reports_a_failed_batch():
    responses.add(responses.PATCH, CHILDREN_URL, status=200, json={"results": [page(0)]})
    responses.add(responses.PATCH, CHILDREN_URL, status=400, body="bad block")

    created, err = append_children(BLOCK_ID, [page(i) for i in range(150)])

    assert "400" in err
    assert len(created) == 1, "partial progress should still be returned to the caller"
    assert len(responses.calls) == 2, "kept sending batches after a failure"


# ─── SEARCH HELPER ─────────────────────────────────────────────────────────────

@responses.activate
def test_search_page_in_db_returns_the_first_match():
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(0), page(1)], "has_more": False})

    found, err = search_page_in_db(DB_ID, "Dune")

    assert err is None
    assert found["id"] == "page-0"
    assert request_bodies()[0]["filter"]["title"] == {"contains": "Dune"}


@responses.activate
def test_search_page_in_db_can_match_exactly():
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [page(0)], "has_more": False})

    search_page_in_db(DB_ID, "Dune", exact=True)

    assert request_bodies()[0]["filter"]["title"] == {"equals": "Dune"}


@responses.activate
def test_search_page_in_db_reports_no_match():
    responses.add(responses.POST, QUERY_URL, status=200,
                  json={"results": [], "has_more": False})

    found, err = search_page_in_db(DB_ID, "Nonexistent")

    assert found is None
    assert "Nonexistent" in err


# ─── RETRY / BACKOFF ───────────────────────────────────────────────────────────

@responses.activate
def test_notion_request_retries_a_server_error_then_succeeds(no_real_sleeping):
    responses.add(responses.POST, QUERY_URL, status=500, body="boom")
    responses.add(responses.POST, QUERY_URL, status=200, json={"results": []})

    resp = notion_request("POST", QUERY_URL, json={})

    assert resp.status_code == 200
    assert len(responses.calls) == 2
    assert no_real_sleeping == [1], "backoff should be 1s before the first retry"


@responses.activate
def test_notion_request_retries_rate_limiting(no_real_sleeping):
    responses.add(responses.POST, QUERY_URL, status=429, body="slow down")
    responses.add(responses.POST, QUERY_URL, status=429, body="slow down")
    responses.add(responses.POST, QUERY_URL, status=200, json={"results": []})

    resp = notion_request("POST", QUERY_URL, json={})

    assert resp.status_code == 200
    assert no_real_sleeping == [1, 2], "backoff should double between retries"


@responses.activate
def test_notion_request_gives_up_after_max_retries(no_real_sleeping):
    for _ in range(3):
        responses.add(responses.POST, QUERY_URL, status=503, body="unavailable")

    resp = notion_request("POST", QUERY_URL, json={})

    assert resp.status_code == 503, "the last response should be returned, not raised"
    assert len(responses.calls) == 3


@responses.activate
def test_notion_request_does_not_retry_a_client_error(no_real_sleeping):
    """A 401 or 400 will never fix itself — retrying just triples the latency."""
    responses.add(responses.POST, QUERY_URL, status=401, body="unauthorized")

    resp = notion_request("POST", QUERY_URL, json={})

    assert resp.status_code == 401
    assert len(responses.calls) == 1
    assert no_real_sleeping == []


@responses.activate
def test_notion_request_sends_the_auth_headers():
    responses.add(responses.POST, QUERY_URL, status=200, json={})

    notion_request("POST", QUERY_URL, json={})

    headers = responses.calls[0].request.headers
    assert headers["Authorization"] == "Bearer test-notion-key"
    assert headers["Notion-Version"] == "2022-06-28"


# ─── CONNECTION POOLING ────────────────────────────────────────────────────────
# Losing pooling is invisible: every test above still passes against a fresh
# requests.request() per call, and production just quietly pays a TLS handshake
# per request again. These assert the mechanism, because nothing else can.

def test_the_session_carries_the_auth_headers():
    """Headers moved onto the Session — an empty one would send unauthenticated."""
    headers = notion_client._session().headers

    assert headers["Authorization"] == "Bearer test-notion-key"
    assert headers["Notion-Version"] == "2022-06-28"


def test_the_same_thread_reuses_one_session():
    assert notion_client._session() is notion_client._session(), (
        "a new Session per call defeats the whole point of pooling")


def test_each_thread_gets_its_own_session():
    """requests.Session is not thread-safe and every call now runs in a
    to_thread worker, so sharing one across threads can hand the same pooled
    socket to two requests at once."""
    from concurrent.futures import ThreadPoolExecutor

    main_session = notion_client._session()
    with ThreadPoolExecutor(max_workers=2) as pool:
        worker_sessions = [f.result() for f in
                           [pool.submit(notion_client._session) for _ in range(2)]]

    assert all(s is not main_session for s in worker_sessions), (
        "a worker thread shared the main thread's Session")


def test_notion_request_goes_through_the_pooled_session(monkeypatch):
    """The call itself must use the Session, not the module-level requests.request."""
    sent = []

    class FakeResponse:
        status_code = 200

    def spy_request(method, url, **kwargs):
        sent.append((method, url, kwargs.get("timeout")))
        return FakeResponse()

    monkeypatch.setattr(notion_client._session(), "request", spy_request)

    resp = notion_request("POST", QUERY_URL, json={})

    assert resp.status_code == 200
    assert sent == [("POST", QUERY_URL, 15)], "the request bypassed the Session"


# ─── FLATTENING BLOCKS BACK TO TEXT ────────────────────────────────────────────
# blocks_to_text replaced three near-identical flatteners (this one, plus
# implement_diet._content_to_text and _content_to_text_deep). These pin the two
# styles apart, and pin down the difference that was not cosmetic.

def _block(btype, text):
    return {"type": btype, btype: {"rich_text": [{"plain_text": text}]}}


PAGE = [
    _block("heading_2", "Perfect Process"),
    _block("paragraph", "Some prose."),
    _block("bulleted_list_item", "A point."),
    _block("numbered_list_item", "A step."),
    _block("quote", "A quotation."),
    _block("callout", "An aside."),
    {"type": "divider", "divider": {}},
]


def test_markdown_style_keeps_the_structure():
    assert notion_client.blocks_to_text(PAGE) == (
        "## Perfect Process\n"
        "Some prose.\n"
        "• A point.\n"
        "- A step.\n"
        '> "A quotation."\n'
        "> 💡 An aside.\n"
        "---"
    )


def test_plain_style_is_the_text_alone():
    """No prefixes, no dividers: the caller already knows which section this is.

    read_section_contents addresses a section by path and stores its content as the
    value, so a `##` inside that value would be describing structure the tree
    already expresses.
    """
    assert notion_client.blocks_to_text(PAGE, style="plain") == (
        "Perfect Process\n"
        "Some prose.\n"
        "A point.\n"
        "A step.\n"
        "A quotation.\n"
        "An aside."
    )


def test_a_block_type_with_no_prefix_is_flattened_rather_than_dropped():
    """THE DIFFERENCE THAT WASN'T COSMETIC.

    The old blocks_to_text listed the types it knew and dropped everything else,
    so a `to_do` in a Manual section was invisible to Claude — while staying very
    much visible to the write-back, which takes Section.content_ids and deletes
    every one of them. The merged content that replaced the section had therefore
    been computed as though that block never existed, and the delete made that
    true. Anything carrying text is flattened now, prefix or no prefix.
    """
    todo = {"type": "to_do", "to_do": {"rich_text": [{"plain_text": "Buy oats"}]}}

    assert notion_client.blocks_to_text([todo]) == "Buy oats"
    assert notion_client.blocks_to_text([todo], style="plain") == "Buy oats"


def test_blocks_with_no_text_contribute_no_lines():
    """An empty paragraph is spacing in Notion, not content."""
    empty = {"type": "paragraph", "paragraph": {"rich_text": []}}

    assert notion_client.blocks_to_text([empty]) == ""
