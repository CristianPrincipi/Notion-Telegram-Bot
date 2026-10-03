"""config.validate() — the startup gate.

The point of this function is that a misconfigured deploy dies immediately with
a complete list, instead of running and failing hours later against whichever
command happened to hit Notion first. So the tests care about two things: that
it exits at all, and that ONE run names EVERY problem.

And one thing about the variables it does NOT refuse: a blank optional variable
is an unset one, to the code that reads it as much as to validate().
"""

import ast
import json
import logging
import os
import pathlib
import subprocess
import sys

import pytest

import config
from conftest import FAKE_ENV


@pytest.fixture
def env(monkeypatch):
    """A complete, valid environment that each test can then break."""
    for key, value in FAKE_ENV.items():
        monkeypatch.setenv(key, value)
    for key in config.OPTIONAL_ENV:
        monkeypatch.setenv(key, "set")
    return monkeypatch


# ─── HAPPY PATH ────────────────────────────────────────────────────────────────

def test_a_complete_environment_passes(env):
    config.validate()          # must not raise


def test_optional_vars_only_warn(env, caplog):
    env.delenv("SUPADATA_KEY")
    env.delenv("DIET_ID")

    with caplog.at_level(logging.WARNING):
        config.validate()      # must not raise

    assert "SUPADATA_KEY" in caplog.text
    assert "DIET_ID" in caplog.text


def test_optional_var_warning_explains_what_breaks(env, caplog):
    env.delenv("GOOGLE_CREDENTIALS_JSON")

    with caplog.at_level(logging.WARNING):
        config.validate()

    assert "reminders fail" in caplog.text


def test_no_warnings_when_everything_is_set(env, caplog):
    with caplog.at_level(logging.WARNING):
        config.validate()

    assert caplog.records == []


# ─── REQUIRED VARS ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("missing", sorted(config.REQUIRED_ENV))
def test_each_required_var_is_enforced(env, missing):
    env.delenv(missing)

    with pytest.raises(SystemExit) as exc:
        config.validate()

    assert missing in str(exc.value)


def test_every_missing_var_is_listed_in_one_run(env):
    """The whole point: fix the deploy once, not once per variable."""
    absent = ["NOTION_KEY", "LETTI_ID", "ANTHROPIC_API_KEY", "CHAT_ID"]
    for name in absent:
        env.delenv(name)

    with pytest.raises(SystemExit) as exc:
        config.validate()

    message = str(exc.value)
    for name in absent:
        assert name in message, f"{name} was missing but not reported"
    assert "4 required" in message


def test_the_error_explains_what_each_missing_var_is_for(env):
    env.delenv("EXPENSES_ID")

    with pytest.raises(SystemExit) as exc:
        config.validate()

    assert config.REQUIRED_ENV["EXPENSES_ID"] in str(exc.value)


def test_the_error_says_where_to_set_them_on_either_kind_of_run(env):
    """It used to say only "Set these in the Railway service variables, then
    redeploy" — the wrong advice on a local `dotenv run -- python david.py`,
    which is exactly the run that meets this error first."""
    env.delenv("EXPENSES_ID")

    with pytest.raises(SystemExit) as exc:
        config.validate()

    assert "Railway" in str(exc.value)
    assert ".env" in str(exc.value)


def test_month_id_is_optional(env, caplog):
    """It is a first-boot SEED, not a live value.

    services/month.py resolves the current month page from Notion by title and
    caches the answer, so David starts and runs correctly with MONTH_ID unset.
    It was in REQUIRED_ENV anyway, which killed a deploy over a variable nothing
    reads — and invited the fix of pasting a stale page ID back in to silence the
    error.
    """
    env.delenv("MONTH_ID")

    with caplog.at_level(logging.WARNING):
        config.validate()          # must NOT raise

    assert any("MONTH_ID" in record.message for record in caplog.records), (
        "an unset MONTH_ID should still warn — it changes where the first "
        "expenses of a fresh container land")


def test_a_blank_var_counts_as_missing(env):
    """Railway happily stores an empty string; "Bearer " is no better than "Bearer None"."""
    env.setenv("NOTION_KEY", "   ")

    with pytest.raises(SystemExit) as exc:
        config.validate()

    assert "NOTION_KEY" in str(exc.value)


# ─── OWNER_ID ──────────────────────────────────────────────────────────────────

def test_a_non_numeric_owner_id_is_fatal(env):
    """OWNER_ID is int()ed to build the auth filter — catch it here, not there."""
    env.setenv("OWNER_ID", "@cristian")

    with pytest.raises(SystemExit) as exc:
        config.validate()

    assert "OWNER_ID" in str(exc.value)
    assert "numeric" in str(exc.value)


def test_a_negative_owner_id_is_allowed(env):
    """Telegram group IDs are negative; don't reject a valid one."""
    env.setenv("OWNER_ID", "-100123456")

    config.validate()          # must not raise


def test_owner_id_problems_are_reported_alongside_missing_vars(env):
    env.setenv("OWNER_ID", "not-a-number")
    env.delenv("NOTION_KEY")

    with pytest.raises(SystemExit) as exc:
        config.validate()

    message = str(exc.value)
    assert "NOTION_KEY" in message
    assert "numeric" in message


# ─── CONTRACT ──────────────────────────────────────────────────────────────────

def test_required_and_optional_do_not_overlap():
    assert set(config.REQUIRED_ENV) & set(config.OPTIONAL_ENV) == set()


def test_every_declared_var_has_a_purpose():
    """The descriptions are what the startup error and the not-set warnings show."""
    for name, purpose in {**config.REQUIRED_ENV, **config.OPTIONAL_ENV}.items():
        assert purpose.strip(), f"{name} has no description"


# ─── A BLANK OPTIONAL VARIABLE IS AN UNSET ONE ─────────────────────────────────
# validate() always said so — it warns "X is not set" for a blank X — while the
# code reading X took the blank literally: `float(os.environ.get(X, "300"))` is
# float("") the moment X exists and is empty, which a `.env` line `BUDGET_CEILING=`
# and a Railway variable created without a value both produce. David died at
# import, before validate() could say a word. GOOGLE_CALENDAR_ID= quietly swapped
# 'primary' for "" instead, which is worse. Two definitions of "unset" was the
# bug; config.env_or is the one that is left.

REPO = pathlib.Path(__file__).resolve().parent.parent

# Every optional variable read with a default, and the expression that shows the
# value it resolved to. The ones without a default (IDs, keys) already treat a
# blank as unset, because their readers test truthiness.
DEFAULTED = {
    "BUDGET_CEILING":             "config.BUDGET_CEILING",
    "ANTHROPIC_MAX_TOKENS":       "config.ANTHROPIC_MAX_TOKENS",
    "ANTHROPIC_DAILY_BUDGET_USD": "config.ANTHROPIC_DAILY_BUDGET_USD",
    "ANTHROPIC_SPEND_FILE":       "clients.anthropic_client.SPEND_FILE",
    "GOOGLE_CALENDAR_ID":         "clients.calendar_client.CALENDAR_ID",
}


def _resolved_with(overrides: dict) -> dict:
    """What a FRESH interpreter resolves each defaulted variable to.

    A subprocess because every one of these is read at import: in this process
    they were frozen when conftest's environment loaded, and importlib.reload
    would leave every module that imported the old value still holding it.
    """
    env = {key: value for key, value in os.environ.items() if key not in DEFAULTED}
    env.update(overrides)
    probe = ("import json, config, clients.anthropic_client, clients.calendar_client;"
             f"print(json.dumps({{{', '.join(f'{k!r}: {v}' for k, v in DEFAULTED.items())}}}))")
    out = subprocess.run([sys.executable, "-c", probe],
                         cwd=REPO, capture_output=True, text=True, env=env)
    assert out.returncode == 0, f"David does not even import:\n{out.stderr}"
    return json.loads(out.stdout)


@pytest.mark.parametrize("blank", ["", "   "], ids=["empty", "whitespace"])
def test_a_blank_optional_variable_behaves_exactly_like_an_unset_one(blank):
    unset = _resolved_with({})

    assert _resolved_with({name: blank for name in DEFAULTED}) == unset


def test_a_set_optional_variable_still_wins():
    """The other half — a fix that ignored the variable entirely would pass the
    blank test above."""
    resolved = _resolved_with({"BUDGET_CEILING": "450", "GOOGLE_CALENDAR_ID": " cal@x "})

    assert resolved["BUDGET_CEILING"] == 450.0
    assert resolved["GOOGLE_CALENDAR_ID"] == "cal@x"


def test_env_or(monkeypatch):
    monkeypatch.setenv("DAVID_PROBE", "  value  ")
    assert config.env_or("DAVID_PROBE", "default") == "value"

    monkeypatch.setenv("DAVID_PROBE", " \t ")
    assert config.env_or("DAVID_PROBE", "default") == "default"

    monkeypatch.delenv("DAVID_PROBE")
    assert config.env_or("DAVID_PROBE", "default") == "default"
    assert config.env_or("DAVID_PROBE") == ""


def test_a_blank_optional_variable_is_reported_as_not_set(env, caplog):
    env.setenv("BUDGET_CEILING", "   ")

    with caplog.at_level(logging.WARNING):
        config.validate()

    assert "BUDGET_CEILING is not set" in caplog.text


# The scan: a default passed to os.environ.get is the shape of the bug, because
# .get() only falls back when the key is ABSENT. So the only call allowed to
# pass one is inside env_or itself.

def _defaulted_env_reads(source: str) -> list:
    """(line, code) for every os.environ.get / os.getenv given a default."""
    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_get = (isinstance(func, ast.Attribute) and func.attr == "get"
                  and ast.unparse(func.value) in {"os.environ", "environ"})
        is_getenv = ast.unparse(func) in {"os.getenv", "getenv"}
        if (is_get or is_getenv) and (len(node.args) > 1 or node.keywords):
            found.append((node.lineno, ast.unparse(node)))
    return found


def _env_or_body() -> set:
    tree = ast.parse((REPO / "config.py").read_text(encoding="utf-8"))
    home = next((node for node in tree.body
                 if isinstance(node, ast.FunctionDef) and node.name == "env_or"), None)
    return set(range(home.lineno, home.end_lineno + 1)) if home else set()


def scanned_files() -> list:
    """The root modules and the same packages every other source scan walks."""
    files = sorted(REPO.glob("*.py"))
    for package in ("bot", "clients", "proactive", "services"):
        files += sorted((REPO / package).rglob("*.py"))
    return files


def test_no_environment_read_passes_its_own_default():
    offences = []
    for path in scanned_files():
        exempt = _env_or_body() if path.name == "config.py" else set()
        for line, code in _defaulted_env_reads(path.read_text(encoding="utf-8")):
            if line not in exempt:
                offences.append(f"{path.relative_to(REPO)}:{line}: {code}")

    assert not offences, (
        "a default given to os.environ.get only applies when the variable is "
        "ABSENT — a blank one gets the blank. Use config.env_or:\n  "
        + "\n  ".join(offences))


@pytest.mark.parametrize("code", [
    'float(os.environ.get("BUDGET_CEILING", "300"))',
    'os.environ.get("X", default="y")',
    'os.getenv("X", "y")',
    'environ.get("X", "y")',
])
def test_the_scan_catches_each_shape(code):
    assert _defaulted_env_reads(code), f"the scan misses {code!r}"


@pytest.mark.parametrize("code", [
    'os.environ.get("X")',
    'os.getenv("X")',
    'config.get("X", "y")',
    'env_or("X", "y")',
])
def test_the_scan_leaves_the_rest_alone(code):
    assert not _defaulted_env_reads(code)


def test_the_scan_is_not_reading_nothing():
    """It must see the repo's environment reads and the one exemption, or a
    green run means nothing."""
    names = {path.relative_to(REPO).as_posix() for path in scanned_files()}
    for package in ("bot", "clients", "proactive", "services"):
        assert any(name.startswith(f"{package}/") for name in names)

    one_argument_reads = sum(
        ast.unparse(node).startswith("os.environ.get(")
        for path in scanned_files()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call))
    assert one_argument_reads >= 5, "the scan no longer sees the repo's env reads"
    assert _defaulted_env_reads((REPO / "config.py").read_text(encoding="utf-8")), (
        "env_or must itself pass a default to os.environ.get — if it no longer "
        "does, the exemption is guarding nothing")


# ─── A NAME READ FROM THE ENVIRONMENT IS USED ─────────────────────────────────
#
# `david.py` read DATABASE_ID, LEARN_ID, DIET_ID, BRAIN_ID and FINANCE_ID at
# import and nothing ever used the names; `services/implement.py` did the same
# with BRAIN_ID. Each read like live configuration — DATABASE_ID stayed for
# months as "unexplained, so it stays" — and a dead read is how a variable ends
# up set on the server, documented and maintained for nobody. Ruff does not flag
# an unused module-level name, so this does.

def _environment_names(source: str) -> list:
    """(line, name) for every module-level name bound from the environment."""
    found = []
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)):
            continue
        for inner in ast.walk(node.value):
            code = ast.unparse(inner)
            reads = (isinstance(inner, ast.Call) and code.startswith(
                         ("os.environ.get(", "os.getenv(", "env_or(", "config.env_or("))
                     or isinstance(inner, ast.Subscript) and ast.unparse(inner.value) == "os.environ")
            if reads:
                found.append((node.lineno, node.targets[0].id))
                break
    return found


def unread_environment_names(sources: dict) -> list:
    """Offences: a name bound from the environment that no production code reads.

    Read means: used by name in its own module, or reached from any module as
    `something.NAME` or `from … import NAME`. Tests do not count — a name only a
    test reads configures nothing.
    """
    trees = {path: ast.parse(source) for path, source in sources.items()}
    offences = []
    for home, source in sorted(sources.items()):
        for line, name in _environment_names(source):
            used = any(
                (isinstance(node, ast.Name) and node.id == name
                 and isinstance(node.ctx, ast.Load) and path == home)
                or (isinstance(node, ast.Attribute) and node.attr == name)
                or (isinstance(node, ast.ImportFrom) and any(a.name == name for a in node.names))
                for path, tree in trees.items() for node in ast.walk(tree))
            if not used:
                offences.append(f"{home}:{line}: {name} is read from the environment and never used")
    return offences


def production_sources() -> dict:
    return {path.relative_to(REPO).as_posix(): path.read_text(encoding="utf-8")
            for path in scanned_files()}


def test_every_name_read_from_the_environment_is_used():
    offences = unread_environment_names(production_sources())
    assert not offences, (
        "a module-level name is bound from the environment and nothing reads it — "
        "delete the read, or the variable is maintained for nobody:\n  "
        + "\n  ".join(offences))


def test_a_dead_environment_read_is_caught():
    """The real one, put back in memory: the read david.py carried for months."""
    sources = production_sources()
    marker = 'OWNER_ID = os.environ.get("OWNER_ID")\n'
    assert sources["david.py"].count(marker) == 1
    sources["david.py"] = sources["david.py"].replace(
        marker, marker + 'DATABASE_ID = os.environ.get("DATABASE_ID")\n')

    offences = unread_environment_names(sources)

    assert len(offences) == 1
    assert offences[0].startswith("david.py:") and "DATABASE_ID" in offences[0]


@pytest.mark.parametrize("code", [
    'X = os.environ.get("X")',
    'X = os.getenv("X")',
    'X = os.environ["X"]',
    'X = float(env_or("X", "1"))',
    'X = config.env_or("X")',
])
def test_the_unread_scan_sees_each_way_of_reading(code):
    assert unread_environment_names({"module.py": code}) == [
        "module.py:1: X is read from the environment and never used"]


@pytest.mark.parametrize("sources", [
    {"module.py": 'X = os.environ.get("X")\n\ndef f():\n    return X\n'},
    {"module.py": 'X = os.environ.get("X")\n', "other.py": "import module\nprint(module.X)\n"},
    {"module.py": 'X = os.environ.get("X")\n', "other.py": "from module import X\n"},
    {"module.py": 'X = "a constant"\n'},
], ids=["used-in-its-module", "read-as-an-attribute", "imported-by-name", "not-from-the-environment"])
def test_the_unread_scan_leaves_a_used_name_alone(sources):
    assert unread_environment_names(sources) == []


def test_the_unread_scan_is_not_reading_nothing():
    names = [name for source in production_sources().values() for _, name in _environment_names(source)]
    assert len(names) >= 20, "the scan no longer sees the repo's environment names"
    assert "TELEGRAM_TOKEN" in names and "EXPENSES_ID" in names
