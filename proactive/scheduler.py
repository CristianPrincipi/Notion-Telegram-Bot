"""
Registers every proactive job onto the python-telegram-bot JobQueue.

david.py __main__ calls register_all(application, CHAT_ID) once at startup.
Add one run_daily / run_repeating call per feature as you build the roadmap.

Error handling mirrors david.notify_error but is kept local on purpose: this
package never imports david.py (david.py is the entrypoint module, so importing
from it would re-execute it under a second name).
"""

from datetime import time

import pytz
from telegram.ext import ContextTypes

from config import (
    PROACTIVE_TIMEZONE,
    MORNING_BRIEFING_HOUR, MORNING_BRIEFING_MINUTE,
    EVENING_BRIEFING_HOUR, EVENING_BRIEFING_MINUTE,
    BUDGET_PACING_HOUR, BUDGET_PACING_MINUTE,
)
from proactive.briefing import build_morning_briefing, build_evening_briefing
from proactive.budget_watch import build_pacing_warning

_TZ = pytz.timezone(PROACTIVE_TIMEZONE)


async def _report_error(context: ContextTypes.DEFAULT_TYPE, where: str, err: Exception):
    """Print + ping the owner when a proactive job fails."""
    print(f"[proactive:{where}] {type(err).__name__}: {err}")
    try:
        await context.bot.send_message(
            chat_id=context.job.chat_id,
            text=f"⚠️ David proactive error in *{where}*:\n`{type(err).__name__}: {err}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"[proactive:{where}] failed to report: {e}")


async def _morning_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = build_morning_briefing()
        if text:
            # Plain text on purpose: event titles may contain Markdown-special
            # characters (_ * `) that would break Markdown parsing.
            await context.bot.send_message(chat_id=context.job.chat_id, text=text)
    except Exception as e:
        await _report_error(context, "morning_briefing", e)


async def _evening_briefing_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = build_evening_briefing()
        if text:
            await context.bot.send_message(chat_id=context.job.chat_id, text=text)
    except Exception as e:
        await _report_error(context, "evening_briefing", e)


async def _budget_pacing_job(context: ContextTypes.DEFAULT_TYPE):
    try:
        text = build_pacing_warning()
        if text:  # only when meaningfully trending over — otherwise silent
            await context.bot.send_message(chat_id=context.job.chat_id, text=text)
    except Exception as e:
        await _report_error(context, "budget_pacing", e)


def register_all(application, chat_id):
    """Register all proactive jobs. Call once, at startup."""
    jq = application.job_queue
    if jq is None:
        print("⚠️ JobQueue unavailable — proactive jobs not registered "
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

    print("✅ Proactive jobs registered: morning_briefing, evening_briefing, budget_pacing.")
