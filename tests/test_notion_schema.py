"""One home for every Notion name, enforced by a scan rather than by discipline.

THE RULE
--------
    A Notion column name, or the title of a page David looks up by name, is
    written in config.py's NOTION SCHEMA section and nowhere else. Every other
    module imports it.

WHY. Before this there were fifteen named properties and three constants, and
one of those three — IMPLEMENTED_PROPERTY — was ALSO spelled out as a literal by
all three of its writers: the constant lived in proactive/, the writers live in
services/, and services/ may not import proactive/. A column renamed in Notion
meant finding every spelling by hand, and the one you miss is a 400 on a write,
or worse, a filter that matches nothing and reads as "no such row".

WHY AST AND NOT GREP. Prose all over the codebase says "Name", "Amount" and
"Implemented", and has to keep saying it. Only a string in a position where
Notion reads it as a column name or a page title counts.

WHAT A NOTION NAME IS, mechanically. Notion's own API keys are lowercase
snake_case — "title", "rich_text", "property", "paragraph", "link". A schema
name is whatever the workspace owner typed. So a string that is NOT shaped like
an API key, in a position where a column name goes, is a column name.

THE FIVE POSITIONS
  payload key    {"Amount": {"number": x}}                 a create or an update
                 properties["Author"] = {"rich_text": x}   (either spelling)
  filter         {"property": "Name", "title": {...}}      a query
  schema dict    {"Amount": "number"}                      an expected-type table
  property read  props.get("Amount", {}).get("number")     reading a row
  page title     search_page_in_db(db, "Manual")           a lookup, or the
                 {"title": [{"text": {"content": "X"}}]}   title a create sends

AND ONE DEFINITION. `FOO_PROPERTY = "Foo"` in any module but config.py is a
second home starting — how LEARN_SOURCE_PROPERTY and IMPLEMENTED_PROPERTY came
to live beside their first reader, and why the second reader never used them.

WHAT IT DOES NOT CATCH. A name in any other position — a dict of titles keyed
by area (services/pkm.py's _AREA_PAGE_TITLE was one), a default argument, a name
built by concatenation — or a schema name that happens to be lowercase
snake_case. It guards the shapes this codebase actually uses, and
test_every_check_still_sees_real_call_sites fails if the code stops using them,
so the guard cannot quietly go blind.
"""

import ast
import pathlib
import re
from collections import Counter

import pytest

from config import LEARN_TYPES
from services import learn

REPO = pathlib.Path(__file__).resolve().parent.parent
HOME = REPO / "config.py"

# The same packages every other source scan walks (see CLAUDE.md, Testing). A
# file that moves into a new package must not drop out of the guard.
PACKAGES = ("bot", "clients", "proactive", "services")

# The types a page PROPERTY can hold. rich_text is also a block field; the
# API-key shape test is what keeps {"paragraph": {"rich_text": ...}} out.
PROPERTY_TYPES = frozenset({
    "title", "rich_text", "number", "date", "select", "multi_select", "status",
    "relation", "checkbox", "url", "email", "phone_number", "people", "files",
})

_API_KEY = re.compile(r"[a-z][a-z0-9_]*\Z")

# What the schema constants are called, so a definition of one outside config.py
# is recognisable by its name alone.
_SCHEMA_CONSTANT = re.compile(r"[A-Z0-9_]+_(PROPERTY|RELATION|PAGE_TITLE)\Z")

# The positions whose compliant uses are counted. A DEFINITION has no compliant
# use outside config.py by construction, so its guard is the offender row alone.
KINDS = ("payload key", "filter", "schema dict", "property read", "page title")


def scanned_files() -> list:
    """Every production module except the one home itself."""
    files = [path for path in sorted(REPO.glob("*.py")) if path != HOME]
    for package in PACKAGES:
        files += sorted((REPO / package).rglob("*.py"))
    return files


# ─── THE SCAN ──────────────────────────────────────────────────────────────────

def _string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _string_keys(node: ast.Dict) -> set:
    return {_string(key) for key in node.keys if key is not None} - {None}


def _get_argument(node):
    """The first argument of `x.get(arg)`, seen through `(x.get(arg) or {})`."""
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
        node = node.values[0]
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get" and node.args):
        return node.args[0]
    return None


def _is_search(func) -> bool:
    return ((isinstance(func, ast.Name) and func.id == "search_page_in_db")
            or (isinstance(func, ast.Attribute) and func.attr == "search_page_in_db"))


def _is_to_thread(func) -> bool:
    return ((isinstance(func, ast.Name) and func.id == "to_thread")
            or (isinstance(func, ast.Attribute) and func.attr == "to_thread"))


def _is_property_value(node) -> bool:
    """A property's value payload, e.g. {"number": x} — or either arm of
    `{"url": u} if ... else {"rich_text": r}`."""
    if isinstance(node, ast.IfExp):
        return _is_property_value(node.body) or _is_property_value(node.orelse)
    return isinstance(node, ast.Dict) and bool(_string_keys(node) & PROPERTY_TYPES)


def scan(source: str) -> tuple[list, Counter]:
    """(offences, seen) for one module.

    `offences` is every quoted Notion name, as "line: kind 'text'". `seen`
    counts, per kind, the sites that name it through a constant instead — which
    is what proves each check is still looking at code this repo really has.
    """
    offences, seen = [], Counter()

    def judge(kind: str, node, *, any_string: bool = False) -> None:
        text = _string(node)
        if text is None:
            seen[kind] += 1                       # a constant, or computed: fine
        elif any_string or not _API_KEY.match(text):
            offences.append(f"{node.lineno}: {kind} {text!r}")
        # else: a Notion API key such as "paragraph" — not a schema name at all

    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.Assign) and _string(node.value) is not None:
            for target in node.targets:
                if isinstance(target, ast.Name) and _SCHEMA_CONSTANT.match(target.id):
                    offences.append(f"{node.lineno}: definition {target.id} = "
                                    f"{node.value.value!r} (it belongs in config.py)")

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_property_value(node.value):
            for target in node.targets:
                if isinstance(target, ast.Subscript):
                    judge("payload key", target.slice)

        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if key is None:                   # a `**spread`
                    continue
                if _string(key) == "property":
                    judge("filter", value)
                if _is_property_value(value):
                    judge("payload key", key)
                    if isinstance(value, ast.Dict) and "title" in _string_keys(value):
                        for inner in ast.walk(value):
                            if isinstance(inner, ast.Dict):
                                for k, v in zip(inner.keys, inner.values):
                                    if k is not None and _string(k) == "content":
                                        judge("page title", v, any_string=True)
                if _string(value) in PROPERTY_TYPES:
                    judge("schema dict", key)

        if isinstance(node, ast.Call):
            if (isinstance(node.func, ast.Attribute) and node.func.attr == "get"
                    and node.args and _string(node.args[0]) in PROPERTY_TYPES):
                inner = _get_argument(node.func.value)
                if inner is not None:
                    judge("property read", inner)
            if _is_search(node.func) and len(node.args) >= 2:
                judge("page title", node.args[1], any_string=True)
            if (_is_to_thread(node.func) and len(node.args) >= 3
                    and _is_search(node.args[0])):
                judge("page title", node.args[2], any_string=True)

        elif isinstance(node, ast.Subscript) and _string(node.slice) in PROPERTY_TYPES:
            if isinstance(node.value, ast.Subscript):
                judge("property read", node.value.slice)

    return offences, seen


def scan_repo() -> tuple[list, Counter]:
    offences, seen = [], Counter()
    for path in scanned_files():
        found, counted = scan(path.read_text(encoding="utf-8"))
        offences += [f"{path.relative_to(REPO).as_posix()}:{line}" for line in found]
        seen += counted
    return offences, seen


# ─── THE RULE ──────────────────────────────────────────────────────────────────

def test_every_notion_name_lives_in_config():
    """THE acceptance test. Put the name in config.py's NOTION SCHEMA section
    and import it — per database, even when two databases agree today."""
    offences, _ = scan_repo()

    assert offences == [], (
        "A Notion column name or page title is spelled out outside config.py. "
        "Add it to the NOTION SCHEMA section and import it:\n" + "\n".join(offences))


def test_the_scan_is_looking_at_real_files():
    """Guards the guard against passing vacuously: a glob that matches nothing
    reports zero offences and looks exactly like compliance."""
    files = scanned_files()
    names = {path.relative_to(REPO).as_posix() for path in files}

    assert HOME not in files, "config.py is the one home — scanning it is a category error"
    assert "budget.py" in names, "the repo root is not being scanned"
    for package in PACKAGES:
        assert any(name.startswith(f"{package}/") for name in names), (
            f"nothing under {package}/ is being scanned")


def test_every_check_still_sees_real_call_sites():
    """The positive control, against this repo's own code.

    Each check has to find at least one site that names Notion through a
    constant. If a refactor moved every payload behind a helper the checks do
    not recognise, the rule above would stay green while guarding nothing —
    this is what turns that red.
    """
    _, seen = scan_repo()

    blind = [kind for kind in KINDS if seen[kind] == 0]
    assert blind == [], f"these checks no longer see any real call site: {blind}"


@pytest.mark.parametrize("source, expected", [
    ('data = {"Amount": {"number": 5}}\n',                              "payload key 'Amount'"),
    ('data = {"Source URL": {"url": u}}\n',                             "payload key 'Source URL'"),
    ('properties["Author"] = {"rich_text": r}\n',                       "payload key 'Author'"),
    ('p["Source URL"] = {"url": u} if t == "url" else {"rich_text": r}\n',
     "payload key 'Source URL'"),
    ('f = {"property": "Name", "title": {"contains": q}}\n',            "filter 'Name'"),
    ('EXPECTED = {"Category": "multi_select"}\n',                       "schema dict 'Category'"),
    ('a = props.get("Amount", {}).get("number", 0)\n',                  "property read 'Amount'"),
    ('c = (props.get("Category") or {}).get("multi_select")\n',         "property read 'Category'"),
    ('d = props["Date"]["date"]\n',                                     "property read 'Date'"),
    ('p = search_page_in_db(DIET_ID, "Diet", exact=True)\n',            "page title 'Diet'"),
    ('p = await asyncio.to_thread(search_page_in_db, db, "Manual")\n',  "page title 'Manual'"),
    ('t = {T: {"title": [{"text": {"content": "Manual"}}]}}\n',         "page title 'Manual'"),
    ('IMPLEMENTED_PROPERTY = "Implemented"\n',                          "definition IMPLEMENTED_PROPERTY"),
    ('DIET_PAGE_TITLE = "Diet"\n',                                      "definition DIET_PAGE_TITLE"),
])
def test_the_guard_can_actually_detect_an_offender(source, expected):
    """A guard that cannot fail is not a guard. Every row except the subscript
    one is a shape that was in this codebase before the constants existed."""
    offences, _ = scan(source)

    assert any(expected in line for line in offences), (
        f"the guard missed {source!r} — it reported {offences}")


@pytest.mark.parametrize("source", [
    'b = {"paragraph": {"rich_text": rich(text)}}\n',           # a block, not a property
    'b = {"text": {"content": url, "link": {"url": url}}}\n',   # a link, not a property
    'f = {"value": "database", "property": "object"}\n',        # the /search API's own filter
    's = {"type": "object", "properties": {"n": {"type": "number"}}}\n',  # a JSON Schema
    '"""Writes Name, Amount and {"Implemented": {"checkbox": True}}."""\n',  # prose
])
def test_notion_api_keys_and_prose_are_not_offences(source):
    """The other half of a usable guard. One that fires on Notion's own keys
    would be switched off within a week."""
    offences, _ = scan(source)

    assert offences == [], f"false positive on {source!r}: {offences}"


# ─── LEARN FILES INTO TWO DATABASES ────────────────────────────────────────────
# `Learn book` goes to Books and every other type to Learn, through ONE
# properties dict in create_learn_page. With per-database constants the names
# have to be picked by database, and these two tests are what hold that up.

def test_every_learn_type_files_into_a_database_whose_columns_are_known():
    """_COLUMNS_BY_DB is indexed directly, so a type added to LEARN_TYPES with a
    new database would be a KeyError on its first save. This is where that
    surfaces instead — in CI, naming the database."""
    missing = sorted({t.db_env for t in LEARN_TYPES.values()} - set(learn._COLUMNS_BY_DB))

    assert missing == [], f"no title/author columns declared for: {missing}"


@pytest.mark.parametrize("content_type, db_env", [("book", "LETTI_ID"), ("article", "LEARN_ID")])
def test_each_learn_type_writes_its_own_databases_column_names(monkeypatch, content_type, db_env):
    """The names really are chosen per database, not merely declared per
    database. Books and Learn both say "Name" and "Author" today, so only
    distinct sentinel names can tell the two routes apart."""
    columns = {"LEARN_ID": ("learn-title", "learn-author"),
               "LETTI_ID": ("books-title", "books-author")}
    monkeypatch.setattr(learn, "_COLUMNS_BY_DB", columns)
    sent = {}

    def fake_create_page(db_id, properties, children=None, icon=None):
        sent.update(properties)
        return "page-1", None

    monkeypatch.setattr(learn, "create_page", fake_create_page)

    ok, page_id, _ = learn.create_learn_page(content_type, "A Title", [],
                                             metadata={"author": "Someone"})

    title_column, author_column = columns[db_env]
    assert ok and page_id == "page-1"
    assert set(sent) == {title_column, author_column}
