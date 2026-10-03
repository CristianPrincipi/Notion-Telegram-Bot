"""The review workflow can speak, and says how it ended — checked from the file.

`claude-review` ran 53 times and never posted a comment: its prompt lacked
`--comment`, and the action was never told to install the inline-comment tool.
Nothing failed, because a mute review is green. Then 5 of 12 runs turned out to
stop in ~20 seconds without reviewing, and nothing said why either: the review
had started its agents in the background and ended its turn, and the action stops
reading at the first result.

These tests read the REAL workflow file. They hold the things it needs to be able
to comment, and they run the "Show what the review concluded" step's own script —
cut out of the workflow text, not copied here — against execution files built by
hand. The shape of those files is the action's: a JSON list of SDK messages, the
last `result` one carrying `result` and `permission_denials`
(`base-action/src/run-claude-sdk.ts` at the pinned SHA).

The can-it-fail tests take one thing out of the real workflow in memory and assert
the check names it.
"""

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "claude-code-review.yml"

INLINE_COMMENT_TOOL = "mcp__github_inline_comment__create_inline_comment"
NO_BACKGROUND = "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"
# The longest review that finished and was right, in minutes. A limit at or
# below it would cancel a review that was working.
SLOWEST_GOOD_REVIEW = 30


def read_workflow() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def uncommented(text: str) -> str:
    """The workflow without its comment lines: a rule quoted in a comment is not a setting."""
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def review_problems(text: str) -> list[str]:
    """Everything that would leave the review unable to report, as sentences."""
    problems = []
    live = uncommented(text)

    prompts = re.findall(r"^\s*prompt: '(.*)'\s*$", live, flags=re.MULTILINE)
    if len(prompts) != 1:
        problems.append(f"expected one prompt, found {len(prompts)}")
    elif not prompts[0].endswith(" --comment"):
        problems.append("the prompt does not end in --comment: the plugin posts nothing without it")

    tools = [
        tool.strip()
        for quoted in re.findall(r'--allowedTools "([^"]*)"', live)
        for tool in quoted.split(",")
    ]
    if INLINE_COMMENT_TOOL not in tools:
        problems.append(
            f"--allowedTools does not name {INLINE_COMMENT_TOOL}: "
            "the action does not install the inline-comment server without it"
        )

    if not re.search(rf'"{NO_BACKGROUND}": "1"', live):
        problems.append(
            f"settings does not set {NO_BACKGROUND} to 1: an agent started in the "
            "background outlives the run, and the review ends having read nothing"
        )

    triggers = re.findall(r"^\s*types: \[(.*)\]\s*$", live, flags=re.MULTILINE)
    if len(triggers) != 1:
        problems.append(f"expected one pull_request types list, found {len(triggers)}")
    else:
        types = {name.strip() for name in triggers[0].split(",")}
        if "synchronize" in types:
            problems.append(
                "the review triggers on synchronize: every push starts a full review, "
                "and the review is meant to run once per pull request"
            )
        if "opened" not in types:
            problems.append("the review does not trigger on opened: no pull request is reviewed at all")

    limits = re.findall(r"^\s*timeout-minutes: (\d+)\s*$", live, flags=re.MULTILINE)
    if len(limits) != 1:
        problems.append(
            f"expected one timeout-minutes, found {len(limits)}: "
            "without a limit a hung review runs for six hours"
        )
    elif int(limits[0]) <= SLOWEST_GOOD_REVIEW:
        problems.append(
            f"timeout-minutes is {limits[0]}: a review has taken "
            f"{SLOWEST_GOOD_REVIEW} minutes and posted a correct finding"
        )

    # The verdict step reads another step's output by id. A renamed id leaves it
    # reading nothing, and "No execution file" on every run looks like a skip.
    step_ids = set(re.findall(r"^\s*id: (\S+)\s*$", live, flags=re.MULTILINE))
    read_from = re.findall(r"EXECUTION_FILE: \$\{\{ steps\.([\w-]+)\.outputs\.execution_file \}\}", live)
    if len(read_from) != 1:
        problems.append(f"expected one EXECUTION_FILE, found {len(read_from)}")
    elif read_from[0] not in step_ids:
        problems.append(f"EXECUTION_FILE reads step {read_from[0]!r}, and no step has that id")

    return problems


def verdict_script(text: str) -> str:
    """The Python the verdict step runs, exactly as the workflow holds it."""
    match = re.search(r"python3 - <<'PY'\n(.*?)\n\s*PY\n", text + "\n", flags=re.DOTALL)
    assert match, "the verdict step's script was not found in the workflow"
    return textwrap.dedent(match.group(1))


def run_verdict(execution_file: str) -> subprocess.CompletedProcess:
    """Run the step's script the way the workflow does: on stdin, the path in the environment."""
    return subprocess.run(
        [sys.executable, "-"],
        input=verdict_script(read_workflow()),
        env={"EXECUTION_FILE": execution_file},
        capture_output=True,
        text=True,
        timeout=30,
    )


def execution_file(tmp_path: Path, messages) -> str:
    path = tmp_path / "claude-execution-output.json"
    path.write_text(json.dumps(messages), encoding="utf-8")
    return str(path)


def result_message(**overrides) -> dict:
    message = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 6,
        "result": "Stopping: the pull request does not need a review.",
        "permission_denials": [],
    }
    message.update(overrides)
    return message


# ─── The workflow can comment ────────────────────────────────────────────────


def test_the_review_can_post_what_it_finds():
    assert review_problems(read_workflow()) == []


def test_a_prompt_without_comment_is_caught():
    text = read_workflow()
    assert " --comment'" in text
    problems = review_problems(text.replace(" --comment'", "'"))
    assert any("--comment" in problem for problem in problems), problems


def test_a_missing_inline_comment_tool_is_caught():
    text = read_workflow()
    assert f",{INLINE_COMMENT_TOOL}" in uncommented(text)
    problems = review_problems(text.replace(f",{INLINE_COMMENT_TOOL}", ""))
    assert any(INLINE_COMMENT_TOOL in problem for problem in problems), problems


@pytest.mark.parametrize(
    "broken",
    [
        lambda text: text.replace(f'"{NO_BACKGROUND}": "1"', f'"{NO_BACKGROUND}": "0"'),
        lambda text: text.replace(f'                "{NO_BACKGROUND}": "1"\n', ""),
    ],
    ids=["set-to-0", "removed"],
)
def test_background_tasks_left_on_are_caught(broken):
    text = read_workflow()
    changed = broken(text)
    assert changed != text
    problems = review_problems(changed)
    assert any(NO_BACKGROUND in problem for problem in problems), problems


def test_a_review_on_every_push_is_caught():
    text = read_workflow()
    assert "types: [opened, " in text
    problems = review_problems(text.replace("types: [opened, ", "types: [opened, synchronize, "))
    assert any("synchronize" in problem for problem in problems), problems


def test_a_review_that_never_starts_is_caught():
    text = read_workflow()
    problems = review_problems(text.replace("types: [opened, ", "types: ["))
    assert any("no pull request is reviewed" in problem for problem in problems), problems


def test_a_review_job_with_no_time_limit_is_caught():
    text = read_workflow()
    limit = re.search(r"^\s*timeout-minutes: \d+\n", text, flags=re.MULTILINE)
    assert limit, "the review job's timeout-minutes was not found"
    problems = review_problems(text.replace(limit.group(0), ""))
    assert any("timeout-minutes" in problem for problem in problems), problems


@pytest.mark.parametrize("minutes", [10, 20, SLOWEST_GOOD_REVIEW])
def test_a_limit_that_would_cancel_a_working_review_is_caught(minutes):
    text = read_workflow()
    changed = re.sub(r"timeout-minutes: \d+", f"timeout-minutes: {minutes}", text)
    assert changed != text
    problems = review_problems(changed)
    assert any("correct finding" in problem for problem in problems), problems


def test_the_settings_block_is_json_the_action_can_read():
    """The action parses `settings` as JSON; a trailing comma would drop the whole block."""
    block = re.search(r"settings: \|\n((?:\s+.*\n)+?)\s+plugin_marketplaces:", read_workflow())
    assert block, "the settings block was not found in the workflow"
    assert json.loads(block.group(1)) == {"env": {NO_BACKGROUND: "1"}}


def test_a_commented_out_setting_does_not_count():
    """The workflow's own comment quotes `--comment`; only the live prompt may satisfy the check."""
    text = read_workflow()
    live_prompt = re.search(r"^(\s*)(prompt: '.*')\s*$", text, flags=re.MULTILINE)
    commented = text.replace(live_prompt.group(0), f"{live_prompt.group(1)}# {live_prompt.group(2)}")
    assert any("prompt" in problem for problem in review_problems(commented))


def test_a_renamed_step_id_is_caught():
    text = read_workflow()
    assert "id: claude-review" in text
    problems = review_problems(text.replace("id: claude-review", "id: review"))
    assert any("no step has that id" in problem for problem in problems), problems


# ─── The verdict step ────────────────────────────────────────────────────────


def test_the_verdict_prints_the_final_message_and_the_refused_calls(tmp_path):
    messages = [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5"},
        result_message(
            permission_denials=[
                {
                    "tool_name": "Bash",
                    "tool_use_id": "toolu_1",
                    "tool_input": {"command": "gh api repos/o/r/pulls/51"},
                }
            ]
        ),
    ]

    done = run_verdict(execution_file(tmp_path, messages))

    assert done.returncode == 0, done.stderr
    assert "Review ended: success, 6 turns." in done.stdout
    assert "Stopping: the pull request does not need a review." in done.stdout
    assert "Tool calls refused: 1" in done.stdout
    assert "- Bash: " in done.stdout
    assert "gh api repos/o/r/pulls/51" in done.stdout


def test_the_verdict_prints_nothing_else_from_the_transcript(tmp_path):
    """The reason the action hides its output: a tool's result can be anything."""
    messages = [
        {"type": "system", "subtype": "init", "model": "claude-sonnet-5", "tools": ["ONLY-IN-INIT"]},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ONLY-IN-AN-ASSISTANT-TURN"}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ONLY-IN-A-TOOL-RESULT"}]}},
        result_message(result="The verdict."),
    ]

    done = run_verdict(execution_file(tmp_path, messages))

    assert done.returncode == 0, done.stderr
    assert "The verdict." in done.stdout
    for hidden in ("ONLY-IN-INIT", "ONLY-IN-AN-ASSISTANT-TURN", "ONLY-IN-A-TOOL-RESULT"):
        assert hidden not in done.stdout


def test_the_verdict_reads_the_last_result_when_there_are_two(tmp_path):
    messages = [result_message(result="The first."), result_message(result="The last.")]

    done = run_verdict(execution_file(tmp_path, messages))

    assert "The last." in done.stdout
    assert "The first." not in done.stdout


def test_a_run_with_no_final_message_says_none(tmp_path):
    messages = [result_message(subtype="error_max_turns", result=None)]

    done = run_verdict(execution_file(tmp_path, messages))

    assert done.returncode == 0, done.stderr
    assert "Review ended: error_max_turns" in done.stdout
    assert "(none)" in done.stdout
    assert "Tool calls refused: 0" in done.stdout


def test_a_long_message_and_a_long_refused_call_are_cut_and_say_so(tmp_path):
    long_message = "m" * 4000 + "END-OF-MESSAGE"
    long_command = "c" * 1500 + "END-OF-COMMAND"
    messages = [
        result_message(
            result=long_message,
            permission_denials=[{"tool_name": "Bash", "tool_input": {"command": long_command}}],
        )
    ]

    done = run_verdict(execution_file(tmp_path, messages))

    assert done.returncode == 0, done.stderr
    assert "END-OF-MESSAGE" not in done.stdout
    assert "END-OF-COMMAND" not in done.stdout
    assert done.stdout.count("more characters]") == 2


@pytest.mark.parametrize("path", ["", "/nonexistent/claude-execution-output.json"])
def test_a_skipped_review_is_reported_as_one(path):
    """A pull request that edits a workflow is skipped: the action writes no file."""
    done = run_verdict(path)

    assert done.returncode == 0, done.stderr
    assert "No execution file" in done.stdout


@pytest.mark.parametrize(
    "content",
    [
        "{ not json",
        "[]",
        '[{"type": "assistant"}]',
        '{"type": "result"}',
    ],
    ids=["malformed", "empty-list", "no-result-message", "an-object-not-a-list"],
)
def test_the_verdict_step_never_fails_the_job(tmp_path, content):
    """It reports; the checks decide. A file it cannot read must not turn the job red."""
    path = tmp_path / "claude-execution-output.json"
    path.write_text(content, encoding="utf-8")

    done = run_verdict(str(path))

    assert done.returncode == 0, done.stderr
    assert "Could not read the review's result" in done.stdout
