"""
    One-time-ish migration: replaces image_url's animepahe CDN link with
    the matching Cloudinary URL, using the mapping CSV produced by
    tools/upload_to_cloudinary.py.

    Safe to stop and re-run: a row counts as already applied if its
    image_url already points at res.cloudinary.com, so restarting after
    an interruption just skips rows already done instead of reapplying.

    Rewrites the whole CSV after every successfully updated row (same
    atomic temp-file + os.replace pattern as patch_columns.py), trading a
    bit of raw throughput for never losing progress on a crash/interrupt.

    No browser, no scraping - this is a pure local file merge, so it runs
    instantly compared to patch_columns.py.
"""

import argparse
import csv
import os
import sys

from common.csv_store import read_csv_rows

from .config import CSV_PATH

_CLOUDINARY_PREFIX = "https://res.cloudinary.com/"


def _write_rows(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def _load_mapping(path: str) -> dict[str, str]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        return {
            row["pahe_id"]: row["cloudinary_url"]
            for row in csv.DictReader(f)
            if row.get("pahe_id") and row.get("cloudinary_url")
        }


def _already_applied(row: dict) -> bool:
    return (row.get("image_url") or "").startswith(_CLOUDINARY_PREFIX)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replace image_url with the matching Cloudinary URL from a mapping CSV."
    )
    parser.add_argument(
        "--mapping-csv", required=True,
        help="The {pahe_id, cloudinary_url} CSV produced by tools/upload_to_cloudinary.py",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List which rows would be updated without writing anything.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Only process the first N rows that need updating - useful for testing on a handful before a full run.",
    )
    return parser.parse_args(argv)


def main(mapping_csv: str, dry_run: bool = False, limit: int | None = None):
    if not os.path.exists(CSV_PATH):
        raise SystemExit(f"No CSV found at {CSV_PATH} - nothing to update.")
    if not os.path.exists(mapping_csv):
        raise SystemExit(f"No mapping CSV found at {mapping_csv}.")

    fieldnames, rows = read_csv_rows(CSV_PATH)
    if "image_url" not in fieldnames:
        raise SystemExit(f"{CSV_PATH} has no image_url column - nothing to update.")

    mapping = _load_mapping(mapping_csv)
    print(f"{len(mapping)} uploaded image(s) found in {mapping_csv}")

    todo = [row for row in rows if not _already_applied(row) and row.get("pahe_id") in mapping]
    already_done = sum(1 for row in rows if _already_applied(row))
    print(f"{len(rows)} rows total, {len(todo)} need updating, {already_done} already done")

    if not todo:
        print("Nothing to do.")
        return

    if limit is not None:
        todo = todo[:limit]
        print(f"--limit {limit}: only processing the first {len(todo)} of those")

    if dry_run:
        print("\n--dry-run: nothing will be written. Rows that would be updated:")
        for row in todo:
            print(f"  - {row.get('title')} ({row['pahe_id']})")
        print(f"Total: {len(todo)}")
        return

    for i, row in enumerate(todo, start=1):
        # Mutates the same dict object sitting in `rows`, so the full
        # rewrite below reflects it without any separate merge step.
        row["image_url"] = mapping[row["pahe_id"]]
        _write_rows(CSV_PATH, fieldnames, rows)
        print(f"  [{i}/{len(todo)}] updated image_url: {row.get('title')}")

    print("Done.")


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    main(mapping_csv=args.mapping_csv, dry_run=args.dry_run, limit=args.limit)
