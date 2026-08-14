"""
Takeaway of the week (Step 5) — one bullet from one Learn page, resurfaced.

WHY IT EXISTS. A personal knowledge base you only ever write to is not a
knowledge base. Everything else in David pushes INTO Notion; this is the one job
that reads something back out and puts it in front of you unasked.

It is the cheapest thing on the roadmap: one query, one page read, no Anthropic
call, no new dependency.

WHAT IT COSTS TO CHOOSE. "Has a takeaways section" lives in the page BODY, so
Notion cannot filter on it — the query returns every Learn page and the choosing
happens here. A page picked and found to have no takeaways is dropped and another
is tried, WITHOUT REPLACEMENT and up to TAKEAWAY_MAX_ATTEMPTS, so the bound
counts distinct pages rather than dice rolls.

THE FAILURE THAT WOULD BE INVISIBLE. If a page whose read FAILED were simply
skipped like a page with no takeaways, then a Notion outage — where every read
fails — would exhaust the attempts and return (None, None). Silence. Meaning
"nothing to say this week". That is the collapse the rest of this package spent a
milestone removing, so the first read error is kept and reported, and the outcome
follows briefing.build_morning_briefing's shape rather than choosing between the
two halves:

    (text, None)   a takeaway, and nothing went wrong
    (text, error)  a takeaway, but some page could not be read — send AND report
    (None, error)  no takeaway, and at least one read failed
    (None, None)   no takeaway, everything was readable: a genuinely quiet week

Returns (text, error) like every builder here, and never sends.
"""

import os
import random

from clients.notion_client import (
    blocks_to_text, extract_rich_text, get_children, get_page_title,
    query_database,
)
from config import TAKEAWAY_MAX_ATTEMPTS, TAKEAWAYS_HEADING, is_unverified_source

LEARN_ID = os.environ.get("LEARN_ID")

# Any heading ends the takeaways section. On a page David wrote the takeaways are
# last, so "to the next heading" and "to the end of the page" agree — which is
# exactly why the stricter rule is written now, while nothing depends on the
# accident. A page you have added notes to below is not a page whose notes belong
# in a takeaway.
_HEADINGS = ("heading_1", "heading_2", "heading_3")


def _block_text(block: dict) -> str:
    return extract_rich_text(block.get(block.get("type", ""), {}).get("rich_text", []))


def takeaways_in(blocks: list) -> list:
    """The bullets under the takeaways heading. Empty if the page has none.

    Public rather than private: tests drive it with blocks built by the REAL
    services/learn.build_notion_blocks, which is what keeps the writer and this
    reader agreeing about TAKEAWAYS_HEADING.
    """
    found, bullets = False, []
    for block in blocks:
        btype = block.get("type", "")
        if btype in _HEADINGS:
            if found:
                break                      # the next heading ends the section
            found = _block_text(block).strip() == TAKEAWAYS_HEADING
            continue
        if found and btype == "bulleted_list_item":
            text = _block_text(block).strip()
            if text:
                bullets.append(text)
    return bullets


def _format(bullet: str, title: str, unverified: bool) -> str:
    """PLAIN TEXT — the bullet and the title are both Claude's words about
    someone else's, and routinely carry * _ and `."""
    lines = ["💡 Takeaway of the week", "", bullet, "", f"— from “{title}”"]
    if unverified:
        # The provenance has to travel with it. A takeaway lifted out of a
        # recollection page and sent on its own is exactly the case
        # UNVERIFIED_MARKER exists for: it reads as a fact about a book that
        # nobody read.
        lines.append("⚠️ That page is a recollection, not an extract — verify "
                     "before acting on it.")
    return "\n".join(lines)


def build_takeaway(*, choose=random.choice) -> tuple:
    """Return (message, error) — see the four outcomes in the module docstring.

    `choose` is a parameter rather than a seed so a test can pass
    `lambda seq: seq[0]` and assert on an exact page. Seeding would be global
    state another test could disturb, and it would pin the ALGORITHM rather than
    the decision. It picks the page AND the bullet, so there is one source of
    randomness to control.
    """
    if not LEARN_ID:
        return None, "LEARN_ID is not set, so I cannot pick a takeaway."

    pages, err = query_database(LEARN_ID)
    if err:
        return None, f"Could not read the Learn database: {err}"
    if not pages:
        return None, None

    pool = list(pages)
    first_error = None

    for _ in range(TAKEAWAY_MAX_ATTEMPTS):
        if not pool:
            break
        page = choose(pool)
        pool.remove(page)                  # without replacement — see config

        blocks, err = get_children(page.get("id", ""))
        if err:
            # NOT a skip. A skip here makes an outage look like a quiet week.
            if first_error is None:
                first_error = (f"Could not read “{get_page_title(page)}” while "
                               f"picking a takeaway: {err}")
            continue

        bullets = takeaways_in(blocks)
        if not bullets:
            continue                       # no takeaways is not a failure

        return _format(choose(bullets), get_page_title(page),
                       is_unverified_source(blocks_to_text(blocks))), first_error

    # Nothing found. Which of the two silences this is depends entirely on
    # whether anything failed on the way.
    return None, first_error
