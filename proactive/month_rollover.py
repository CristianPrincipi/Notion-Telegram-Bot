"""
Month Rollover — the job that keeps expenses landing in the right month.

month.py owns the work and the wording; this module owns the one thing that is a
*proactive* decision: when the job is worth interrupting you for.

  • the MONTH moved → say so, with the new page ID. In a healthy deploy that is
    the 1st and only the 1st.
  • the same month, resolved again → silence, like the evening briefing on an
    empty tomorrow. The job runs every night; a nightly "still August" ping would
    train you to ignore it, and this is a message you want to read on the one
    night it matters.
  • a failure → always speak. A silent failure here is the original bug in a new
    costume: expenses keep being written to LAST month's page and nothing says so.

WHY THE TEST IS `rolled_over` AND NOT `changed`
-----------------------------------------------
It was `changed`, and that is why the message arrived every day rather than
monthly. `changed` means the run WROTE something, and a run writes something
every time a fresh process resolves the month for the first time — which is every
deploy, since the resolved page is cached in memory only. Correct behaviour,
correctly logged, but "✅ Monthly expenses page updated" on a day nothing about
the month changed is a false claim, and a claim that arrives that often is one you
stop reading, which costs you the one on the 1st.

`rolled_over` fires on the month turning over. A run that started not knowing
which month it was on — which every fresh process does — cannot claim the month
moved, so it stays quiet unless it actually CREATED the page, which is news
whoever asked for it and can only happen once a month. See
month.Rollover.rolled_over.
"""

from month import ensure_current_month_page, format_rollover


def build_rollover_message() -> tuple:
    """Roll the month page forward. Returns (message, error).

    The failure text is returned as the MESSAGE, not only as the error: this is
    the one builder that already spoke on failure, and its wording (which page,
    which month) is more useful to read than a bare exception. The error is
    returned alongside so the scheduler logs it at ERROR too.
    """
    result = ensure_current_month_page()

    if result.error:
        return format_rollover(result), result.error
    if not result.rolled_over:
        return None, None
    return format_rollover(result), None
