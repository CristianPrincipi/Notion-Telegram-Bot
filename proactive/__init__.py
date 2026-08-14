"""
Proactive surfacing system for David — scheduled, push-style messages.

Each proactive feature lives in its own module here; register its job(s) in
proactive/scheduler.py -> register_all(), which david.register_jobs() calls once
at startup. Nothing in this package imports david.py, which keeps the bot's
entry module free of circular imports.

These jobs replaced the old david.send_daily_reminders: the morning briefing
owns the 07:30 slot it used, and the evening briefing owns tomorrow's events.

Build order (see the roadmap):
  Step 1  briefing.build_morning_briefing      ← implemented
  Step 2  briefing.build_evening_briefing      ← implemented
  Step 3  calendar_watch (conflict detection)  ← not built as a job: the check
          exists as clients.calendar_client.find_conflicts, called by
          reminder.handle_remind when an event is CREATED rather than on a
          schedule
  Step 4  budget_watch   (pacing / overspend)  ← implemented
  Step 5  takeaway.build_takeaway             ← implemented
  Step 6  learn_nudge.build_nudge              ← implemented
  Step 7  tasks          (overdue follow-up + Done command, needs state)

Steps 5 and 6 are the two that read Notion back OUT. Everything else in David
pushes into it: Step 6 gives the `Implemented` checkbox a reader at last (both
Implement paths had been ticking it since they were written, and nothing looked
at it), and Step 5 resurfaces a takeaway, because a knowledge base you only ever
write to is not a knowledge base.
"""
