---
name: ship-feature
description: Ship the current branch as a pull request — check every change against CLAUDE.md's Documentation contract, update the docs and CHANGELOG it requires, run ruff and pytest, commit, push, open the PR from the fixed template, and wait for CI and the review. Stops before merging.
argument-hint: "[what the change is, in a few words]"
disable-model-invocation: true
---

# Ship the current branch

Turns finished work into a pull request that keeps the docs true. It pushes and
opens a PR, so it runs only when a person asks for it — and it **never merges**.

`$ARGUMENTS`, if given, is the person's one-line description of the change.

## 1. Check where you are

- Run `git branch --show-current`. On `main`, **stop**: CLAUDE.md forbids committing to
  `main`. Offer to create a branch.
- Run `git fetch origin main`, then `git status --porcelain`. Uncommitted changes
  are part of what ships; files that are not part of this change (scratch files,
  `.claude/settings.local.json`) are not — never stage those.
- If `PLAN.md` exists, read it: it says what this change is meant to do.

## 2. List what changed

```bash
git diff --name-status origin/main...HEAD
git diff origin/main...HEAD
git diff HEAD                              # changes not committed yet
git ls-files --others --exclude-standard   # NEW files — no diff shows these
```

Read the whole diff, not just the file names, and read every new file in full:
`git diff` never shows an untracked file, so a change that is mostly new files
looks almost empty. The classification in step 3 depends on what changed inside
each file.

## 3. Classify against the Documentation contract

Open CLAUDE.md's **Documentation contract** table and decide, row by row, whether
this change triggers it. Use the diff, not the branch name:

| Contract row | Look in the diff for |
| --- | --- |
| A command | a `Command(` added, removed, or with a changed `pattern`/`usage`/`notes` in `david.py`; a new handler in `bot/` |
| An environment variable | `REQUIRED_ENV` / `OPTIONAL_ENV` in `config.py`; any new `env_or(` or `os.environ.get(` |
| A Notion column, database or looked-up page | the `NOTION SCHEMA` section of `config.py`; a new `*_ID` database |
| A scheduled job or its time | `run_daily` in `proactive/scheduler.py` or `david.register_jobs`; `*_HOUR` / `*_MINUTE` / `*_DAY` in `config.py` |
| A setup step or service | a new package in `requirements.txt` that needs an account or key; a new module in `clients/` |
| A new way to fail | a new message a user can receive — `notify(...)` / `notify_md(...)` with `❌`, `⚠️` or a refusal |
| A rule or convention | `CLAUDE.md`; a new source-scan test; a new "only place" for something |
| A module added or moved | a new or renamed `.py` file under `bot/`, `services/`, `clients/` or `proactive/`, or at the root |
| Anything a user would notice | any change to what a command replies, does, accepts or costs |

**Show the person the result as a table before editing any doc:**

| Change | Contract row | Docs to update |
| --- | --- | --- |
| … | … | `docs/…` — or "not needed: <reason>" |

"Not needed" always gets a reason. A row you skip without one is a doc that drifts.

A row firing is a question, not a verdict. A new `❌` message that passes on an
error `docs/troubleshooting.md` already explains (a Notion 400/404, a calendar
share) needs no new entry — say which entry covers it. A failure the person could
cause or fix themselves does.

## 4. Update the docs

For each row that applies, edit the doc it names:

- Match the voice of the surrounding text. User docs (`docs/setup.md`,
  `features.md`, `configuration.md`, `troubleshooting.md`, `notion-schema.md`) talk
  to the person running David; `docs/design-notes.md` explains why; CLAUDE.md states
  rules in one or two lines.
- Quote messages and names from the code, never from memory.
- A rule goes in CLAUDE.md **and** its reason in `docs/design-notes.md`, under a
  heading with the same name.
- A Notion schema change cannot update the published Notion template from here.
  Say so under **Before you merge** in the PR.

## 5. CHANGELOG

If anything a user would notice changed, add a line under `## [Unreleased]` in
`CHANGELOG.md`, under `### Added`, `### Changed` or `### Fixed`, in plain words:
what is different for the person using David. Contributor-only changes (tests,
docs for developers, CI) get no entry.

## 6. Verify

```bash
ruff check .
pytest
```

- Both must pass. If either fails, **stop and report** — do not ship a red branch.
- **A bug fix needs a test that fails without it.** If the diff fixes a bug and
  adds no test that would have caught it, stop and say so. Show that it fails
  against the old code: either it was written first and run before the fix, or
  put the old file back (`git show origin/main:<path> > <path>`), run it, and
  restore.
- **A changed return shape breaks test doubles.** If a function now returns
  something different (a value became `(value, error)`, say), find every double
  with `grep -rn <name> tests/` and give it the new shape — a double must return
  what production returns, or the tests pass against a shape nothing produces.
- Check that every relative link you added or changed in Markdown points at a file
  and heading that exist.

## 7. Commit

Stage only the files that belong to this change, by name — never `git add -A`.
Commit in the repo's style (see `git log`):

- **Title:** what was wrong or what is new, in plain words — not "fix bug".
- **Body:** the cause, the fix, what the tests prove, the docs touched. Wrap at
  ~72 columns.
- End with the attribution trailer this session was given.

## 8. Push and open the PR

```bash
git push -u origin <branch>
gh pr create --base main --head <branch> --title "<the commit title>" --body-file <file>
```

Fill this template, keeping its headings. Drop an optional section rather than
leaving it empty.

```markdown
## What changed

<What a user (or contributor) sees differently, then why. For a bug: what went
wrong, the cause, the fix. Short paragraphs or bullets.>

## Docs touched

<Each doc and what changed in it, or "None: <reason>">

## Tests

<New or changed tests, what each proves, and that it failed before the fix.
Then: "N passed, ruff clean.">

## Before you merge          ← optional

<Anything only a person can do: a Railway variable, the Notion template, a
manual check.>

## Left out                  ← optional

<What was noticed and deliberately not done here, and why.>

<the PR attribution line this session was given>
```

## 9. Wait for CI and the review — then stop

```bash
gh pr checks <number> --watch
```

- **The review runs once, when the PR is opened** — which is why step 8 opens it
  only after everything is committed and pushed. A push afterwards is not
  reviewed: say so when you report a fix made after the review. Closing and
  reopening the PR runs it again; that spends a full review, so it is the
  person's call.
- **A real review takes minutes and costs dollars.** If `claude-review` passes in a
  few seconds, read its log: a PR that edits a file under `.github/workflows/` is
  never reviewed (the action skips itself, and GitHub shows the skip as a pass), and
  one that *fails* in seconds with no tokens spent means `CLAUDE_CODE_OAUTH_TOKEN`
  has stopped working. One that passes in under a minute, with a final message
  saying the PR does not need a review, stopped at the review's own first step —
  it happened to nine lines of docs. Report that PR as **not reviewed**.
- **The review's answer is its comment, not its green check.** Read both kinds:
  `gh pr view <number> --comments` for a summary and
  `gh api repos/{owner}/{repo}/pulls/<number>/comments` for the inline findings.
  If it left neither, it has reported nothing: read the review job's last step,
  "Show what the review concluded", and report its final message — never report
  that review as passed.
- **Check each finding before acting on it.** A finding is a claim: verify it
  against the code, and say when the line is right and the reason is not. A
  committable suggestion is not checked by being committable.
- Report: the PR link, CI, the review (real or skipped, and what its comments
  say), and anything under **Before you merge**.

**Do not merge.** Merging deploys to Railway; that decision belongs to the person.
