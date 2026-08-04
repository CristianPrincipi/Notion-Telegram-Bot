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
  Step 2  briefing.build_evening_briefing
  Step 3  calendar_watch (conflict detection)
  Step 4  budget_watch   (pacing / overspend)
  Step 5  knowledge.takeaway_of_the_week
  Step 6  knowledge.unimplemented_learn_nudge
  Step 7  tasks          (overdue follow-up + Done command, needs state)
"""
