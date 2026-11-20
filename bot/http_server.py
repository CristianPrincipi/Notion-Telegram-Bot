"""David's one HTTP server: an aiohttp app beside the Telegram poller, in the
same event loop, on Railway's $PORT.

    /kindle/*      the Kindle quote endpoint (bot/kindle_http.py), when KINDLE_TOKEN is set
    /assistant/*   the Android assistant (bot/assistant.py), when ASSISTANT_TOKEN is set

Each set of routes has its OWN secret, so the Kindle's token cannot drive the
assistant and the other way round. With neither set nothing listens at all — a
public endpoint with no secret is not a degraded mode — and each one unset says
so once in the log.

Started from PTB's post_init, which does NOT swallow an exception: a port in use
would abort David's whole startup, Telegram included. It logs and carries on
without the server instead (CLAUDE.md: optional features degrade out loud).
"""

import logging

from aiohttp import web

from bot import assistant, kindle_http
from bot.notify import for_chat
from config import ASSISTANT_TOKEN, KINDLE_MAX_REQUEST_BYTES, KINDLE_PORT, KINDLE_TOKEN

logger = logging.getLogger(__name__)


def build_app(bot, chat_id, router, application=None):
    """The routes whose token is set. (app, names) — `names` empty: nothing to serve."""
    app = web.Application(client_max_size=KINDLE_MAX_REQUEST_BYTES)
    names = []
    if KINDLE_TOKEN:
        kindle_http.add_routes(app, token=KINDLE_TOKEN, notify=for_chat(bot, chat_id))
        names.append("Kindle quotes")
    else:
        logger.warning("KINDLE_TOKEN is not set — the Kindle quote endpoint is off.")
    if ASSISTANT_TOKEN:
        assistant.add_routes(app, token=ASSISTANT_TOKEN, assistant=assistant.Assistant(
            router=router, bot=bot, chat_id=chat_id, application=application))
        names.append("Android assistant")
    else:
        logger.warning("ASSISTANT_TOKEN is not set — the Android assistant endpoint is off.")
    return app, names


async def start(bot, chat_id, router, application=None):
    """Start listening on $PORT. Returns the runner to stop, or None."""
    app, names = build_app(bot, chat_id, router, application)
    if not names:
        return None
    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=KINDLE_PORT)
        await site.start()
    except OSError:
        logger.exception("The HTTP endpoint could not start on port %s — "
                         "David runs without it.", KINDLE_PORT)
        await runner.cleanup()
        return None
    logger.info("HTTP endpoint listening on port %s: %s", KINDLE_PORT, ", ".join(names))
    return runner


async def stop(runner):
    if runner is not None:
        await runner.cleanup()
