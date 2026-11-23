"""`Giornale` (and `/giornale`): build today's newspaper EPUB and send it here.

Detached (bot.tasks.run_detached): a build fetches ~13 feeds and ~50 article
pages, which is seconds on a good day and up to NEWSPAPER_BUILD_TIMEOUT on a bad
one, and the chat must not queue behind it. It writes nothing anywhere — no
Notion, no calendar — so running beside another command cannot reorder a write.
The same build runs from proactive.scheduler every morning.
"""

import asyncio
import logging

import config
from bot.tasks import run_detached
from clients.calendar_client import now_local
from services.newspaper import build_newspaper

logger = logging.getLogger(__name__)

PREPARING = "📰 Sto preparando il giornale…"


async def cmd_giornale(update, context, args):
    run_detached(context, update, handle_giornale(update, context), "giornale")


async def handle_giornale(update, context):
    await update.message.reply_text(PREPARING)
    try:
        result, err = await asyncio.wait_for(asyncio.to_thread(build_newspaper, now_local()),
                                             timeout=config.NEWSPAPER_BUILD_TIMEOUT)
    except asyncio.TimeoutError:
        err = f"took longer than {config.NEWSPAPER_BUILD_TIMEOUT} seconds"
    if err:
        logger.warning("Giornale failed: %s", err)
        # Plain text: `err` quotes YAML paths and feed names as they are.
        await update.message.reply_text(f"❌ Non sono riuscito a preparare il giornale: {err}")
        return
    await update.message.reply_document(document=result.epub, filename=result.filename,
                                        caption=result.caption)
