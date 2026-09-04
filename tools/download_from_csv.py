"""
    Reusable, standalone downloader.

    Not tied to animepahe or any specific crawler on purpose: point it at
    any CSV that has an id column and a url column, and it downloads
    whatever's in the url column to <dest-dir>/<id>.<ext>. Anything already
    on disk is skipped, so it's safe to re-run or resume.

    Usage:
        python tools/download_from_csv.py animepahe/data/image_urls.csv \
            --id-column pahe_id --url-column image_url \
            --dest-dir animepahe/data/images \
            --referer https://animepahe.pw/

    Run from the repo root so `common` is importable.
"""

import argparse
import asyncio
import csv
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.downloader import download_file  # noqa: E402
from common.throttle import human_delay  # noqa: E402


def _load_rows(csv_path: str, id_column: str, url_column: str) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [row for row in reader if row.get(id_column) and row.get(url_column)]


async def _worker(row, id_column, url_column, dest_dir, referer, semaphore, delay_range, counters):
    row_id = row[id_column]
    url = row[url_column]
    ext = Path(urlsplit(url).path).suffix or ".jpg"
    dest_path = Path(dest_dir) / f"{row_id}{ext}"

    if dest_path.exists():
        counters["skipped"] += 1
        return

    async with semaphore:
        await human_delay(*delay_range)
        ok = await download_file(url, str(dest_path), referer=referer)

    counters["downloaded" if ok else "failed"] += 1


async def run(args) -> None:
    rows = _load_rows(args.csv_path, args.id_column, args.url_column)
    print(f"{len(rows)} rows with a URL found in {args.csv_path}")

    Path(args.dest_dir).mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(args.concurrency)
    counters = {"downloaded": 0, "skipped": 0, "failed": 0}

    await asyncio.gather(
        *(
            _worker(
                row,
                args.id_column,
                args.url_column,
                args.dest_dir,
                args.referer,
                semaphore,
                (args.min_delay, args.max_delay),
                counters,
            )
            for row in rows
        )
    )

    print(
        f"Downloaded: {counters['downloaded']}  "
        f"Skipped (already on disk): {counters['skipped']}  "
        f"Failed: {counters['failed']}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="CSV file containing the URLs to download")
    parser.add_argument("--id-column", default="pahe_id", help="Column used to name each downloaded file")
    parser.add_argument("--url-column", default="image_url", help="Column containing the URL to fetch")
    parser.add_argument("--dest-dir", default="data/images", help="Where to save downloaded files")
    parser.add_argument("--referer", default=None, help="Referer header to send - many CDNs require it")
    parser.add_argument("--concurrency", type=int, default=4, help="Max downloads in flight at once")
    parser.add_argument("--min-delay", type=float, default=1.0, help="Minimum seconds between a slot's downloads")
    parser.add_argument("--max-delay", type=float, default=2.5, help="Maximum seconds between a slot's downloads")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
