"""`v` — which build is actually running, answered by the process itself.

WHY THIS EXISTS. `Agenda` and `Cancel` merged to `main` on 2026-08-15, passed CI,
and were absent from the running bot for three days: Railway's last deploy was
`f0209cc` from the day before, and the merges after it produced no deployment
record at all, so no build was ever attempted. CLAUDE.md's Deployment section
already warned that a failed deploy is silent because the previous version keeps
serving — what it had no answer for was how to NOTICE. Finding out took a local
verification, a startup smoke run, a `h` from the phone to see which commands the
process knew, and thirty GitHub deployment records read through the API. David
knew the answer the whole time and could not say it.

THE VALUES COME FROM THE ENVIRONMENT, NOT FROM THE REPO, and that is the single
decision this module is built around. A committed `VERSION` file — or one written
by a build step — records what the SOURCE says, which was never the thing in
doubt: the source was verified, green, and correct. What was in doubt is what the
PROCESS is running, and those two had silently drifted three days apart. Railway
injects the git variables below into the container it actually started, so
reading them answers the question that was actually asked. (Two lesser reasons:
the container has no `.git`, so shelling out to git is not available; and a
generated file would be one more thing that can go stale without saying so.)

MISSING IS NAMED, NEVER GUESSED. Absent a variable, the line says so and names
it. There is no placeholder that could be mistaken for a real build and no
fallback derived from the source tree — in the one command whose entire purpose
is to be trusted about what is deployed, a confident wrong answer is the whole
bug class this repo keeps re-learning, from the briefing that announced a clear
day during a calendar outage to the read failure that made a Notion section look
empty. An unknown build and a known one must not render alike.

PLAIN TEXT, and `run_version` takes no Markdown channel so it cannot drift into
one. See the comment on that function.
"""

import os
from datetime import datetime, timedelta

from clients.calendar_client import now_local

# Injected by Railway into the container it starts, for a service deployed from a
# GitHub repo. Named as constants because they appear twice each — once to read,
# once to name in the "not set" message — and a variable named in a message that
# does not match the one being read is a lie that reads like a diagnosis.
#
# THEY ARE DELIBERATELY NOT IN config.REQUIRED_ENV/OPTIONAL_ENV. That contract is
# what the OWNER sets, and config.validate() warns about anything in it that is
# unset — so listing these would print "set RAILWAY_GIT_COMMIT_SHA" on every
# local run, advice that is wrong about a variable you must not set by hand. The
# README documents them as platform-provided instead.
SHA_ENV     = "RAILWAY_GIT_COMMIT_SHA"
BRANCH_ENV  = "RAILWAY_GIT_BRANCH"
MESSAGE_ENV = "RAILWAY_GIT_COMMIT_MESSAGE"
DEPLOY_ENV  = "RAILWAY_DEPLOYMENT_ID"

# Stamped at import, which is process start: this module is imported from the
# registry in david.py, so the two happen within milliseconds of each other.
#
# now_local(), never datetime.now() — the clock is a project decision and lives
# in clients/calendar_client.py. It matters here for the same reason it matters
# to the briefings: Railway runs UTC, and a start time an hour or two off the
# time on your phone is worse than none, because you would use it to decide
# whether a deploy you just triggered is the one running.
_STARTED_AT = now_local()

# How much of a SHA to show. Seven characters is what `git log --oneline` prints,
# resolves unambiguously with `git show`/`git log`, and works in a GitHub URL.
# The full forty wrap onto three lines on a phone, which is where this reply is
# read, and buy nothing you would do differently.
_SHA_CHARS = 7


def _value(name: str) -> str | None:
    """The variable's value, or None if it is unset OR empty.

    Empty is treated as absent because a platform that injects its variables can
    inject them blank, and `""` formatted into the report renders as a label with
    nothing after it — which looks like a value David failed to print rather than
    one it never had. config.validate() strips for the same reason.
    """
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else None


def _unknown(name: str) -> str:
    """The one wording for a value David does not have.

    One function rather than an f-string per field, so every missing value reads
    identically and none of them can be quietly softened into something that
    scans like a real answer.
    """
    return f"unknown — {name} is not set"


def format_uptime(delta: timedelta) -> str:
    """A duration as 'd h m', dropping the units that are zero from the left.

    Coarse on purpose: this answers "is this the build I just deployed, or has it
    been up all week?", and seconds cannot help with that. Minutes are kept at
    every scale because the interesting case is a deploy from moments ago.
    """
    minutes = int(delta.total_seconds() // 60)
    if minutes < 0:
        # Only reachable if the clock moved backwards under us. Reported rather
        # than rendered as a plausible small number.
        return "unknown — the clock moved backwards since start-up"
    days, minutes = divmod(minutes, 60 * 24)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def build_version_report(started_at: datetime | None = None) -> str:
    """The whole reply, as text. Pure — no I/O, no Telegram, no clock but ours.

    EVERY FIELD IS REPORTED INDEPENDENTLY, one per line, and that is a shape
    decision rather than laziness: a combined line ("b09739d (main)") has to
    invent a rendering for each way its parts can be missing, and the readings
    multiply. One line each means one rule each — a value, or `_unknown`.

    `started_at` is a parameter with a module-level default so a test can pin it
    without reaching into module state, while production never passes it.
    """
    started = started_at or _STARTED_AT

    sha = _value(SHA_ENV)
    branch = _value(BRANCH_ENV)
    message = _value(MESSAGE_ENV)
    deploy = _value(DEPLOY_ENV)

    lines = ["🤖 David"]
    lines.append(f"commit   {sha[:_SHA_CHARS] if sha else _unknown(SHA_ENV)}")
    lines.append(f"branch   {branch or _unknown(BRANCH_ENV)}")

    # FIRST LINE ONLY. A commit message in this repo carries a long body — the
    # reasoning is the point of them — and the whole thing would bury the four
    # facts above it. The subject is the identity; the body is in git, where you
    # have the SHA to find it with. Deliberately not a character cap: a number
    # here would be one more unexplained literal, and the subject line is
    # already bounded by convention.
    lines.append(f"message  {message.splitlines()[0] if message else _unknown(MESSAGE_ENV)}")

    # IN FULL, unlike the SHA. Its only use is being matched against a row in the
    # Railway dashboard, which is a copy-paste, not a read.
    lines.append(f"deploy   {deploy or _unknown(DEPLOY_ENV)}")

    lines.append(f"up       {started.strftime('%Y-%m-%d %H:%M')}  "
                 f"({format_uptime(now_local() - started)})")
    return "\n".join(lines)


async def run_version(*, notify) -> None:
    """Report the running build.

    NO `notify_md`, AND THAT IS THE POINT OF THE SIGNATURE. The reply interpolates
    a commit MESSAGE — arbitrary text, and in this repo one that regularly
    contains backticks, asterisks and underscores. Markdown v1 has no literal
    asterisk and ignores backslash escapes inside a code span, so escaping cannot
    save it: David's own commit subjects are exactly the input that would make
    Telegram reject the whole message. That puts `v` in the same family as
    notify_error / on_error / scheduler._report_error — a diagnostic that has to
    survive the ugly input it exists to report.

    Every other service takes `notify_md=None` and defaults it, so this one is
    the exception, and it is enforced rather than commented: there is no channel
    here to send Markdown down, so "fixing" this into Markdown means changing the
    signature and the two tests that assert it.

    No `asyncio.to_thread`: reading four environment variables and formatting a
    string does not touch the network or the disk, so there is nothing to offload
    (and the async discipline in CLAUDE.md is about blocking calls, not about
    every function being wrapped).
    """
    await notify(build_version_report())
