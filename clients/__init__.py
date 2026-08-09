"""Everything David talks to over the wire.

One module per external system, and each one is the ONLY place its protocol is
spoken:

  notion_client     headers, per-thread Session, retry/backoff, pagination, blocks
  calendar_client   Google Calendar, per-thread service, and `now_local()` — the
                    project clock
  anthropic_client  complete_json, retry, stop_reason checks, the daily spend guard

A client owns transport, not meaning. It knows how to make a request and how to
report a failure as `(value, error)`; it does not know what a Manual is, which
section a summary belongs in, or what to tell the owner about any of it. That
lives in `services/`, which is the only thing above it.

WHY THEY ARE A PACKAGE AND NOT THREE ROOT MODULES. Reading `from
clients.notion_client import query_database` at the top of a service says which
layer the call crosses, which a bare `from notion_client import …` did not. It
also un-shadows a real PyPI distribution called `notion-client` that this module
has nothing to do with — at the repo root, ours won.

The filenames kept their `_client` suffix through the move. `clients/notion.py`
would read better in isolation and would have made the diff a delete-plus-add of
a 450-line file for anyone reviewing it. The move is the change; the name is not.
"""
