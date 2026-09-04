"""
    Resumable CSV writer shared across crawlers.

    Loads whatever ids already exist in the CSV on startup, so a crawl that
    gets stopped (or crashes) can be re-run without rescraping or
    duplicating rows.
"""

import csv
import os

class ResumableCsvStore:
    def __init__(self, path: str, field_names: list[str], id_field: str):
        self.path = path
        self.field_names = field_names
        self.id_field = id_field
        self._seen_ids: set[str] = set()

        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._load_existing_ids()

    def _load_existing_ids(self) -> None:
        if not os.path.exists(self.path):
            return

        with open(self.path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                value = row.get(self.id_field)
                if value:
                    self._seen_ids.add(value)

    def exists(self, id_value: str) -> bool:
        return id_value in self._seen_ids

    def append(self, row: dict) -> None:
        write_header = not os.path.exists(self.path)

        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=self.field_names)
            if write_header:
                writer.writeheader()
            writer.writerow(row)

        self._seen_ids.add(row[self.id_field])

    def count(self) -> int:
        return len(self._seen_ids)
