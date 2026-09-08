"""
    Browser-backed downloader for image CDNs that sit behind real
    Cloudflare bot protection, not just hotlink checks.

    tools/download_from_csv.py (the plain urllib version) confirmed this
    is the case for animepahe's image CDN (i.animepahe.pw): it still got
    HTTP 403 with a correct Referer header, which rules out simple
    hotlink protection. That needs an actual browser session - see
    common/downloader.py's download_via_tab() docstring for how this
    reuses pydoll's browser-context `tab.request` client to get past it,
    including the CORS fallback it needs.

    Still deliberately generic (not animepahe-specific): point it at any
    CSV with an id column and a url column, plus a page URL to load first
    so the browser has a real session/cookies for that domain before it
    starts requesting assets.

    Sequential by design, one request at a time - a single tab isn't
    meant to be hammered with concurrent CDP calls, and Cloudflare cares
    about request cadence as much as concurrency (see common/throttle.py).

    Usage:
        python tools/download_images_via_browser.py animepahe/data/images.csv \
            --start-url https://animepahe.pw/ \
            --id-column pahe_id --url-column image_url \
            --dest-dir animepahe/data/images

    Run from the repo root so `common` is importable.
"""

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pydoll.browser.chromium import Chrome  # noqa: E402

from common.browser import build_browser_options  # noqa: E402
from common.cloudflare import resolve_cloudflare_if_present, safe_page_source  # noqa: E402
from common.csv_store import read_csv_rows  # noqa: E402
from common.downloader import download_via_tab  # noqa: E402
from common.throttle import human_delay  # noqa: E402


def _load_rows(csv_path: str, id_column: str, url_column: str) -> list[dict]:
    _, rows = read_csv_rows(csv_path)
    return [row for row in rows if row.get(id_column) and row.get(url_column)]


async def _establish_session(tab, start_url: str) -> bool:
    """
        Load start_url once so the browser has real cookies/session for
        the target domain before any tab.request calls - a bare
        tab.request with no prior navigation would carry no Cloudflare
        clearance at all.

        Returns False if we're still stuck on a Cloudflare challenge
        after a manual-solve attempt - callers should stop rather than
        burn through every row against a page that isn't actually clear.
    """
    await tab.go_to(start_url)
    html = await safe_page_source(tab, retries=3, retry_delay=1.5)
    html = await resolve_cloudflare_if_present(tab, html, context=f"session start ({start_url})")
    return html is not None


async def run(args) -> None:
    rows = _load_rows(args.csv_path, args.id_column, args.url_column)
    print(f"{len(rows)} rows with a URL found in {args.csv_path}")

    dest_dir = Path(args.dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    pending = []
    skipped = 0
    for row in rows:
        row_id = row[args.id_column]
        url = row[args.url_column]
        ext = Path(urlsplit(url).path).suffix or ".jpg"
        dest_path = dest_dir / f"{row_id}{ext}"
        if dest_path.exists():
            skipped += 1
            continue
        pending.append((row_id, url, dest_path))

    print(f"{skipped} already on disk, {len(pending)} to download")
    if not pending:
        return

    options = build_browser_options()
    downloaded = 0
    failed = 0

    async with Chrome(options=options) as browser:
        tab = await browser.start()
        await tab.enable_auto_solve_cloudflare_captcha()

        if not await _establish_session(tab, args.start_url):
            print(f"<error> could not establish a session at {args.start_url} - aborting")
            await tab.disable_auto_solve_cloudflare_captcha()
            return

        total = len(pending)
        for i, (row_id, url, dest_path) in enumerate(pending, start=1):
            await human_delay(args.min_delay, args.max_delay)
            try:
                ok = await download_via_tab(tab, url, str(dest_path))
            except Exception as e:
                print(f"<e> [{i}/{total}] download failed for {row_id}: {e}")
                failed += 1
                continue

            if ok:
                downloaded += 1
                print(f"  [{i}/{total}] downloaded: {row_id} -> {dest_path.name}")
            else:
                failed += 1
                # download_via_tab already printed the specific reason (HTTP
                # status, fetch error, etc.) - this just gives it the same
                # [i/total] framing as a success line so the log reads as one
                # consistent progress stream instead of two different styles.
                print(f"<e> [{i}/{total}] download failed for {row_id}")

        await tab.disable_auto_solve_cloudflare_captcha()

    print(f"Downloaded: {downloaded}  Skipped (already on disk): {skipped}  Failed: {failed}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("csv_path", help="CSV file containing the URLs to download")
    parser.add_argument(
        "--start-url", required=True,
        help="Page to load first so the browser has a real session/cookies for the target domain",
    )
    parser.add_argument("--id-column", default="pahe_id", help="Column used to name each downloaded file")
    parser.add_argument("--url-column", default="image_url", help="Column containing the URL to fetch")
    parser.add_argument("--dest-dir", default="data/images", help="Where to save downloaded files")
    parser.add_argument("--min-delay", type=float, default=2.0, help="Minimum seconds between downloads")
    parser.add_argument("--max-delay", type=float, default=5.0, help="Maximum seconds between downloads")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()
