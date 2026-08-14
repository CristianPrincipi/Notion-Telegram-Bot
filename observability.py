"""Logging setup, per-update correlation IDs, and the counters the heartbeat reads.

WHY THIS IS A MODULE. Diagnostics used to be print() — no level, no timestamp, no
way to tell one command's trace from another's when two ran close together, and
no way to turn the volume up on Railway without editing code. Three separate
problems, one home.

THE CORRELATION ID is the part that changes how debugging feels. Every log line
emitted while handling an update carries that update's ID, so `grep '[12345]'` on
the Railway log returns that command's entire trace — router decision, Notion
calls, Anthropic tokens, the error — and nothing else. Without it the lines of two
overlapping commands interleave and neither can be read.

It rides on a ContextVar rather than a parameter threaded through forty functions,
and that works across both boundaries David actually crosses:

  - run_detached → Application.create_task, and asyncio.Task copies the current
    context at creation;
  - every blocking call → asyncio.to_thread, which runs the function inside
    contextvars.copy_context().

So a Notion write logged from a worker thread inside a detached Learn still
carries the update ID of the message that started it.

THE COUNTERS are in memory on purpose. They answer "what has David been doing
since it last spoke", and Railway's filesystem is ephemeral anyway — a restart
resetting them is honest, and the heartbeat says so rather than implying the
numbers are all-time.
"""

import contextvars
import logging
import os
import threading

# The ID of the update being handled, or "-" outside one (startup, scheduled
# jobs). Read by the log record factory below; never read directly elsewhere.
_correlation_id = contextvars.ContextVar("correlation_id", default="-")

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [%(cid)s]: %(message)s"

_VALID_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")

_factory_installed = False


# ─── CORRELATION ID ────────────────────────────────────────────────────────────

def set_correlation_id(value) -> None:
    """Tag everything logged from here on with `value`. Call once per update."""
    _correlation_id.set(str(value) if value is not None else "-")


def correlation_id() -> str:
    return _correlation_id.get()


def _install_record_factory() -> None:
    """Give every LogRecord a `cid` attribute.

    A record factory rather than a logging.Filter on the handler: a filter only
    runs for the handlers it is attached to, so a record reaching any other
    handler would have no `cid` and LOG_FORMAT would raise while formatting it —
    turning a log line into an exception. The factory guarantees the attribute
    exists on every record, whoever ends up handling it (pytest's caplog included).
    """
    global _factory_installed
    if _factory_installed:
        return

    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        record.cid = _correlation_id.get()
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


# ─── SETUP ─────────────────────────────────────────────────────────────────────

def resolve_level(raw) -> tuple:
    """(level, complaint). An unusable LOG_LEVEL falls back to INFO, loudly.

    Returned rather than raised: a typo in an optional variable should not stop
    the bot from starting, but it must not silently mean "you get INFO" either —
    someone who set LOG_LEVEL=DEGUB is debugging something and deserves to know
    why it did not take effect.
    """
    name = (raw or "INFO").strip().upper()
    if name in _VALID_LEVELS:
        return getattr(logging, name), None
    return logging.INFO, (
        f"LOG_LEVEL={raw!r} is not one of {', '.join(_VALID_LEVELS)} — using INFO."
    )


def setup_logging(level=None) -> None:
    """Configure the root logger. Call once, before anything logs.

    Called at david.py IMPORT scope, not from __main__, because config.validate()
    runs first thing in __main__ and emits warnings for unset optional variables —
    they need somewhere to go.
    """
    resolved, complaint = resolve_level(level if level is not None else os.environ.get("LOG_LEVEL"))

    _install_record_factory()

    # basicConfig is a no-op once the root logger has handlers, which is exactly
    # what happens on a second call, so level and format are set explicitly too.
    logging.basicConfig(level=resolved, format=LOG_FORMAT)
    root = logging.getLogger()
    root.setLevel(resolved)
    for handler in root.handlers:
        handler.setFormatter(logging.Formatter(LOG_FORMAT))

    if complaint:
        logging.getLogger(__name__).warning("%s", complaint)


# ─── COUNTERS ──────────────────────────────────────────────────────────────────
#
# Written from handler coroutines on the event loop AND from the error handler,
# and read from a worker thread when the heartbeat builds its report — so the
# lock is a threading.Lock, not an asyncio one. An asyncio.Lock between two
# threads acquires without ever blocking, which is the same trap services/month.py
# documents.

_counters_lock = threading.Lock()
_counters = {"commands": 0, "errors": 0}


def record_command() -> None:
    """One inbound command accepted from the owner."""
    with _counters_lock:
        _counters["commands"] += 1


def record_error() -> None:
    """One exception caught by the global error handler."""
    with _counters_lock:
        _counters["errors"] += 1


def snapshot() -> dict:
    """Current counts. A copy, so a caller cannot mutate the live totals."""
    with _counters_lock:
        return dict(_counters)


def reset_counters() -> None:
    """Back to zero. Used by the tests; the heartbeat deliberately does NOT call
    it, so the numbers stay cumulative-since-restart rather than resetting to
    zero every week and hiding what happened between two failed heartbeats."""
    with _counters_lock:
        _counters["commands"] = 0
        _counters["errors"] = 0
