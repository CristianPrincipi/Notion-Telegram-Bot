"""The docs say what the code does — checked, not hoped.

Every doc that copies something from the code by hand drifts: the hand-written
help advertised `Learn recipe`, the README's environment table and the startup
error were "one text" that was never one text. CLAUDE.md's Documentation contract
says which doc a change must update; these tests catch the update that was
forgotten anyway.

Each check is a pure function from (what the code says, what the doc says) to a
list of problems. The real tests assert that list is empty. The can-it-fail tests
take the REAL doc, delete one entry from it in memory, and assert the check names
what went missing — the revert check, run on every test run instead of once.

Covered here: environment variables (docs/configuration.md, .env.example), the
Notion schema (docs/notion-schema.md), the scheduled jobs (docs/features.md), and
every relative link and anchor, and the changelog's `[Unreleased]` section.
Commands are covered by the Commands-table tests in tests/test_router.py.
"""

import re
from pathlib import Path

import pytest
from telegram.ext import ApplicationBuilder

import config
import david

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIGURATION = DOCS / "configuration.md"
NOTION_SCHEMA = DOCS / "notion-schema.md"
FEATURES = DOCS / "features.md"
ENV_EXAMPLE = ROOT / ".env.example"

# The one documented variable that is a pattern, not a name.
AREA_PATTERN = "{AREA}_ID"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# ─── MARKDOWN HELPERS ──────────────────────────────────────────────────────────

def section(text: str, heading: str) -> str:
    """The body under `## heading`, up to the next heading of the same level."""
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.M)
    assert match, f"no '## {heading}' section"
    rest = text[match.end():]
    end = re.search(r"^## ", rest, re.M)
    return rest[:end.start()] if end else rest


def column_codes(text: str, column: int) -> list:
    """Every `code span` in the given column of every table row in `text`."""
    codes = []
    for line in text.splitlines():
        if not line.startswith("|") or re.match(r"^\|\s*-", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if column < len(cells):
            codes += re.findall(r"`([^`]+)`", cells[column])
    return codes


# ─── 1. ENVIRONMENT VARIABLES ──────────────────────────────────────────────────

def env_doc_problems(required: set, optional: set, configuration_md: str) -> list:
    documented_required = set(column_codes(section(configuration_md, "Required"), 0))
    documented_optional = set(column_codes(section(configuration_md, "Optional"), 0)) - {AREA_PATTERN}
    problems = [f"{name} is required in config but not in the Required table"
                for name in sorted(required - documented_required)]
    problems += [f"{name} is optional in config but not in the Optional table"
                 for name in sorted(optional - documented_optional)]
    problems += [f"{name} is documented but config does not read it"
                 for name in sorted((documented_required | documented_optional)
                                    - required - optional)]
    return problems


ENV_LINE = re.compile(r"^(#\s*)?([A-Z][A-Z0-9_]*)=", re.M)


def env_example_problems(required: set, optional: set, env_example: str) -> list:
    active = {name for comment, name in ENV_LINE.findall(env_example) if not comment}
    listed = {name for _, name in ENV_LINE.findall(env_example)}
    problems = [f"{name} is missing from .env.example" for name in sorted((required | optional) - listed)]
    problems += [f"{name} is required but commented out in .env.example"
                 for name in sorted(required & listed - active)]
    problems += [f"{name} is in .env.example but config does not read it"
                 for name in sorted(listed - required - optional)]
    return problems


def test_configuration_md_documents_exactly_the_variables_config_reads():
    problems = env_doc_problems(set(config.REQUIRED_ENV), set(config.OPTIONAL_ENV),
                                read(CONFIGURATION))
    assert problems == [], "\n".join(problems)


def test_env_example_lists_exactly_the_variables_config_reads():
    problems = env_example_problems(set(config.REQUIRED_ENV), set(config.OPTIONAL_ENV),
                                    read(ENV_EXAMPLE))
    assert problems == [], "\n".join(problems)


# ─── 2. THE NOTION SCHEMA ──────────────────────────────────────────────────────

SCHEMA_CONSTANT = re.compile(r"[A-Z0-9_]+_(PROPERTY|RELATION|PAGE_TITLE)")


def schema_names() -> dict:
    """{constant: value} for every Notion name in config's NOTION SCHEMA."""
    return {name: getattr(config, name) for name in dir(config) if SCHEMA_CONSTANT.fullmatch(name)}


def option_values() -> set:
    return set(config.CATEGORY_MAP.values()) | set(config.GENRE_MAP.values())


def documented_columns(notion_schema_md: str) -> list:
    """First-column code spans of every `| Column |` table."""
    columns, in_table = [], False
    for line in notion_schema_md.splitlines():
        if line.startswith("| Column |"):
            in_table = True
            continue
        if not line.startswith("|"):
            in_table = False
        elif in_table:
            columns += column_codes(line, 0)
    return columns


def notion_doc_problems(names: dict, options: set, notion_schema_md: str) -> list:
    problems = [f"{constant} = {value!r} is not in docs/notion-schema.md"
                for constant, value in sorted(names.items()) if f"`{value}`" not in notion_schema_md]
    problems += [f"option {value!r} is not in docs/notion-schema.md"
                 for value in sorted(options) if f"`{value}`" not in notion_schema_md]
    problems += [f"docs/notion-schema.md lists column {column!r}, which config does not name"
                 for column in sorted(set(documented_columns(notion_schema_md)) - set(names.values()))]
    return problems


def test_notion_schema_md_matches_config():
    problems = notion_doc_problems(schema_names(), option_values(), read(NOTION_SCHEMA))
    assert problems == [], "\n".join(problems)


# ─── 3. SCHEDULED JOBS ─────────────────────────────────────────────────────────

def registered_jobs() -> set:
    """What actually gets scheduled — read off a real JobQueue, both families
    (proactive.scheduler's and david's budget recap), not grepped from source."""
    application = ApplicationBuilder().token("123456:test-token").build()
    assert david.register_jobs(application, "-100123"), "scheduling is off"
    return {job.name for job in application.job_queue.jobs()}


def jobs_doc_problems(jobs: set, features_md: str) -> list:
    documented = set(column_codes(section(features_md, "Scheduled messages"), 1))
    problems = [f"job {name!r} is not in the Scheduled messages table" for name in sorted(jobs - documented)]
    problems += [f"the Scheduled messages table lists {name!r}, which is not a job"
                 for name in sorted(documented - jobs)]
    return problems


def test_features_md_lists_exactly_the_scheduled_jobs():
    problems = jobs_doc_problems(registered_jobs(), read(FEATURES))
    assert problems == [], "\n".join(problems)


# ─── 4. LINKS AND ANCHORS ──────────────────────────────────────────────────────

def linked_files() -> list:
    files = [ROOT / "README.md", ROOT / "CHANGELOG.md", ROOT / "CLAUDE.md", *sorted(DOCS.glob("*.md"))]
    files += sorted((ROOT / ".claude").rglob("*.md"))
    return files


FENCE = re.compile(r"^(```|~~~).*?^\1", re.S | re.M)


def anchors(markdown: str) -> set:
    """GitHub's heading anchors: lower-cased, punctuation dropped, spaces to
    hyphens, and a -N suffix on repeats."""
    found, seen = set(), {}
    for line in FENCE.sub("", markdown).splitlines():
        if not line.startswith("#"):
            continue
        slug = re.sub(r"[^\w\- ]", "", line.lstrip("#").strip().lower()).replace(" ", "-")
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        found.add(slug if count == 0 else f"{slug}-{count}")
    return found


def link_problems(path: Path, markdown: str) -> list:
    problems = []
    for target in re.findall(r"\]\(([^)\s]+)\)", FENCE.sub("", markdown)):
        if re.match(r"[a-z]+:", target):
            continue
        file_part, _, anchor = target.partition("#")
        destination = (path.parent / file_part).resolve() if file_part else path.resolve()
        if not destination.exists():
            problems.append(f"{path.relative_to(ROOT)}: {target} — no such file")
        elif anchor and destination.suffix == ".md" and anchor not in anchors(read(destination)):
            problems.append(f"{path.relative_to(ROOT)}: {target} — no such heading")
    return problems


def test_every_relative_link_and_anchor_resolves():
    problems = [problem for path in linked_files() for problem in link_problems(path, read(path))]
    assert problems == [], "\n".join(problems)


# ─── 5. THE CHANGELOG ──────────────────────────────────────────────────────────

def changelog_problems(changelog_md: str) -> list:
    """A release renames `[Unreleased]`; the next change still has to land somewhere.

    CLAUDE.md's Documentation contract and `/ship-feature` both write under
    `## [Unreleased]`, so it stays the FIRST section, empty or not.
    """
    sections = re.findall(r"^## \[(.+?)\]", changelog_md, flags=re.MULTILINE)
    problems = []
    if not sections or sections[0] != "Unreleased":
        problems.append(
            "CHANGELOG.md's first section is not [Unreleased]: "
            "the Documentation contract and /ship-feature write there"
        )
    if len(sections) != len(set(sections)):
        problems.append("CHANGELOG.md names a section twice")
    return problems


def test_the_changelog_keeps_an_unreleased_section_on_top():
    assert changelog_problems(read(ROOT / "CHANGELOG.md")) == []


# ─── CAN THESE FAIL? ───────────────────────────────────────────────────────────
# Each takes the REAL doc, removes one entry in memory, and asserts the check
# names it. A check that stays quiet here is reading nothing.

def without_line(text: str, needle: str) -> str:
    lines = text.splitlines(keepends=True)
    kept = [line for line in lines if needle not in line]
    assert len(kept) == len(lines) - 1, f"expected exactly one line containing {needle!r}"
    return "".join(kept)


def test_a_variable_dropped_from_configuration_md_is_caught():
    doctored = without_line(read(CONFIGURATION), "| `NOTION_KEY` |")
    problems = env_doc_problems(set(config.REQUIRED_ENV), set(config.OPTIONAL_ENV), doctored)
    assert any("NOTION_KEY" in problem for problem in problems), problems


def test_a_variable_in_the_wrong_table_is_caught():
    """Required and optional are different promises — the startup check enforces one."""
    moved = without_line(read(CONFIGURATION), "| `BUDGET_CEILING` |").replace(
        "| `NOTION_KEY` |", "| `BUDGET_CEILING` | x |\n| `NOTION_KEY` |", 1)
    problems = env_doc_problems(set(config.REQUIRED_ENV), set(config.OPTIONAL_ENV), moved)
    assert any("BUDGET_CEILING is optional" in problem for problem in problems), problems


def test_a_variable_config_does_not_know_is_caught():
    doctored = read(CONFIGURATION).replace(
        "| `LOG_LEVEL` |", "| `NOT_A_REAL_VAR` | — | x |\n| `LOG_LEVEL` |", 1)
    problems = env_doc_problems(set(config.REQUIRED_ENV), set(config.OPTIONAL_ENV), doctored)
    assert any("NOT_A_REAL_VAR" in problem for problem in problems), problems


def test_env_example_gaps_are_caught():
    text = read(ENV_EXAMPLE)
    required, optional = set(config.REQUIRED_ENV), set(config.OPTIONAL_ENV)

    missing = env_example_problems(required, optional, without_line(text, "SUPADATA_KEY="))
    commented = env_example_problems(required, optional, text.replace("\nLEARN_ID=", "\n# LEARN_ID=", 1))
    unknown = env_example_problems(required, optional, text + "\nNOT_A_REAL_VAR=\n")

    assert any("SUPADATA_KEY is missing" in problem for problem in missing), missing
    assert any("LEARN_ID is required but commented out" in problem for problem in commented), commented
    assert any("NOT_A_REAL_VAR" in problem for problem in unknown), unknown


def test_notion_schema_gaps_are_caught():
    text = read(NOTION_SCHEMA)

    no_column = notion_doc_problems(schema_names(), option_values(), text.replace("`Source URL`", "`Link`"))
    no_option = notion_doc_problems(schema_names(), option_values(), text.replace("`Philosophy`", "Philosophy"))

    assert any("LEARN_SOURCE_PROPERTY" in problem for problem in no_column), no_column
    assert any("'Link'" in problem for problem in no_column), "a column config does not name went unnoticed"
    assert any("Philosophy" in problem for problem in no_option), no_option


def test_a_job_missing_from_the_table_is_caught():
    doctored = without_line(read(FEATURES), "`heartbeat`")
    problems = jobs_doc_problems(registered_jobs(), doctored)
    assert any("'heartbeat'" in problem for problem in problems), problems


def test_a_broken_link_or_anchor_is_caught():
    readme = ROOT / "README.md"
    text = read(readme)

    no_file = link_problems(readme, text.replace("(docs/setup.md)", "(docs/set-up.md)", 1))
    no_anchor = link_problems(readme, text.replace("docs/setup.md#8-first-run-checklist",
                                                   "docs/setup.md#9-first-run-checklist", 1))

    assert any("docs/set-up.md" in problem for problem in no_file), no_file
    assert any("#9-first-run-checklist" in problem for problem in no_anchor), no_anchor


def test_a_release_that_drops_unreleased_is_caught():
    """What a release does by hand: rename the heading. Renaming without re-adding is the bug."""
    changelog = read(ROOT / "CHANGELOG.md")
    assert "## [Unreleased]\n" in changelog
    renamed = changelog.replace("## [Unreleased]\n", "", 1)
    assert any("[Unreleased]" in problem for problem in changelog_problems(renamed))

    released_twice = changelog.replace("## [Unreleased]\n", "## [Unreleased]\n\n## [Unreleased]\n", 1)
    assert any("twice" in problem for problem in changelog_problems(released_twice))


def test_the_checks_are_reading_real_docs():
    """Lower bounds, so a renamed heading or a reformatted table cannot turn a
    check into one that compares two empty sets and passes."""
    configuration = read(CONFIGURATION)
    assert len(column_codes(section(configuration, "Required"), 0)) >= len(config.REQUIRED_ENV)
    assert len(column_codes(section(configuration, "Optional"), 0)) >= len(config.OPTIONAL_ENV)
    assert len(ENV_LINE.findall(read(ENV_EXAMPLE))) >= len(config.REQUIRED_ENV) + len(config.OPTIONAL_ENV)
    assert len(schema_names()) >= 15
    assert len(documented_columns(read(NOTION_SCHEMA))) >= 10
    assert len(registered_jobs()) >= 8
    assert len(column_codes(section(read(FEATURES), "Scheduled messages"), 1)) >= 8
    links = sum(len(re.findall(r"\]\(([^)\s]+)\)", FENCE.sub("", read(path)))) for path in linked_files())
    assert links >= 50, f"only {links} links found — the link check is reading the wrong files"


@pytest.mark.parametrize("path", linked_files(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_linked_file_exists(path):
    """The link check's own inputs — a file that moved would silently drop out."""
    assert path.exists()
