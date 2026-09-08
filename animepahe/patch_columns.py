"""
    One-time-ish migration: backfills studios/themes/genres/demographics/
    external_links on rows that were scraped before those became
    list/object-array fields (see parsers.PATCH_FIELDS), without
    duplicating or reordering any existing row.

    Safe to stop and re-run: a row counts as already patched if its
    `studios` cell already decodes as JSON - so restarting after an
    interruption just skips rows already done instead of re-fetching them.

    Rewrites the whole CSV after every successfully patched row (via a
    temp file + atomic os.replace), trading a bit of raw throughput for
    never losing progress on a crash/interrupt. Fine at hundred-to-low-
    thousand-row scale; if the dataset grows much larger, batch the
    rewrite instead of doing it every row.
"""

import argparse
import asyncio
import csv
import json
import os
import sys

from pydoll.browser.chromium import Chrome

from common.browser import build_browser_options
from common.throttle import human_delay

from common.csv_store import read_csv_rows

from .config import BASE_URL, CSV_PATH
from .parsers import PATCH_FIELDS
from .scraper import scrape_patch_fields


def _write_rows(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def _already_patched(row: dict) -> bool:
    value = row.get("studios")
    if not value:
        return False
    try:
        return isinstance(json.loads(value), list)
    except (json.JSONDecodeError, TypeError):
        return False


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill studios/themes/genres/demographics/external_links to list/object format."
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List which rows would be patched without launching a browser, scraping, or writing anything.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process the first N rows that need patching - useful for testing on a handful before a full run.",
    )
    return parser.parse_args(argv)


async def main(dry_run: bool = False, limit: int | None = None):
    if not os.path.exists(CSV_PATH):
        raise SystemExit(f"No CSV found at {CSV_PATH} - nothing to patch.")

    fieldnames, rows = read_csv_rows(CSV_PATH)
    missing = [f for f in PATCH_FIELDS if f not in fieldnames]
    if missing:
        raise SystemExit(
            f"{CSV_PATH} is missing column(s) {missing} - this CSV predates "
            "those fields being added to config.FIELD_NAMES entirely, not "
            "just their format. This tool only backfills format, not new "
            "columns - you'd need a full rescrape for that."
        )

    todo = [row for row in rows if not _already_patched(row)]
    print(f"{len(rows)} rows total, {len(todo)} need patching, {len(rows) - len(todo)} already done")

    if not todo:
        print("Nothing to do.")
        return

    if limit is not None:
        todo = todo[:limit]
        print(f"--limit {limit}: only processing the first {len(todo)} of those")

    if dry_run:
        print("\n--dry-run: no browser, scraping, or writing will happen. Rows that would be patched:")
        for row in todo:
            print(f"  - {row.get('title')} ({row['pahe_id']})")
        print(f"Total: {len(todo)}")
        return

    options = build_browser_options()

    async with Chrome(options=options) as browser:
        tab = await browser.start()
        await tab.enable_auto_solve_cloudflare_captcha()

        for i, row in enumerate(todo, start=1):
            pahe_id = row["pahe_id"]
            url = f"{BASE_URL}/anime/{pahe_id}"
            await human_delay()

            try:
                patch = await scrape_patch_fields(tab, url)
            except Exception as e:
                print(f"<e> [{i}/{len(todo)}] patch failed for {row.get('title')} ({pahe_id}): {e}")
                continue

            # Mutates the same dict object that's sitting in `rows`, so the
            # full rewrite below reflects it without any separate merge step.
            row.update(patch)
            _write_rows(CSV_PATH, fieldnames, rows)
            print(f"  [{i}/{len(todo)}] patched: {row.get('title')}")

        await tab.disable_auto_solve_cloudflare_captcha()

    print("Done.")


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    asyncio.run(main(dry_run=args.dry_run, limit=args.limit))
