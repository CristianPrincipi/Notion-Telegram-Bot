"""Logging setup, correlation IDs, and the heartbeat counters.

The correlation ID is the load-bearing part: it has to survive the two boundaries
David crosses on every long command — Application.create_task (run_detached) and
asyncio.to_thread. If it does not, the log lines of two overlapping commands
interleave with nothing to tell them apart, which is the state these tests exist
to prevent returning to.

The last section is a scan over the tests themselves: a test that listens for a log
line has to listen to a logger something logs through.
"""

import asyncio
import logging
import re
from pathlib import Path

import pytest

import observability
from observability import (
    correlation_id, record_command, record_error, reset_counters,
    resolve_level, set_correlation_id, snapshot,
)
from conftest import run


@pytest.fixture(autouse=True)
def clean_counters():
    """Counters are module-level state; a leaked one would poison later tests."""
    reset_counters()
    yield
    reset_counters()


@pytest.fixture(autouse=True)
def clean_correlation_id():
    set_correlation_id(None)
    yield
    set_correlation_id(None)


# ─── LOG_LEVEL ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("DEBUG",    logging.DEBUG),
    ("INFO",     logging.INFO),
    ("WARNING",  logging.WARNING),
    ("ERROR",    logging.ERROR),
    ("CRITICAL", logging.CRITICAL),
    ("debug",    logging.DEBUG),      # case-insensitive
    ("  info  ", logging.INFO),       # tolerant of stray whitespace
])
def test_resolve_level_accepts_every_documented_value(raw, expected):
    level, complaint = resolve_level(raw)
    assert level == expected
    assert complaint is None


def test_an_unset_log_level_defaults_to_info_silently():
    assert resolve_level(None) == (logging.INFO, None)
    assert resolve_level("") == (logging.INFO, None)


def test_a_typo_falls_back_to_info_but_says_so():
    """Someone who set LOG_LEVEL=DEGUB is debugging and must not be left guessing."""
    level, complaint = resolve_level("DEGUB")
    assert level == logging.INFO
    assert complaint is not None
    assert "DEGUB" in complaint


def test_setup_logging_is_safe_to_call_twice():
    """It runs at import; a test or a second import must not chain record factories."""
    observability.setup_logging("INFO")
    observability.setup_logging("INFO")

    record = logging.getLogger("x").makeRecord("x", logging.INFO, "f", 1, "m", None, None)
    assert hasattr(record, "cid")


# ─── CORRELATION ID ────────────────────────────────────────────────────────────

def test_every_log_record_carries_a_correlation_id():
    """The format string references %(cid)s — a record without it would raise."""
    observability.setup_logging("INFO")
    set_correlation_id(4242)

    record = logging.getLogger("x").makeRecord("x", logging.INFO, "f", 1, "m", None, None)

    assert record.cid == "4242"


def test_records_outside_an_update_get_a_placeholder():
    observability.setup_logging("INFO")
    set_correlation_id(None)

    record = logging.getLogger("x").makeRecord("x", logging.INFO, "f", 1, "m", None, None)

    assert record.cid == "-"


def test_the_correlation_id_survives_asyncio_to_thread():
    """Every Notion, Anthropic and PyPDF2 call is offloaded — the tag must follow."""
    set_correlation_id(99)

    async def main():
        return await asyncio.to_thread(correlation_id)

    assert run(main()) == "99"


def test_the_correlation_id_survives_a_detached_task():
    """run_detached hands the long commands to Application.create_task."""
    set_correlation_id(7)
    seen = []

    async def worker():
        seen.append(correlation_id())

    async def main():
        task = asyncio.create_task(worker())
        # A later update must not retag work already in flight.
        set_correlation_id(8)
        await task

    run(main())
    assert seen == ["7"]


def test_two_concurrent_commands_do_not_share_a_tag():
    """The whole point: overlapping traces must stay separable."""
    seen = {}

    async def command(update_id):
        set_correlation_id(update_id)
        await asyncio.sleep(0)                     # let the other one interleave
        seen[update_id] = await asyncio.to_thread(correlation_id)

    async def main():
        await asyncio.gather(
            asyncio.create_task(command(111)),
            asyncio.create_task(command(222)),
        )

    run(main())
    assert seen == {111: "111", 222: "222"}


# ─── COUNTERS ──────────────────────────────────────────────────────────────────

def test_counters_start_at_zero():
    assert snapshot() == {"commands": 0, "errors": 0}


def test_counters_record_commands_and_errors_separately():
    record_command()
    record_command()
    record_error()

    assert snapshot() == {"commands": 2, "errors": 1}


def test_the_snapshot_is_a_copy():
    """A caller mutating the report must not corrupt the live totals."""
    record_command()
    taken = snapshot()
    taken["commands"] = 9999

    assert snapshot()["commands"] == 1


def test_counters_are_safe_across_threads():
    """They are written from the event loop and read from a worker thread."""
    async def main():
        await asyncio.gather(*(asyncio.to_thread(record_command) for _ in range(50)))

    run(main())
    assert snapshot()["commands"] == 50


# ─── TESTS LISTEN TO LOGGERS THAT EXIST ────────────────────────────────────────
#
# `caplog.at_level(INFO, logger="anthropic_client")` raised the level of a logger
# nothing wrote to: the module had moved into `clients/`, and logs through
# `clients.anthropic_client`. The test still passed in the full suite, because an
# earlier file imports `david`, whose setup puts the ROOT logger at INFO — and
# failed when its file ran alone. A stale name is silent in exactly the run
# everybody does, so the names are checked against the loggers the code creates.

REPO = Path(__file__).resolve().parent.parent
PACKAGES = ("bot", "clients", "proactive", "services")


def production_logger_names() -> set:
    """Every logger name production code creates: `__name__` is the module's dotted path."""
    files = sorted(REPO.glob("*.py"))
    for package in PACKAGES:
        files += sorted((REPO / package).rglob("*.py"))

    names = set()
    for path in files:
        module = ".".join(path.relative_to(REPO).with_suffix("").parts)
        for argument in re.findall(r"logging\.getLogger\(([^)]*)\)", path.read_text(encoding="utf-8")):
            argument = argument.strip()
            if argument == "__name__":
                names.add(module)
            elif argument[:1] in ("'", '"'):
                names.add(argument.strip("'\""))
    return names


def sources_of_tests() -> dict:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted((REPO / "tests").glob("*.py"))}


def stale_logger_problems(names: set, sources: dict) -> list:
    """Every `logger=` a test names, against the loggers that exist."""
    listened = re.compile(r"""logger=["']([^"']+)["']""")
    problems = []
    for filename, source in sorted(sources.items()):
        for number, line in enumerate(source.splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue          # a name quoted in a comment is not listened to
            for name in listened.findall(line):
                if name not in names:
                    problems.append(
                        f"tests/{filename}:{number}: listens to logger {name!r}, "
                        "which nothing logs through"
                    )
    return problems


def test_every_logger_a_test_listens_to_exists():
    assert stale_logger_problems(production_logger_names(), sources_of_tests()) == []


def test_a_logger_name_left_behind_by_a_moved_module_is_caught():
    """The real bug, put back in memory: the client's tests listening to its old name."""
    sources = sources_of_tests()
    current = "clients.anthropic_client"
    assert f'="{current}"' in sources["test_anthropic_client.py"]
    sources["test_anthropic_client.py"] = sources["test_anthropic_client.py"].replace(
        f'="{current}"', '="anthropic_client"')

    problems = stale_logger_problems(production_logger_names(), sources)

    assert len(problems) == 1
    assert "test_anthropic_client.py" in problems[0] and "'anthropic_client'" in problems[0]


def test_the_logger_scan_reads_real_loggers_and_real_tests():
    """A scan that finds no loggers, or no listeners, passes by reading nothing."""
    names = production_logger_names()
    assert {"david", "clients.anthropic_client", "services.learn", "telegram_text"} <= names

    listeners = [line for source in sources_of_tests().values() for line in source.splitlines()
                 if re.search(r"""logger=["']""", line)]
    assert len(listeners) >= 5
