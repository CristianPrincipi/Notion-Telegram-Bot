"""The Kindle quote endpoint: a small aiohttp server beside the Telegram poller.

    POST /kindle/quote   Authorization: Bearer <KINDLE_TOKEN>
                         {"id", "book", "author", "title", "text"}  →  services.kindle
    GET  /kindle/ping    Authorization: Bearer <KINDLE_TOKEN>  →  {"ok": true}

WHY A SERVER AND NOT A TELEGRAM MESSAGE. A bot cannot message another bot, and
the Kindle has no Telegram client; the alternative was the third-party relay the
"Send to Bot" plugin used, which put every highlight through someone else's
server. This keeps the path Kindle → David → Notion.

THE GATE. OWNER_ID gates Telegram; here the gate is KINDLE_TOKEN, compared in
constant time. Without it set, nothing listens at all (`start` returns None and
says so once) — a public endpoint with no secret is not a degraded mode.

STATUS CODES ARE THE KINDLE'S QUEUE POLICY, so they are chosen by `retryable`,
not by taste: 200 saved (or already saved), 422 refused for good (drop it),
503 try again later (keep it queued). 400/401 are the plugin's own bug or a
wrong token — both shown on the Kindle, neither retried.

Runs in the same event loop as PTB, inside the one server bot/http_server.py
starts (with the Android assistant's routes beside these), so the service's
Notion calls go through asyncio.to_thread like every other command's.
"""

import hmac
import logging

from aiohttp import web

from config import KINDLE_MAX_REQUEST_BYTES
from services import kindle

logger = logging.getLogger(__name__)

FIELD_LIMITS = {"id": 100, "book": 500, "author": 500, "title": 500}


def _authorized(request, token):
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].encode(), token.encode())


def _parse(payload):
    """(KindleQuote, None) or (None, reason)."""
    if not isinstance(payload, dict):
        return None, "body must be a JSON object"
    values = {}
    for field in ("id", "book", "author", "title", "text"):
        value = payload.get(field, "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            return None, f"'{field}' must be a string"
        limit = FIELD_LIMITS.get(field)
        if limit and len(value) > limit:
            return None, f"'{field}' is longer than {limit} characters"
        values[field] = value
    return kindle.KindleQuote(**values), None


def build_app(*, token, notify):
    """An application with only the Kindle's routes, for the tests."""
    app = web.Application(client_max_size=KINDLE_MAX_REQUEST_BYTES)
    add_routes(app, token=token, notify=notify)
    return app


def add_routes(app, *, token, notify):
    """The Kindle's routes, on the server bot/http_server.py starts."""

    async def quote(request):
        if not _authorized(request, token):
            return web.json_response({"ok": False, "error": "Wrong or missing token"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "Body is not valid JSON"}, status=400)
        parsed, reason = _parse(payload)
        if reason:
            return web.json_response({"ok": False, "error": reason}, status=400)

        result, err = await kindle.run_kindle_quote(parsed, notify=notify)
        if err:
            status = 503 if err.retryable else 422
            return web.json_response(
                {"ok": False, "error": err.message, "retry": err.retryable}, status=status)
        return web.json_response({"ok": True, **result})

    async def ping(request):
        if not _authorized(request, token):
            return web.json_response({"ok": False, "error": "Wrong or missing token"}, status=401)
        return web.json_response({"ok": True})

    app.router.add_post("/kindle/quote", quote)
    app.router.add_get("/kindle/ping", ping)
