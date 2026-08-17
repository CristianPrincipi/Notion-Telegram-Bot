# Plan: `v` — which build is actually running

_Last updated: 2026-08-17_

Branch: `version-command`, off `main` at `b09739d`. Baseline: **1048 passed**,
`ruff check .` clean.

Supersedes the M6 plan, which is finished except for one step carried forward
below: M6 merged as `b09739d` on 2026-08-15 and **has never been deployed**.
Railway's last deploy is `f0209cc` (2026-08-14 10:55, PR #31); the merges of
#32 and #33 produced no deployment record at all, so no build was attempted.
That is what this command exists to make visible in one message.

## The problem it solves

`Agenda` and `Cancel` were on `main`, green in CI, and absent from the running
bot for three days. Finding that out took: a full local verification, a startup
smoke run, a `h` from the phone, and finally 30 GitHub deployment records read
through the API. David could not answer "what are you running?" because nothing
in the process knew.

## Decisions taken before writing code

**1. The version is read from the ENVIRONMENT, not from a file in the repo.**
A committed `VERSION` file, or one written by a build step, records what the
SOURCE says — which was never in doubt. What was in doubt is what the PROCESS
is running, and those are exactly the two things that had drifted apart. Railway
injects `RAILWAY_GIT_COMMIT_SHA`, `RAILWAY_GIT_BRANCH`,
`RAILWAY_GIT_COMMIT_MESSAGE` and `RAILWAY_DEPLOYMENT_ID` into the container it
actually started, so reading them answers the right question. Two lesser
reasons: the container has no `.git`, so shelling out to git is not available,
and a generated file would be a second thing that can go stale silently.

**2. Missing is reported as missing.** If the git variables are absent — a local
run, or Railway changing their names — `v` names the variable it could not read.
It must never print a plausible-looking placeholder, and must never fall back to
anything derived from the source tree: that is the same class of bug as the
briefing that announced a clear day during a calendar outage, in the one command
whose entire purpose is to be trusted about what is deployed. An unknown build
and a known one cannot render alike.

**3. The reply goes out PLAIN.** It interpolates a commit MESSAGE — arbitrary
text, frequently containing `*` or backticks, and in this repo's case containing
things like `` `Agenda` `` — and Markdown v1 ignores backslash escapes inside a
code span, so escaping cannot save it. This puts `v` in the same family as
`notify_error` / `on_error` / `_report_error`: a diagnostic that must survive the
ugly input it exists to report. It uses `notify`, never `notify_md`, and carries
a comment saying so, so it is not "fixed" into Markdown later.

**4. The Railway variables do NOT join `REQUIRED_ENV`/`OPTIONAL_ENV`.** That
contract is what the OWNER sets, and `config.validate()` warns about anything in
it that is unset. Listing platform-injected variables would print "set
`RAILWAY_GIT_COMMIT_SHA`" on every local run — advice that is wrong, about a
variable you must not set by hand — and would break `test_config_validate`'s
empty-warning assertion unless `conftest` faked them. They are documented in the
README as platform-provided, in their own short section rather than in the env
table.

**5. Uptime is included, and it comes from `now_local()`.** A SHA alone cannot
tell a fresh deploy from a container restart of the same build, which is the
distinction the deployment records could not settle either. Process start is
stamped at import; the clock is `clients.calendar_client.now_local()`, never
`datetime.now()`, per the project rule.

**6. `services/version.py` + `bot/version.py`, mirroring `month`.** The report
builder is a pure function returning text, so it is testable with a list's
`append` and no `Update` at all — which is the property the layering split
exists to produce.

## Milestone 1: the service

- [x] `services/version.py` — reads its own `os.environ` (per the config rule),
      stamps `_STARTED_AT` at import from `now_local()`.
- [x] `build_version_report() -> str` — pure, no I/O, returns the whole reply.
      Takes `started_at` with a module-level default so a test can pin it
      without reaching into module state.
- [x] Fields: short SHA, branch, commit subject, deployment ID (in full,
      because its job is to be matched against the Railway dashboard), and
      "up since / for". One line each — see the Changelog on the full SHA.
- [x] Each absent variable is named in the output; no placeholder that could be
      mistaken for a real value. Empty counts as absent.
- [x] `run_version(*, notify)` — the service, taking only the plain channel,
      which is how the plain-only decision is enforced by the signature rather
      than by remembering.

## Milestone 2: the adapter and the registry

- [x] `bot/version.py` — bind the pair, pass only `notify`, comment why.
- [x] `david.py`: `Command(name="v", pattern=re.compile(r"v|version", re.I))`,
      inline, positioned next to `h` as the other meta command.
- [x] No collision: `test_no_input_can_be_claimed_by_two_commands` stays green,
      and `v` is not shadowed — the three `v` rows route to `handle_version`.

## Milestone 3: tests

- [x] `tests/test_version.py`, 20 tests:
      - full environment → every field appears, SHA shortened to seven;
      - **git variables absent → says which, and the output cannot be mistaken
        for a real build** (the guard from decision 2), plus the empty-string
        case and the one-missing-of-four case;
      - a commit message containing `` ` ``, `*` and `_` survives verbatim, with
        no escaping and no `parse_mode` — the test that fails if someone sends
        this reply as Markdown;
      - `inspect.signature(run_version)` has no `notify_md` at all;
      - uptime derives from `now_local()`, asserted by monkeypatching it, plus a
        parametrised `format_uptime` table and the backwards-clock case;
      - the service driven with one async collector — no bot, no `Update`.
      - An **autouse fixture unsets the four variables**, for the reason the
        title-property cache needed one: these are real variable names, and a
        machine with one exported would satisfy the "not set" tests without them
        being unset.
- [x] `tests/test_router.py`: `import bot.version`, a `SPY_TARGETS` +
      `SPY_HOMES` entry for `handle_version`, and rows for `v`, `version`, `V`,
      plus negatives (`v please`, `versions`) that must fall through.
- [x] Full suite green (**1074 passed**, up from 1048), `ruff check .` clean.
- [x] **Guard-revert pass** — every guard reverted one at a time, table below.

## Milestone 4: docs

- [x] `README.md` — command table row (`v` only; `version` is an alias and the
      table's first column is asserted to hold command NAMES, which is how
      `help`/`aiuto` already live in prose), and a "Which build is running"
      section naming the four platform-provided variables and why they are not
      in the env table.
- [x] `CLAUDE.md` — module map rows for both new files; the Deployment section
      gained "a merge is not a deploy, and `v` is how you tell", the `v`/`h`
      split of duties, and the `gh api …/deployments` check that tells "no build
      was attempted" from "the build failed".

## Guard-revert pass

Each guard was removed, a named test went red, and the guard was restored. A
guard that cannot go red reads like protection and is not.

| Reverted to | Test that went red |
| --- | --- |
| `_unknown()` returns `"0000000"` — a placeholder that scans like a SHA | `test_an_absent_variable_is_named_rather_than_guessed`, `test_a_missing_build_cannot_be_mistaken_for_a_real_one`, `test_an_empty_variable_counts_as_absent`, `test_one_missing_variable_does_not_hide_the_others` (4) |
| `run_version(*, notify, notify_md=None)` sending down `notify_md` | `test_the_service_cannot_send_markdown_at_all` — and NOT the hostile-message test, which passes only `notify`. That is why the signature is the guard. |
| `datetime.now(tz)` in place of `now_local()` | `test_uptime_comes_from_now_local_and_not_the_system_clock`, `test_a_deployed_build_reports_its_commit_branch_and_deploy` |
| `cmd_version` bypassing `handle_version` (the `SPY_HOMES` check) | all three router rows: `v`, `version`, `V` |

## Milestone 5: ship

- [ ] `ruff check .` + full suite.
- [ ] Branch, commit, push, PR, CI green.
- [ ] Merge — needs your click; the classifier blocked `gh pr merge` last time.
- [ ] **Verified from Telegram**: `v` answers with a SHA. Not ticked from CI.

## Changelog

- **The full 40-character SHA is not printed, only the 7-character short form.**
  Milestone 1 originally said "short + full". Dropped while writing the report:
  seven characters is what `git log --oneline` prints, what `git show` and a
  GitHub URL both resolve, and the full forty wrap onto three lines on the phone
  this reply is read on. There is no follow-up action the long form enables. The
  deployment ID is still printed whole, because that one is copy-pasted into the
  Railway dashboard rather than read.
- **Fields are one per line rather than combined.** The mock in the plan message
  had `commit b09739d (main)`. A combined line needs a rendering for each way its
  parts can be missing and the readings multiply; one line each means one rule
  each — a value, or `_unknown`. `test_one_missing_variable_does_not_hide_the_others`
  is the test that would have caught the combined version being wrong.

## Open questions

- **`v` cannot prove anything until Railway deploys again, and shipping it does
  not fix the deploy.** It will merge into a `main` that Railway is currently
  ignoring, so its first useful output is the confirmation that the integration
  has been repaired — the dashboard fix comes first, or this lands and stays
  invisible exactly like M6 did. Worth being blunt about: this makes the next
  deploy verifiable, it does not make it happen.
- **PR #34 is still open and touches `PLAN.md`, which this plan has rewritten.**
  Two open PRs editing the same file will conflict. Recommendation: close #34 as
  superseded — its only content was the PLAN.md checkbox, and the deploy step it
  added is carried forward at the top of this file.
- Whether `v` should also print the command count or the registry names. Left
  out: `h` already lists the commands, and that is the check that found this
  bug. Adding a second, terser copy of the same information is a thing to keep
  in sync for no new answer.
