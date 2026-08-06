"""config.validate() — the startup gate.

The point of this function is that a misconfigured deploy dies immediately with
a complete list, instead of running and failing hours later against whichever
command happened to hit Notion first. So the tests care about two things: that
it exits at all, and that ONE run names EVERY problem.
"""

import logging

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


def test_month_id_is_optional(env, caplog):
    """It is a first-boot SEED, not a live value.

    month.py resolves the current month page from Notion by title and caches the
    answer, so David starts and runs correctly with MONTH_ID unset. It was in
    REQUIRED_ENV anyway, which killed a deploy over a variable nothing reads —
    and invited the fix of pasting a stale page ID back in to silence the error.
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
