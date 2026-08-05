"""
Month Rollover — the job that keeps expenses landing in the right month.

month.py owns the work and the wording; this module owns the one thing that is a
*proactive* decision: when the job is worth interrupting you for.

  • something moved (a page created, renamed, or newly pointed at) → say so, with
    the new page ID
  • nothing to do → silence, like the evening briefing on an empty tomorrow. The
    job runs every night; a nightly "still August" ping would train you to ignore
    it, and this is a message you want to read on the one night it matters.
  • a failure → always speak. A silent failure here is the original bug in a new
    costume: expenses keep being written to LAST month's page and nothing says so.
"""

from month import ensure_current_month_page, format_rollover


def build_rollover_message() -> str | None:
    """Roll the month page forward. Returns the text to send, or None when silent."""
    result = ensure_current_month_page()

    if result.error:
        return format_rollover(result)
    if not result.changed:
        return None
    return format_rollover(result)
