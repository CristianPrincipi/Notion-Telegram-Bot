"""The output-token cap is ONE number, and the error that names it tells the truth.

WHAT WENT WRONG
---------------
`Learn video <a ~50-minute podcast>` came back:

    Claude hit the 4096-token output cap and the answer was cut off mid-object.
    Nothing was saved. Try a shorter source, or raise ANTHROPIC_MAX_TOKENS.

`config.ANTHROPIC_MAX_TOKENS` was 8192. `services.learn.summarize_with_claude`
passed a literal `max_tokens=4096`, which SHADOWED it at the one call site that
can plausibly hit an output cap — so the summarisation ran at half the configured
budget, and the fix the message named was one the failing call ignored.

Same family as SUMMARY_INPUT_CHARS and MANUAL_SOURCE_CHARS (one budget, two
numbers), with one difference worth stating: those two DRIFTED apart, while a
shadowed constant is wrong from the first commit and no end-to-end test can see
it — the run succeeds or fails identically either way, and the only visible
symptom is a summary shorter than it should have been.

SO THERE ARE THREE GUARDS HERE, AND THEY ARE INDEPENDENT
--------------------------------------------------------
  1. a SOURCE SCAN, because the bug is a literal at a call site and nothing that
     runs the code can distinguish "capped at 4096" from "capped at 8192" without
     knowing what the answer should have been;
  2. a BEHAVIOURAL check on the shipping `summarize_with_claude`, which goes red
     against the code as it shipped;
  3. an END-TO-END check that the environment variable the error message names
     actually reaches the API call — through a subprocess, because `config` reads
     `os.environ` at import and `complete_json`'s default argument is bound at
     def time, so no monkeypatch in this process can prove it.
"""

import ast
import inspect
import os
import pathlib
import subprocess
import sys

import pytest

from clients import anthropic_client
from config import ANTHROPIC_MAX_TOKENS, ANTHROPIC_ROUTE_MAX_TOKENS
from services import learn

REPO = pathlib.Path(__file__).resolve().parent.parent

# Same roots as tests/test_layering.py's scans, and for the same reason: a file
# that moves into a package must not drop silently out of the guard.
SCANNED = ["bot", "clients", "services", "proactive"]

# The constants a call site MAY name. Anything else passed to max_tokens is an
# anonymous number describing a budget that already has one. `max_tokens` itself
# is here because complete_json forwards its own parameter to the SDK.
ALLOWED_CAPS = {"ANTHROPIC_MAX_TOKENS", "ANTHROPIC_ROUTE_MAX_TOKENS", "max_tokens"}


def python_files():
    """Every .py file in every scanned package, plus the root modules."""
    files = [p for pkg in SCANNED for p in (REPO / pkg).rglob("*.py")]
    files += list(REPO.glob("*.py"))
    return sorted(files)


def _cap_arguments(tree: ast.AST):
    """(line, source-of-the-value) for every `max_tokens=...` passed at a call."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "max_tokens":
                yield node.lineno, ast.unparse(kw.value)


# ─── 1. THE SOURCE SCAN ────────────────────────────────────────────────────────

def test_no_call_site_passes_an_anonymous_output_cap():
    """A bare number here is a second budget that no test can see.

    `services/learn.py` passed 4096 while the constant said 8192, and every test
    in the suite stayed green: the request was well-formed, the response parsed,
    and the only difference was how much of the podcast survived.
    """
    offences = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for line, value in _cap_arguments(tree):
            if value not in ALLOWED_CAPS:
                offences.append(f"{path.relative_to(REPO)}:{line} -> max_tokens={value}")

    assert not offences, (
        "an output cap must be a named constant in config.py, not a literal:\n"
        + "\n".join(offences)
    )


def test_the_scan_is_looking_at_real_call_sites():
    """The positive control. A scan that matches nothing passes for free, which
    is how a guard rots into decoration — this one has to keep finding the
    routing calls that legitimately name their own smaller budget."""
    found = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found += [f"{path.relative_to(REPO)}:{line}" for line, _ in _cap_arguments(tree)]

    assert len(found) >= 2, f"the scan found almost nothing, so it proves nothing: {found}"


@pytest.mark.parametrize("source", [
    "complete_json(system, user, schema, max_tokens=4096)",
    "complete_json(system, user, schema, max_tokens=2048)",
    "x = client.messages.create(model=m, max_tokens=8192)",
])
def test_the_scan_can_actually_go_red(source):
    """The guard must be able to fail. Written by putting the shipped bug back."""
    offences = [v for _, v in _cap_arguments(ast.parse(source)) if v not in ALLOWED_CAPS]
    assert offences, f"the scan cannot see {source!r}, so it would not have caught the bug"


# ─── 2. THE CALL SITES THEMSELVES ──────────────────────────────────────────────

def _effective_cap(recorded):
    """What the API would be told, given the kwargs a call site actually passed.

    A call site that passes nothing inherits `complete_json`'s default, so
    "absent" and "equal to the constant" are the same answer — which is what
    makes this readable as one number rather than as a presence check.
    """
    default = inspect.signature(
        anthropic_client.complete_json).parameters["max_tokens"].default
    return recorded.get("max_tokens", default)


def test_the_learn_summary_runs_at_the_configured_cap(monkeypatch):
    """RED before the fix: this call passed 4096 against a constant of 8192.

    Driven through the shipping `summarize_with_claude` rather than by reading
    the constant, and the spy goes on `services.learn.complete_json` because that
    is the namespace the call resolves the name through — the SPY_HOMES rule in
    CLAUDE.md. A stub on `clients.anthropic_client.complete_json` would keep this
    green against nothing.
    """
    recorded = {}

    def spy(system, user, schema=None, **kwargs):
        recorded.update(kwargs)
        return {"title": "t", "tldr": "g"}, None

    monkeypatch.setattr(learn, "complete_json", spy)
    learn.summarize_with_claude("video", "a transcript", title="T", source="url")

    assert _effective_cap(recorded) == ANTHROPIC_MAX_TOKENS, (
        "the one call that can hit an output cap is not running at the configured one"
    )


def test_the_routing_calls_keep_their_own_smaller_budget():
    """Not everything should inherit the big cap. The routing calls answer with a
    list of section NAMES, so capping them separately is real money on the
    Implement path — the point is that the number has a NAME, not that it is
    shared."""
    assert ANTHROPIC_ROUTE_MAX_TOKENS < ANTHROPIC_MAX_TOKENS


# ─── 3. THE MESSAGE, AND THE LEVER IT NAMES ────────────────────────────────────

def test_the_environment_variable_the_error_names_reaches_the_api_call():
    """END TO END, in a subprocess, because it cannot be done in this one.

    `config` reads os.environ at import, and `complete_json`'s default argument
    is evaluated at def time — so by the time a test runs, both are frozen and a
    monkeypatch would only prove that monkeypatching works. A fresh interpreter
    is the only thing that exercises the real chain: env -> config -> the default
    the request is built with.

    Without this, "ANTHROPIC_MAX_TOKENS is now overridable" is an assertion about
    a line in config.py, not about the advice the failing message gives you.
    """
    probe = (
        "import inspect;"
        "from clients.anthropic_client import complete_json;"
        "print(inspect.signature(complete_json).parameters['max_tokens'].default)"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO, capture_output=True, text=True,
        env={**os.environ, "ANTHROPIC_MAX_TOKENS": "12345"},
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "12345", (
        "raising ANTHROPIC_MAX_TOKENS does not change the cap the API is called with "
        f"— which is exactly what the truncation error tells you to do. Got {out.stdout!r}"
    )
