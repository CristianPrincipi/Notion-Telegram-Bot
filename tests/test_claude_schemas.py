"""Every schema sent to Claude is one structured output accepts, and every call
names its effort.

WHY THIS EXISTS
---------------
On Claude Sonnet 5.5 the answer's shape is enforced by the API through
`output_config.format` (structured output) instead of a forced tool call, which
the model rejects. Structured output is stricter about the schema itself than a
tool's `input_schema` ever was:

  - every object must say `additionalProperties: false`, nested ones included;
  - `minimum` / `maximum`, `minLength` / `maxLength` and array constraints beyond
    `minItems` 0 or 1 are not supported;
  - at most 24 optional (not `required`) properties per request.

A schema that breaks one of these is a 400 on EVERY call through it, and nothing
offline can see that except a check of the schema itself. The tool-use path
accepted all of it silently, so the six schemas David had were written without
any of these rules.

HOW THE SCHEMAS ARE FOUND
-------------------------
From the call sites, not from a list: every `complete_json(...)` in the scanned
packages is read with `ast`, and its schema argument is resolved to the
module-level constant it names. A new feature's schema is checked the moment it is
passed, and a schema built inline (which this check could not see) is refused.

EFFORT
------
Thinking bills as output, and Sonnet 5.5 thinks at `high` unless told otherwise.
The level is a named constant in config, like the output cap
(tests/test_output_cap.py), and section 3 drives the shipping call sites to show
which one each sends.
"""

import ast
import importlib
import inspect
import pathlib

import pytest

from clients import anthropic_client
from config import ANTHROPIC_EFFORT, ANTHROPIC_ROUTE_EFFORT
from services import implement, implement_diet, learn

REPO = pathlib.Path(__file__).resolve().parent.parent

# Same roots as the other source scans (tests/test_layering.py).
SCANNED = ["bot", "clients", "services", "proactive"]

MAX_OPTIONAL = 24

# Keywords structured output rejects with a 400. `minItems` is handled apart:
# 0 and 1 are allowed.
UNSUPPORTED = {
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "patternProperties",
}


def python_files():
    files = [p for pkg in SCANNED for p in (REPO / pkg).rglob("*.py")]
    files += list(REPO.glob("*.py"))
    return sorted(files)


def _module_name(path: pathlib.Path) -> str:
    return ".".join(path.relative_to(REPO).with_suffix("").parts)


def _is_complete_json(call: ast.Call) -> bool:
    f = call.func
    return ((isinstance(f, ast.Name) and f.id == "complete_json")
            or (isinstance(f, ast.Attribute) and f.attr == "complete_json"))


def _schema_arg(call: ast.Call):
    if len(call.args) >= 3:
        return call.args[2]
    for kw in call.keywords:
        if kw.arg == "schema":
            return kw.value
    return None


def call_sites():
    """(where, module, schema-argument node) for every complete_json call."""
    sites = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_complete_json(node):
                where = f"{path.relative_to(REPO)}:{node.lineno}"
                sites.append((where, _module_name(path), _schema_arg(node)))
    return sites


def schemas():
    """(where, schema) for every schema a call site passes. A call whose schema is
    not a module-level name is returned with schema None, and fails below."""
    found = []
    for where, module, arg in call_sites():
        if isinstance(arg, ast.Name):
            found.append((f"{where} {arg.id}",
                          getattr(importlib.import_module(module), arg.id, None)))
        else:
            found.append((where, None))
    return found


def schema_problems(schema, at="schema") -> list:
    """Every reason structured output would refuse this schema."""
    problems = []
    if not isinstance(schema, dict):
        return [f"{at}: not a schema object"]

    for key in sorted(UNSUPPORTED & schema.keys()):
        problems.append(f"{at}: `{key}` is not supported")
    if schema.get("minItems", 0) not in (0, 1):
        problems.append(f"{at}: `minItems` above 1 is not supported")

    props = schema.get("properties")
    if schema.get("type") == "object" or props is not None:
        if schema.get("additionalProperties") is not False:
            problems.append(f"{at}: an object without `additionalProperties: false`")
        for name in schema.get("required", []):
            if name not in (props or {}):
                problems.append(f"{at}: requires `{name}`, which it does not declare")
        for name, sub in (props or {}).items():
            problems += schema_problems(sub, f"{at}.{name}")

    if "items" in schema:
        problems += schema_problems(schema["items"], f"{at}[]")
    for key in ("anyOf", "allOf"):
        for i, sub in enumerate(schema.get(key, [])):
            problems += schema_problems(sub, f"{at}.{key}[{i}]")
    return problems


def optional_count(schema) -> int:
    """Properties not listed in `required`, across the whole schema."""
    if not isinstance(schema, dict):
        return 0
    props = schema.get("properties") or {}
    count = len(set(props) - set(schema.get("required", [])))
    count += sum(optional_count(sub) for sub in props.values())
    count += optional_count(schema.get("items"))
    for key in ("anyOf", "allOf"):
        count += sum(optional_count(sub) for sub in schema.get(key, []))
    return count


# ─── 1. EVERY SCHEMA DAVID SENDS ───────────────────────────────────────────────

def test_the_scan_finds_every_schema_david_sends():
    """The positive control: Learn's summary, Implement's route / merge / build,
    and the Diet route / merge. A scan that finds nothing passes for free."""
    found = SCHEMAS
    assert len(found) >= 6, f"the scan found almost nothing, so it proves nothing: {found}"


SCHEMAS = schemas()


@pytest.mark.parametrize("where,schema", SCHEMAS, ids=[w for w, _ in SCHEMAS])
def test_every_schema_is_one_structured_output_accepts(where, schema):
    """RED on the schemas as they were written for forced tool use: not one of
    them said `additionalProperties: false`, so every call would be a 400."""
    assert schema is not None, (
        f"{where}: pass the schema as a module-level constant, so this check can read it")
    problems = schema_problems(schema)
    assert not problems, f"{where}:\n" + "\n".join(problems)
    assert optional_count(schema) <= MAX_OPTIONAL, (
        f"{where}: {optional_count(schema)} optional properties; the API allows "
        f"{MAX_OPTIONAL} per request")


# ─── 2. THE CHECK CAN FAIL ─────────────────────────────────────────────────────

GOOD = {"type": "object", "properties": {"a": {"type": "string"}},
        "required": ["a"], "additionalProperties": False}


def test_a_schema_that_passes_is_clean():
    assert schema_problems(GOOD) == []


@pytest.mark.parametrize("bad", [
    {**GOOD, "additionalProperties": True},
    {k: v for k, v in GOOD.items() if k != "additionalProperties"},
    {**GOOD, "properties": {"a": {"type": "object", "properties": {}}}},
    {**GOOD, "properties": {"a": {"type": "array", "items": {
        "type": "object", "properties": {"b": {"type": "string"}}}}}},
    {**GOOD, "properties": {"a": {"type": "string", "maxLength": 80}}},
    {**GOOD, "properties": {"a": {"type": "array", "items": {"type": "string"},
                                  "minItems": 2}}},
    {**GOOD, "required": ["a", "b"]},
], ids=["open-object", "unstated", "nested-object", "object-in-array",
        "maxLength", "minItems-2", "required-undeclared"])
def test_the_check_can_actually_go_red(bad):
    assert schema_problems(bad), "the check cannot see this, so it would not catch it"


def test_an_inline_schema_is_refused():
    """A schema built at the call site cannot be resolved, so it cannot be checked."""
    call = ast.parse('complete_json(s, u, {"type": "object"})').body[0].value
    assert not isinstance(_schema_arg(call), ast.Name)


def test_the_optional_count_sees_nested_properties():
    nested = {"type": "object", "properties": {
        "a": {"type": "array", "items": {"type": "object", "properties": {
            "b": {"type": "string"}, "c": {"type": "string"}}, "required": ["b"]}}}}
    assert optional_count(nested) == 2   # `a` at the top, `c` in the items


# ─── 3. EFFORT AT EACH CALL SITE ───────────────────────────────────────────────

ALLOWED_EFFORTS = {"ANTHROPIC_EFFORT", "ANTHROPIC_ROUTE_EFFORT", "effort"}


def _effort_arguments(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "effort":
                    yield node.lineno, ast.unparse(kw.value)


def test_no_call_site_passes_an_anonymous_effort():
    """Same rule as the output cap: a level is a named constant in config, so the
    one place to retune it is the one place it is written."""
    offences = []
    for path in python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        offences += [f"{path.relative_to(REPO)}:{line} -> effort={value}"
                     for line, value in _effort_arguments(tree)
                     if value not in ALLOWED_EFFORTS]
    assert not offences, "name the effort level in config.py:\n" + "\n".join(offences)


@pytest.mark.parametrize("source", [
    "complete_json(system, user, schema, effort='low')",
    "complete_json(system, user, schema, effort=LEVEL)",
])
def test_the_effort_scan_can_actually_go_red(source):
    assert [v for _, v in _effort_arguments(ast.parse(source)) if v not in ALLOWED_EFFORTS]


def _effective_effort(recorded):
    default = inspect.signature(anthropic_client.complete_json).parameters["effort"].default
    return recorded.get("effort", default)


@pytest.fixture
def spy(monkeypatch):
    """Replaces complete_json in the namespace each call site resolves it through
    (the SPY_HOMES rule), and records the keyword arguments of the last call."""
    def install(module, answer):
        recorded = {}

        def fake(system, user, schema, **kwargs):
            recorded.update(kwargs, called=True)
            return answer, None
        monkeypatch.setattr(module, "complete_json", fake)
        return recorded
    return install


def test_the_route_effort_is_lower_than_the_default():
    """Routing answers with a list of section names: classification, not
    writing. If the two levels were equal there would be no reason for two."""
    levels = ["low", "medium", "high", "xhigh", "max"]
    assert levels.index(ANTHROPIC_ROUTE_EFFORT) < levels.index(ANTHROPIC_EFFORT)


def test_manual_routing_runs_at_the_route_effort(spy):
    recorded = spy(implement, {"affected": [], "new_steps": []})
    implement.route_sections(["Overview"], "source", "Title")
    assert recorded.get("called")
    assert _effective_effort(recorded) == ANTHROPIC_ROUTE_EFFORT


def test_diet_routing_runs_at_the_route_effort(spy):
    recorded = spy(implement_diet, {"affected": []})
    implement_diet.route_sections(["Supplements"], "summary", "Title")
    assert recorded.get("called")
    assert _effective_effort(recorded) == ANTHROPIC_ROUTE_EFFORT


@pytest.mark.parametrize("module,answer,call", [
    (learn, {"title": "t", "tldr": "g"},
     lambda: learn.summarize_with_claude("article", "text", title="T", source="u")),
    (implement, {"updates": []},
     lambda: implement.merge_sections(
         [{"path": "Overview", "style": "bullet", "text": "- a line"}], "source", "T")),
    (implement, {"title": "Manual: t", "routine": []},
     lambda: implement.build_manual("source", "Topic", "T")),
    (implement_diet, {"updates": []},
     lambda: implement_diet.merge_sections({"Supplements": "- a line"}, "summary", "T")),
], ids=["learn-summary", "manual-merge", "manual-build", "diet-merge"])
def test_the_writing_calls_run_at_the_default_effort(spy, module, answer, call):
    """Summaries, merges and builds write the text that ends up in Notion, so they
    get the higher level."""
    recorded = spy(module, answer)
    call()
    assert recorded.get("called"), "the call never reached complete_json"
    assert _effective_effort(recorded) == ANTHROPIC_EFFORT
