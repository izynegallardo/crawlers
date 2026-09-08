"""
    Removes duplicate rows from a CSV, keeping the first occurrence of each
    id and dropping the rest. Generic, not animepahe-specific - point it at
    any CSV plus the column that should be unique.

    Built after a real incident: a leading UTF-8 BOM in a CSV's header
    silently broke common/csv_store.py's resumability check (see that
    file's _load_existing_ids for the fix), so a re-run of animepahe/main.py
    treated every already-scraped anime as new and re-appended it. This
    cleans up rows written under that bug without needing to know in
    advance how many there were.

    Rewrites via a temp file + atomic os.replace, same pattern as
    animepahe/patch_columns.py's _write_rows - never leaves the original
    file half-written if interrupted.

    Usage:
        python tools/dedupe_csv.py animepahe/data/anime.csv --id-column pahe_id
        python tools/dedupe_csv.py animepahe/data/images.csv --id-column pahe_id

    Run from the repo root.
"""

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.csv_store import _normalize_id, read_csv_rows  # noqa: E402


def _write_rows(path: str, fieldnames: list[str], rows: list[dict]) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp_path, path)


def dedupe(path: str, id_column: str, dry_run: bool = False) -> None:
    """
        Compares normalized ids (see common.csv_store._normalize_id), not
        raw strings - confirmed necessary for title-keyed CSVs: the exact
        same title can come back with curly vs straight quotes depending on
        which part of the page it was scraped from, which a plain string
        comparison would treat as two different ids and miss entirely.
    """
    fieldnames, rows = read_csv_rows(path)
    if id_column not in fieldnames:
        raise SystemExit(f"{path} has no column {id_column!r} - found {fieldnames}")

    seen: set[str] = set()
    deduped: list[dict] = []
    duplicate_ids: list[str] = []

    for row in rows:
        value = row.get(id_column)
        key = _normalize_id(value) if value else None
        if key and key in seen:
            duplicate_ids.append(value)
            continue
        if key:
            seen.add(key)
        deduped.append(row)

    print(f"{len(rows)} rows total, {len(duplicate_ids)} duplicate(s) found, {len(deduped)} would remain")

    if not duplicate_ids:
        print("Nothing to do.")
        return

    if dry_run:
        print("--dry-run: no changes written. Duplicate ids:")
        for dup_id in duplicate_ids:
            print(f"  - {dup_id}")
        return

    _write_rows(path, fieldnames, deduped)
    print(f"Rewrote {path}: removed {len(duplicate_ids)} duplicate row(s), kept the first occurrence of each id.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="CSV file to dedupe")
    parser.add_argument("--id-column", default="pahe_id", help="Column that should be unique")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="List which rows would be removed without writing anything.",
    )
    args = parser.parse_args()

    dedupe(args.csv_path, args.id_column, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
