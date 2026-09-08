"""
    Resumable CSV writer shared across crawlers.

    Loads whatever ids already exist in the CSV on startup, so a crawl that
    gets stopped (or crashes) can be re-run without rescraping or
    duplicating rows.
"""

import csv
import os
import re
import unicodedata

_PUNCTUATION_RE = re.compile(r"[^\w\s]", flags=re.UNICODE)


def read_csv_rows(path: str) -> tuple[list[str], list[dict]]:
    """
        Read a CSV using utf-8-sig so any stray leading BOM is ignored.

        A BOM on the first header cell silently turns e.g. "pahe_id" into
        "\ufeffpahe_id" and breaks id matching/resume logic exactly the way
        we saw in the real crawler bug; centralized here so every CSV reader
        uses the same fix.
    """
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)
    return fieldnames, rows


def _normalize_id(value: str) -> str:
    """
        Canonicalize a value before using it as a resumability key.

        Confirmed necessary for title-keyed stores (animepahe/main.py uses
        id_field="title" - see that file's docstring for why pahe_id isn't
        stable enough to use instead): the same anime's title can come back
        with different exact characters depending on which part of the page
        it was read from - an index page's anchor title="..." attribute
        versus its own detail page's <h1> text node aren't guaranteed to
        render punctuation identically.

        First version of this function only folded curly quotes to
        straight ones, which fixed most cases but not all - 'Tis Time for
        "Torture," Princess kept getting rescraped every run even after
        that fix, and directly inspecting the stored row showed it already
        used plain straight quotes, so quote-curling wasn't the actual
        mismatch for this one. Rather than keep adding one-off character
        mappings as new mismatches turn up, this strips ALL punctuation and
        lowercases everything - matching should only care about the actual
        words, not which punctuation convention a given page happened to
        use. tools/dedupe_csv.py imports this same function, so both use
        identical matching logic by construction.

        Real trade-off: two titles differing ONLY in punctuation (e.g.
        "Trigun" vs "Trigun!", hypothetically) would now collide. Given the
        alternative was a real anime being silently rescraped forever, this
        is the right side to err on.
    """
    value = unicodedata.normalize("NFKC", value)
    value = value.lower()
    value = _PUNCTUATION_RE.sub("", value)
    return " ".join(value.split())


class ResumableCsvStore:
    def __init__(self, path: str, field_names: list[str], id_field: str):
        self.path = path
        self.field_names = field_names
        self.id_field = id_field
        self._seen_ids: set[str] = set()

        # os.path.dirname returns "" for a bare filename with no directory
        # component (e.g. "mapping.csv" relative to cwd) - os.makedirs("")
        # raises FileNotFoundError, so only call it when there's an actual
        # directory to create.
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._load_existing_ids()

    def _load_existing_ids(self) -> None:
        if not os.path.exists(self.path):
            return

        # utf-8-sig, not utf-8: transparently strips a leading BOM if one
        # is present (e.g. from Excel/VS Code re-saving the file) and is a
        # no-op otherwise. A stray BOM before the header's first column
        # silently turns "pahe_id" into "\ufeffpahe_id", which makes every
        # row.get(id_field) below return None - confirmed as the real cause
        # of a full duplicate re-scrape on 2026-09-06, since _seen_ids ended
        # up empty and every id looked new.
        with open(self.path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                value = row.get(self.id_field)
                if value:
                    self._seen_ids.add(_normalize_id(value))

    def exists(self, id_value: str) -> bool:
        return _normalize_id(id_value) in self._seen_ids

    def append(self, row: dict) -> None:
        write_header = not os.path.exists(self.path)

        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.field_names)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

        self._seen_ids.add(_normalize_id(row[self.id_field]))

    def count(self) -> int:
        return len(self._seen_ids)
