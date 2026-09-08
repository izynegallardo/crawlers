"""
    One-off diagnostic: inspects a CSV for a given pahe_id or title
    substring and prints the exact repr() and Unicode codepoints of the
    title field, plus how many rows match. Used to debug a resumability
    mismatch that plain visual inspection of terminal/file output can't
    reliably catch, since terminal fonts and CSV escaping can both make
    two different underlying strings look identical.

    Usage:
        python tools/debug_title_match.py animepahe/data/anime.csv --pahe-id f9fe1293-464b-93a0-6b2c-6444fa09e229
        python tools/debug_title_match.py animepahe/data/anime.csv --contains "Tis Time"

    Run from the repo root.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.csv_store import _normalize_id, read_csv_rows  # noqa: E402


def _describe(label: str, value: str) -> None:
    print(f"{label}:")
    print(f"  repr:       {value!r}")
    print(f"  length:     {len(value)}")
    print(f"  normalized: {_normalize_id(value)!r}")
    codepoints = " ".join(f"U+{ord(c):04X}({c!r})" for c in value)
    print(f"  codepoints: {codepoints}")
    print()


def main(csv_path: str, pahe_id: str | None, contains: str | None) -> None:
    fieldnames, rows = read_csv_rows(csv_path)
    print(f"Header fieldnames ({len(fieldnames)}): {fieldnames}")
    print()

    print(f"{len(rows)} total rows loaded by csv.DictReader")
    print()

    matches = []
    for i, row in enumerate(rows):
        title = row.get("title") or ""
        row_pahe_id = row.get("pahe_id") or ""
        if pahe_id and row_pahe_id == pahe_id:
            matches.append((i, row))
        elif contains and contains.lower() in title.lower():
            matches.append((i, row))

    print(f"{len(matches)} matching row(s) found\n")

    for i, row in matches:
        print(f"--- row index {i} (0-based, after header) ---")
        print(f"  pahe_id: {row.get('pahe_id')!r}")
        _describe("  title", row.get("title") or "")

    # Cross-check: does the normalized title collide with any OTHER row's
    # normalized title in a way that might indicate duplicate/corrupted
    # entries csv.DictReader silently merged or split incorrectly?
    if matches:
        target_norm = _normalize_id(matches[0][1].get("title") or "")
        same_norm = [
            (i, row) for i, row in enumerate(rows)
            if row.get("title") and _normalize_id(row["title"]) == target_norm
        ]
        print(f"Rows whose normalized title equals the first match's normalized title: {len(same_norm)}")
        for i, row in same_norm:
            print(f"  row {i}: pahe_id={row.get('pahe_id')!r} title={row.get('title')!r}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path")
    parser.add_argument("--pahe-id", default=None)
    parser.add_argument("--contains", default=None)
    args = parser.parse_args()

    if not args.pahe_id and not args.contains:
        raise SystemExit("Provide --pahe-id or --contains")

    main(args.csv_path, args.pahe_id, args.contains)
