"""The layering rule, enforced by a test rather than by discipline.

THE RULE
--------
    Nothing under services/ may import telegram, and no service function may
    take `update` as a parameter.

WHY IT IS MECHANICAL. Every architectural rule in this repo that was left to
discipline eventually broke, and the ones that held are the ones with a test
under them: telegram_text has an ast walk that fails on a stray parse_mode,
the weekday constants have a scan of every `days=` call site, page_lock has a
scan of every `page_lock(` key. Each of those was added AFTER the rule had
already been violated once. This one is written before there is anything under
services/ to violate it, so the first violation is the one that turns it red.

WHY AST AND NOT GREP. A docstring may legitimately discuss `update`, and this
module's own prose says "import telegram" several times. Only a real import
statement and a real parameter name count as offences.

WHAT EACH CHECK CATCHES, and why it is not redundant with the others:

  import telegram        the direct welding. `from telegram.ext import
                         ContextTypes` counts — a service that names a PTB type
                         in a signature is a service you cannot call from a job.

  a parameter named      the indirect welding, and the one that survives an
  `update`               import check untouched: a function typed
                         `def run(update, ...)` needs no import at all to be
                         unusable from anywhere but a handler.

  `.message.reply_text`  the same welding with the parameter renamed. A service
  / `.reply_text(...)`   that reaches into `whatever.message.reply_text` is
                         sending, not reporting, whatever it calls its argument.

  layer direction        services must not import bot, and clients must not
                         import services or bot. Without this the package names
                         are decoration: a "service" that imports a handler is
                         the same tangle with more directories.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

SERVICES = REPO / "services"
CLIENTS  = REPO / "clients"
BOT      = REPO / "bot"


def python_files(directory: pathlib.Path) -> list:
    """Every .py file in a package, __init__ included."""
    return sorted(p for p in directory.rglob("*.py"))


# ─── THE CHECKS ────────────────────────────────────────────────────────────────

def _imported_roots(tree: ast.AST):
    """(line, module) for every import, by its ROOT package name.

    ast.walk rather than tree.body: an import inside a function or a `try` is
    still an import, and moving one there is the obvious way to slip past a
    check that only reads the top of the file.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            # A relative import has no module root to report; level > 0 means it
            # cannot reach another top-level package anyway.
            if node.level == 0 and node.module:
                yield node.lineno, node.module.split(".")[0]


def _parameter_named(tree: ast.AST, name: str):
    """(line, function) for every def whose signature takes `name`."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        every = (list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
                 + [a for a in (args.vararg, args.kwarg) if a is not None])
        if any(arg.arg == name for arg in every):
            yield node.lineno, node.name


def _telegram_sends(tree: ast.AST):
    """(line, call) for every direct send to Telegram, whatever it is called on.

    Matches the method NAME rather than the object, because the object is the
    thing a rename hides: `update.message.reply_text` and
    `whatever.message.reply_text` are the same offence.
    """
    senders = ("reply_text", "send_message", "reply_markdown", "reply_html")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in senders:
                yield node.lineno, node.func.attr


def layering_offences(path: pathlib.Path, forbidden_imports=("telegram",)) -> list:
    """Every way `path` breaks the rule, as readable strings."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []

    for line, root in _imported_roots(tree):
        if root in forbidden_imports:
            found.append(f"{path.name}:{line}: imports {root}")
    for line, func in _parameter_named(tree, "update"):
        found.append(f"{path.name}:{line}: {func}() takes `update`")
    for line, call in _telegram_sends(tree):
        found.append(f"{path.name}:{line}: calls {call}() directly")

    return found


# ─── THE RULE ──────────────────────────────────────────────────────────────────

def test_no_service_touches_telegram():
    """THE acceptance test. A service welded to Telegram cannot be driven from a
    test without a fake Update, reused from a scheduled job, or called from
    anything else — so every other caller needs its own copy of the logic, and
    the copies drift. That is not hypothetical: reminder.build_today_message and
    build_tomorrow_message were exactly that, and carried a bug that had already
    been fixed in the live copy.

    Progress reports go out through the `notify` / `notify_md` callbacks instead.
    See services/__init__.py.
    """
    offenders = [line for path in python_files(SERVICES)
                 for line in layering_offences(path)]

    assert offenders == [], (
        "services/ must not know about Telegram — take a `notify` callback "
        "instead, and let bot/ bind it to reply_text:\n" + "\n".join(offenders))


def test_the_services_scan_is_looking_at_real_files():
    """Guards the guard against passing vacuously.

    A scan of a directory that does not exist, or that the glob no longer
    matches, reports zero offenders and looks exactly like compliance. This is
    the same blind spot test_data_integrity closes for its weekday scan.
    """
    assert SERVICES.is_dir(), "services/ does not exist — the rule scans nothing"
    assert python_files(SERVICES), "no .py files under services/ — the glob is wrong"


@pytest.mark.parametrize("source, expected", [
    ("import telegram\n",                                    "imports telegram"),
    ("from telegram.ext import ContextTypes\n",              "imports telegram"),
    ("def f():\n    import telegram\n",                      "imports telegram"),
    ("async def run(update, x):\n    pass\n",                "takes `update`"),
    ("def run(*, update=None):\n    pass\n",                 "takes `update`"),
    ("async def run(u):\n    await u.message.reply_text('hi')\n",
     "calls reply_text() directly"),
])
def test_the_guard_can_actually_detect_an_offender(tmp_path, source, expected):
    """A guard that cannot fail is not a guard.

    Each row is a real way this rule has been broken elsewhere in the codebase:
    the plain import, the PTB-type import, the import hidden inside a function,
    the positional `update`, the keyword-only `update` that a positional-only
    check would miss, and the send made through a renamed parameter.
    """
    offender = tmp_path / "bad.py"
    offender.write_text(source, encoding="utf-8")

    offences = layering_offences(offender)

    assert any(expected in line for line in offences), (
        f"the guard missed {source!r} — it reported {offences}")


def test_the_guard_finds_the_offences_that_really_are_in_david():
    """The positive control, against a real file rather than a synthetic one.

    david.py is the transport layer and is SUPPOSED to import telegram, take
    `update` and call reply_text. Pointing the detector at it proves the checks
    fire on this repo's own code, not just on a string written in this test.
    """
    offences = layering_offences(REPO / "david.py")

    assert any("imports telegram" in line for line in offences)
    assert any("takes `update`" in line for line in offences)
    assert any("calls reply_text() directly" in line for line in offences)


# ─── THE DIRECTION OF THE ARROWS ───────────────────────────────────────────────

def test_services_never_import_the_bot_layer():
    """bot → services → clients, one way.

    Without this the directories are decoration: a "service" that imports a
    handler is the same tangle david.py was, just spread over more files.
    """
    offenders = [f"{path.name}:{line}: imports {root}"
                 for path in python_files(SERVICES)
                 for line, root in _imported_roots(ast.parse(path.read_text(encoding="utf-8")))
                 if root == "bot"]

    assert offenders == [], "services/ must not depend on bot/:\n" + "\n".join(offenders)


def test_clients_never_import_the_layers_above_them():
    """A client owns transport, not meaning. It cannot need a service to make a
    request, and if it does, the meaning has leaked downwards."""
    offenders = [f"{path.name}:{line}: imports {root}"
                 for path in python_files(CLIENTS)
                 for line, root in _imported_roots(ast.parse(path.read_text(encoding="utf-8")))
                 if root in ("services", "bot")]

    assert offenders == [], "clients/ must not depend on services/ or bot/:\n" + "\n".join(offenders)


def test_every_layer_exists_and_is_a_package():
    """`import services.expenses` has to work from the repo root, which is what
    pytest.ini's `pythonpath = .` and the Procfile's `python david.py` both
    assume. A directory without __init__.py imports as a namespace package on
    3.11 and would work by accident here — asserted so it stays deliberate."""
    for package in (SERVICES, CLIENTS, BOT):
        assert (package / "__init__.py").is_file(), f"{package.name}/ has no __init__.py"
