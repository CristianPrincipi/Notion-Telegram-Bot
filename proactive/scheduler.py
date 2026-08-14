"""
Registers every proactive job onto the python-telegram-bot JobQueue.

david.register_jobs() calls register_all(application, CHAT_ID) once at startup.
Add one run_daily / run_repeating call per feature as you build the roadmap.

Error handling mirrors david.notify_error but is kept local on purpose: this
package never imports david.py (david.py is the entrypoint module, so importing
from it would re-execute it under a second name).
"""

import asyncio
import logging
from datetime import time

import pytz
from telegram.ext import ContextTypes

from config import (
    PROACTIVE_TIMEZONE,
    MORNING_BRIEFING_HOUR, MORNING_BRIEFING_MINUTE,
    EVENING_BRIEFING_HOUR, EVENING_BRIEFING_MINUTE,
    BUDGET_PACING_HOUR, BUDGET_PACING_MINUTE,
    MONTH_ROLLOVER_HOUR, MONTH_ROLLOVER_MINUTE,
    HEARTBEAT_DAY, HEARTBEAT_HOUR, HEARTBEAT_MINUTE,
    LEARN_NUDGE_DAY, LEARN_NUDGE_HOUR, LEARN_NUDGE_MINUTE,
)
from proactive.briefing import build_morning_briefing, build_evening_briefing
from proactive.budget_watch import build_pacing_warning
from proactive.heartbeat import build_heartbeat
from proactive.learn_nudge import build_nudge
from proactive.month_rollover import build_rollover_message
from telegram_text import send

logger = logging.getLogger(__name__)

_TZ = pytz.timezone(PROACTIVE_TIMEZONE)


async def _report_error(context: ContextTypes.DEFAULT_TYPE, where: str, err):
    """Print + ping the owner when a proactive job fails.

    PLAIN TEXT, DELIBERATELY — do not add parse_mode back. Same reasoning as
    david.notify_error: this interpolates an error string, Notion 400 bodies and
    tracebacks carry unbalanced * _ ` and [, and under parse_mode="Markdown" the
    report itself raised BadRequest and was swallowed by the except below. The
    text sat in a `code span`, where Markdown v1 ignores backslash escapes, so
    escape_md() cannot rescue it either — this sender is exempt from
    telegram_text on purpose.

    `err` is an Exception or a plain error string: builders now return
    (text, error) tuples, and a returned error deserves the same report as a
    raised one.
    """
    detail = f"{type(err).__name__}: {err}" if isinstance(err, BaseException) else str(err)
    logger.error("proactive job %s failed: %s", where, detail,
                 exc_info=err if isinstance(err, BaseException) else None)
    try:
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=f"⚠️ David proactive error in {where}:\n{detail}",
        )
    except Exception:
        logger.exception("proactive job %s could not report its failure.", where)


# Every builder below reads Google Calendar and/or Notion synchronously, so they
# all run on worker threads. A job that blocks the event loop is no better than a
# command that does: it freezes inbound commands and the other jobs with it, and
# the briefings fire at fixed times whether or not you are mid-conversation.
#
# ONE SEND PATH. Every builder returns (text, error), so this helper is the single
# place a proactive message is sent and a proactive failure is reported. It used
# to be four copy-pasted callbacks that each did `if text:` and nothing else —
# which is how a returned error could be dropped on the floor four different ways.
#
# The three states, kept apart:
#   (text, None)  → send it
#   (None, None)  → genuinely nothing to say; stay silent
#   (_,    error) → report the error, AND send the text if there is one


async def _run_job(context: ContextTypes.DEFAULT_TYPE, where: str, builder, markdown=False):
    try:
        text, err = await asyncio.to_thread(builder)
    except Exception as e:
        await _report_error(context, where, e)
        return

    if text:
        if markdown:
            await send(context.bot, context.job.chat_id, text)
        else:
            # Plain text on purpose: event titles and diagnostics may contain
            # Markdown-special characters (_ * `) that would break parsing.
            await context.bot.send_message(chat_id=context.job.chat_id, text=text)

    if err:
        await _report_error(context, where, err)


async def _morning_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    await _run_job(context, "morning_briefing", build_morning_briefing)


async def _evening_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    await _run_job(context, "evening_briefing", build_evening_briefing)


async def _budget_pacing_job(context: ContextTypes.DEFAULT_TYPE):
    await _run_job(context, "budget_pacing", build_pacing_warning)


async def _month_rollover_job(context: ContextTypes.DEFAULT_TYPE):
    # Markdown, unlike the others: the message carries a Notion page ID and
    # backticks make it one tap to copy.
    #
    # The old comment here claimed nothing user-written is interpolated. That held
    # only for the success path — format_rollover's ERROR branch interpolates a
    # raw Notion error string, so the rollover failure notice was the message most
    # likely to be rejected. month.py escapes it now, and send() retries plain if
    # anything still slips through.
    await _run_job(context, "month_rollover", build_rollover_message, markdown=True)


async def _heartbeat_job(context: ContextTypes.DEFAULT_TYPE):
    await _run_job(context, "heartbeat", build_heartbeat)


async def _learn_nudge_job(context: ContextTypes.DEFAULT_TYPE):
    # Plain, like the briefings: every line interpolates a Notion page title,
    # which is user data and routinely carries _ and *.
    await _run_job(context, "learn_nudge", build_nudge)


def register_all(application, chat_id):
    """Register all proactive jobs. Call once, at startup."""
    jq = application.job_queue
    if jq is None:
        logger.warning("JobQueue unavailable — proactive jobs not registered "
                       "(install python-telegram-bot[job-queue]).")
        return

    jq.run_daily(
        _morning_briefing_job,
        time=time(hour=MORNING_BRIEFING_HOUR, minute=MORNING_BRIEFING_MINUTE, tzinfo=_TZ),
        chat_id=chat_id,
        name="morning_briefing",
    )

    jq.run_daily(
        _evening_briefing_job,
        time=time(hour=EVENING_BRIEFING_HOUR, minute=EVENING_BRIEFING_MINUTE, tzinfo=_TZ),
        chat_id=chat_id,
        name="evening_briefing",
    )

    jq.run_daily(
        _budget_pacing_job,
        time=time(hour=BUDGET_PACING_HOUR, minute=BUDGET_PACING_MINUTE, tzinfo=_TZ),
        chat_id=chat_id,
        name="budget_pacing",
    )

    # Daily, not monthly, though it only ever has something to do on the 1st —
    # see the note in config.py. A missed or failed rollover retries tomorrow
    # rather than in a month's time, and the run is a no-op on the other 30 days.
    jq.run_daily(
        _month_rollover_job,
        time=time(hour=MONTH_ROLLOVER_HOUR, minute=MONTH_ROLLOVER_MINUTE, tzinfo=_TZ),
        chat_id=chat_id,
        name="month_rollover",
    )

    # Weekly, unlike everything else here. It always sends, so a daily one would
    # be noise; weekly is frequent enough to catch an outage and regular enough
    # that a missing message is noticeable. See config.py.
    jq.run_daily(
        _heartbeat_job,
        time=time(hour=HEARTBEAT_HOUR, minute=HEARTBEAT_MINUTE, tzinfo=_TZ),
        days=(HEARTBEAT_DAY,),
        chat_id=chat_id,
        name="heartbeat",
    )

    # Weekly, like the heartbeat and for the mirror-image reason: the heartbeat
    # always speaks, so daily would be noise; this one's list barely changes
    # between one day and the next, so daily would be the same noise.
    jq.run_daily(
        _learn_nudge_job,
        time=time(hour=LEARN_NUDGE_HOUR, minute=LEARN_NUDGE_MINUTE, tzinfo=_TZ),
        days=(LEARN_NUDGE_DAY,),
        chat_id=chat_id,
        name="learn_nudge",
    )

    logger.info("Proactive jobs registered: morning_briefing, evening_briefing, "
                "budget_pacing, month_rollover, heartbeat, learn_nudge.")
