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
    """The descriptions are what the README table and the error message show."""
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
