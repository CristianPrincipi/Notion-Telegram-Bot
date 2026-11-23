"""Build today's newspaper EPUB locally, without Telegram.

    python scripts/build_newspaper.py [output-folder] [--sources path/to/news_sources.yaml]

Reads the real feeds, writes Giornale_AAAA-MM-GG.epub into the folder (the
current one by default) and prints the caption the bot would send. Needs no
environment variable: nothing here touches Notion, Telegram or a model.
"""

import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from clients.calendar_client import now_local  # noqa: E402
from services.newspaper import build_newspaper  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folder", nargs="?", default=".")
    parser.add_argument("--sources", help="a news_sources.yaml other than the repo's")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    result, err = build_newspaper(now_local(), config_path=args.sources)
    if err:
        print(f"❌ {err}", file=sys.stderr)
        return 1
    path = os.path.join(args.folder, result.filename)
    with open(path, "wb") as fh:
        fh.write(result.epub)
    print(f"\n{path} ({len(result.epub) // 1024} KB)\n\n{result.caption}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
