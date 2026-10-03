"""`v` — the command that says which build is running, and the two ways it could lie.

WHAT THIS LOCKS DOWN
--------------------
**1. An unknown build never renders like a known one.** This is the whole reason
the command exists, so it is the thing most worth a test. `Agenda` and `Cancel`
sat on `main` for three days while the deployed process did not have them, and
every signal available said "fine": CI green, tests green, the source correct. A
`v` that answered with a placeholder, or fell back to something derived from the
source tree, would join that list — it would be a confident wrong answer about
the one fact you asked for, which is the failure this repo keeps paying for (the
briefing that announced a clear day during an outage; the read error that made a
Notion section look empty and got it overwritten).

**2. The reply survives a commit message.** David's own commit subjects contain
backticks, asterisks and underscores — `` `Agenda` `` and `` `Cancel` `` are in
the message this command was written to report. Markdown v1 has no literal
asterisk and ignores backslash escapes inside a code span, so a Markdown `v`
would be rejected by Telegram on exactly the commits it is asked about. The
service therefore has NO `notify_md` parameter, and
`test_the_service_cannot_send_markdown_at_all` asserts the signature rather than
the behaviour, because a comment saying "keep this plain" is not a guard.

**3. The clock is the project's.** `now_local()`, never `datetime.now()` — Railway
runs UTC, and this is a value you read to decide whether the deploy you triggered
two minutes ago is the one running.
"""

import inspect
from datetime import timedelta

import pytest

from conftest import FakeUpdate, run, with_update
from services import version

FULL_SHA = "b09739d3c4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9"
SUBJECT  = "The calendar could be written to and never read or undone"


@pytest.fixture(autouse=True)
def clean_build_env(monkeypatch):
    """No Railway variables unless a test asks for them.

    AUTOUSE, and it clears rather than sets: these are real variable names, and a
    machine that happens to have one exported — or a future conftest that fakes
    them — would otherwise satisfy the "not set" tests without them being unset,
    which is the same class of bug as the process-lifetime caches that made
    schema-lookup tests pass without a lookup.
    """
    for name in (version.SHA_ENV, version.BRANCH_ENV,
                 version.MESSAGE_ENV, version.DEPLOY_ENV):
        monkeypatch.delenv(name, raising=False)


def deployed(monkeypatch, *, sha=FULL_SHA, branch="main", message=SUBJECT,
             deploy="4addf1d4-c147-4cc9-a004-9b62180709f4"):
    """The environment Railway injects into a container it deployed from GitHub."""
    for name, value in ((version.SHA_ENV, sha), (version.BRANCH_ENV, branch),
                        (version.MESSAGE_ENV, message), (version.DEPLOY_ENV, deploy)):
        if value is not None:
            monkeypatch.setenv(name, value)


def freeze(monkeypatch, moment):
    """Pin the service's clock. Patched on `version`, which is where it is called."""
    monkeypatch.setattr(version, "now_local", lambda: moment)


def start_and_now(monkeypatch, *, up_for):
    """A start time and a now, `up_for` apart. Returns the start."""
    from clients.calendar_client import TIMEZONE
    from datetime import datetime

    started = TIMEZONE.localize(datetime(2026, 8, 17, 16, 41))
    freeze(monkeypatch, started + up_for)
    return started


# ─── THE POINT OF THE COMMAND ──────────────────────────────────────────────────

def test_a_deployed_build_reports_its_commit_branch_and_deploy(monkeypatch):
    deployed(monkeypatch)
    started = start_and_now(monkeypatch, up_for=timedelta(hours=3, minutes=12))

    report = version.build_version_report(started)

    assert "b09739d" in report,                                     "short SHA missing"
    assert "main" in report,                                        "branch missing"
    assert SUBJECT in report,                                       "commit subject missing"
    assert "4addf1d4-c147-4cc9-a004-9b62180709f4" in report,        "deployment ID missing"
    assert "2026-08-17 16:41" in report,                            "start time missing"
    assert "3h 12m" in report,                                      "uptime missing"
    assert "unknown" not in report, (
        "nothing was missing from the environment, so nothing should be unknown")


def test_the_deployment_id_is_reported_in_full(monkeypatch):
    """Unlike the SHA. Its only use is matching a row in the Railway dashboard,
    which is a copy-paste — a shortened one would need re-reading from the UI."""
    deployed(monkeypatch, deploy="dep-0123456789abcdef")
    assert "dep-0123456789abcdef" in version.build_version_report(
        start_and_now(monkeypatch, up_for=timedelta(minutes=1)))


def test_the_sha_is_shortened_to_seven_characters(monkeypatch):
    """What `git log --oneline` prints, and what a GitHub URL accepts. The full
    forty wrap onto three lines on the phone this is read on."""
    deployed(monkeypatch)
    report = version.build_version_report(
        start_and_now(monkeypatch, up_for=timedelta(minutes=1)))
    assert "b09739d" in report
    assert FULL_SHA not in report, "the full 40-character SHA should not be printed"


# ─── AN UNKNOWN BUILD DOES NOT LOOK LIKE A KNOWN ONE ───────────────────────────

def test_an_absent_variable_is_named_rather_than_guessed(monkeypatch):
    """The guard this command lives or dies by.

    Every field is missing, so every line must SAY which variable it wanted. A
    placeholder here would be a confident answer about the one thing you asked,
    on a run where David does not know it.
    """
    started = start_and_now(monkeypatch, up_for=timedelta(minutes=4))

    report = version.build_version_report(started)

    for name in (version.SHA_ENV, version.BRANCH_ENV,
                 version.MESSAGE_ENV, version.DEPLOY_ENV):
        assert name in report, f"{name} is unset and the report does not say so"
    assert report.count("unknown") == 4, (
        "each of the four build facts should be reported unknown, once")


def test_a_missing_build_cannot_be_mistaken_for_a_real_one(monkeypatch):
    """The same guard from the reader's side.

    Not "does it contain the word unknown" but: is there anything on the commit
    line that could be copied into `git show` and taken for an answer. A
    placeholder SHA, a default branch name or an empty value would all pass the
    test above and fail this one.
    """
    started = start_and_now(monkeypatch, up_for=timedelta(minutes=4))

    commit_line = next(line for line in version.build_version_report(started).splitlines()
                       if line.startswith("commit"))

    assert commit_line == f"commit   unknown — {version.SHA_ENV} is not set"


def test_an_empty_variable_counts_as_absent(monkeypatch):
    """A platform that injects its own variables can inject them blank.

    `""` formatted into the report is a label with nothing after it, which reads
    as a value David failed to print rather than one it never had.
    """
    deployed(monkeypatch)
    monkeypatch.setenv(version.SHA_ENV, "   ")

    report = version.build_version_report(
        start_and_now(monkeypatch, up_for=timedelta(minutes=1)))

    assert f"unknown — {version.SHA_ENV} is not set" in report


def test_one_missing_variable_does_not_hide_the_others(monkeypatch):
    """Each field is reported independently — one line, one rule.

    A combined "b09739d (main)" line would have to invent a rendering for each
    way its halves can be missing, and the readings multiply.
    """
    deployed(monkeypatch, branch=None)

    report = version.build_version_report(
        start_and_now(monkeypatch, up_for=timedelta(minutes=1)))

    assert "b09739d" in report,                                  "the SHA is known and should print"
    assert SUBJECT in report,                                    "the subject is known and should print"
    assert f"unknown — {version.BRANCH_ENV} is not set" in report
    assert report.count("unknown") == 1,                         "only the branch was missing"


# ─── THE COMMIT MESSAGE IS ARBITRARY TEXT ──────────────────────────────────────

def test_only_the_subject_line_of_the_commit_message_is_shown(monkeypatch):
    """This repo's commit bodies are long — the reasoning is the point of them —
    and the body would bury the four facts above it. The SHA is right there to
    read the rest with."""
    deployed(monkeypatch, message=f"{SUBJECT}\n\nA long body explaining why.\n\nAnd more.")

    report = version.build_version_report(
        start_and_now(monkeypatch, up_for=timedelta(minutes=1)))

    assert SUBJECT in report
    assert "A long body" not in report
    assert len(report.splitlines()) == 6, (
        "the report is a fixed six lines; a multi-line commit body must not grow it")


def test_markdown_characters_in_a_commit_message_survive_verbatim(monkeypatch):
    """The test that fails if someone switches this reply to notify_md.

    These are not hypothetical characters: the commit this command was written to
    report is "The calendar could be written to and never read or undone", and
    the PR body around it is full of `Agenda` and `Cancel` in backticks. Sent as
    Markdown, a subject like this is rejected by Telegram outright — on exactly
    the commits you would be asking about.
    """
    hostile = "`Agenda` and *Cancel*: the _underscore_ case too"
    deployed(monkeypatch, message=hostile)
    update = FakeUpdate(text="v")

    run(version.run_version(notify=with_update(update)["notify"]))

    sent = update.message.reply_texts[0]
    assert hostile in sent, "the subject was altered on the way out"
    assert "\\" not in sent, "nothing should be escaped — this channel is plain"
    assert update.message.replies[0][1] == {}, (
        "no kwargs, so no parse_mode: Telegram must not try to parse this")


def test_the_service_cannot_send_markdown_at_all(monkeypatch):
    """A signature assertion, on purpose.

    Every other service takes `notify_md=None` and defaults it to `notify`. This
    one takes no such parameter, so keeping the reply plain is not a thing anyone
    has to remember — there is no channel to send Markdown down, and restoring
    one means changing this line and reading the comment that explains it.
    """
    params = inspect.signature(version.run_version).parameters

    assert "notify" in params
    assert "notify_md" not in params, (
        "run_version grew a Markdown channel — see why it must not have one")


# ─── THE CLOCK ─────────────────────────────────────────────────────────────────

def test_uptime_comes_from_now_local_and_not_the_system_clock(monkeypatch):
    """`now_local()`, never `datetime.now()` — the clock is a project decision.

    Same test `test_learn_nudge` carries for the same reason. It matters more here
    than in most places: Railway runs UTC, and a start time an hour off the time
    on your phone is worse than none, because you would use it to decide whether
    the deploy you just triggered is the one answering.
    """
    deployed(monkeypatch)
    started = start_and_now(monkeypatch, up_for=timedelta(days=2, hours=1, minutes=5))

    assert "2d 1h 5m" in version.build_version_report(started)


@pytest.mark.parametrize("delta, expected", [
    (timedelta(minutes=0),                      "0m"),
    (timedelta(minutes=7),                      "7m"),
    (timedelta(hours=1),                        "1h 0m"),
    (timedelta(hours=3, minutes=12),            "3h 12m"),
    (timedelta(days=1, hours=0, minutes=3),     "1d 0h 3m"),
    (timedelta(days=11, hours=23, minutes=59),  "11d 23h 59m"),
])
def test_uptime_drops_the_leading_units_that_are_zero(delta, expected):
    """Minutes always print, at every scale: the interesting case is a deploy
    from moments ago, and "0h" alone cannot tell you it just restarted."""
    assert version.format_uptime(delta) == expected


def test_a_backwards_clock_is_reported_rather_than_rendered(monkeypatch):
    """Only reachable if the clock moves under us. A negative duration formatted
    as a plausible small number would read as a fresh deploy."""
    assert "unknown" in version.format_uptime(timedelta(minutes=-5))


# ─── THE SERVICE, DRIVEN WITHOUT A BOT ─────────────────────────────────────────

def test_the_service_reports_through_notify_with_no_update_at_all(monkeypatch):
    """The property the layering split exists to produce: one async one-liner is a
    complete implementation of this service's whole interface — no bot, no
    `Update`, and nothing Markdown-shaped to bind."""
    deployed(monkeypatch)
    start_and_now(monkeypatch, up_for=timedelta(minutes=2))
    sent = []

    async def collect(text):
        sent.append(text)

    run(version.run_version(notify=collect))

    assert len(sent) == 1
    assert "b09739d" in sent[0]
