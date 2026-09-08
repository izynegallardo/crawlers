import argparse
import asyncio
import sys

from pydoll.browser.chromium import Chrome

from common.browser import build_browser_options
from common.csv_store import ResumableCsvStore
from common.throttle import human_delay

from .config import CSV_PATH, FIELD_NAMES, IMAGE_URLS_CSV, LETTER_ORDER
from .scraper import ensure_letter_loaded, load_index, scrape_anime


def _normalize_letter(raw: str) -> str:
    return "hash" if raw.lower() == "hash" else raw.upper()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl animepahe.pw and scrape anime data to CSV.")
    parser.add_argument(
        "--start-letter", "-s", default=None,
        help=f"Letter (or 'hash') to start the crawl from, inclusive. One of {LETTER_ORDER}. Default: from the beginning.",
    )
    parser.add_argument(
        "--end-letter", "-e", default=None,
        help=f"Letter (or 'hash') to end the crawl at, inclusive. One of {LETTER_ORDER}. Default: through the end.",
    )
    return parser.parse_args(argv)


def _letters_from(start_letter: str | None, end_letter: str | None) -> list[str]:
    """
        Slice LETTER_ORDER so a crawl can be started and/or stopped at
        given letters without touching load_index() or the loop below - it
        still parses the whole index page (needed regardless, since it's
        one page load), it just skips iterating over letters outside the
        requested range.
    """
    start_index = 0
    end_index = len(LETTER_ORDER) - 1

    if start_letter:
        if start_letter not in LETTER_ORDER:
            raise SystemExit(f"--start-letter must be one of {LETTER_ORDER}, got {start_letter!r}")
        start_index = LETTER_ORDER.index(start_letter)

    if end_letter:
        if end_letter not in LETTER_ORDER:
            raise SystemExit(f"--end-letter must be one of {LETTER_ORDER}, got {end_letter!r}")
        end_index = LETTER_ORDER.index(end_letter)

    if start_index > end_index:
        raise SystemExit(
            f"--start-letter ({LETTER_ORDER[start_index]!r}) comes after "
            f"--end-letter ({LETTER_ORDER[end_index]!r}) in crawl order {LETTER_ORDER}"
        )

    return LETTER_ORDER[start_index:end_index + 1]


async def main(start_letter: str | None = None, end_letter: str | None = None):
    # id_field="title", not "pahe_id": confirmed animepahe's anime slug/UUID
    # is a rotating session identifier, not a permanent one (independently
    # documented by other animepahe tooling - e.g. animepahe-dl's own docs
    # say "the value of anime slug/uuid often changes, not permanent").
    # Matches what we saw directly: re-running the crawl produced 0
    # duplicate pahe_ids but 20 duplicate titles, meaning every re-scrape
    # minted a brand-new pahe_id for anime we'd already scraped. Keying
    # resumability on pahe_id therefore never skips anything on a second
    # run - title is the only field animepahe keeps stable across sessions.
    #
    # Real trade-off, not a free fix: unlike an actual ID, title isn't
    # guaranteed unique - if animepahe ever has two distinct anime sharing
    # an exact title, the second would be silently treated as "already
    # scraped" and skipped. No other stable identifier is exposed by the
    # site, so this is the best option available, not a risk-free one.
    store = ResumableCsvStore(CSV_PATH, FIELD_NAMES, id_field="title")
    image_store = ResumableCsvStore(
        IMAGE_URLS_CSV, ["pahe_id", "image_url"], id_field="pahe_id"
    )
    new_count = 0

    letters_to_process = _letters_from(start_letter, end_letter)

    options = build_browser_options()

    async with Chrome(options=options) as browser:
        tab = await browser.start()
        await tab.enable_auto_solve_cloudflare_captcha()

        await asyncio.sleep(5)

        index = await load_index(tab)

        for letter in letters_to_process:
            entries = await ensure_letter_loaded(tab, letter, index.get(letter, []))
            print(f"[{letter}] {len(entries)} anime found")

            for entry in entries:
                if store.exists(entry["title"]):
                    continue

                await human_delay()

                try:
                    row = await scrape_anime(tab, entry["pahe_id"], entry["url"])
                except Exception as e:
                    print(f"<e> scrape_anime failed for {entry['title']}: {e}")
                    continue

                store.append(row)
                if row.get("image_url"):
                    image_store.append(
                        {"pahe_id": row["pahe_id"], "image_url": row["image_url"]}
                    )
                new_count += 1
                print(f"  scraped: {row.get('title')}")

        await tab.disable_auto_solve_cloudflare_captcha()

    print(f"New anime scraped: {new_count}")
    print(f"Total anime in csv: {store.count()}")


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    start_letter = _normalize_letter(args.start_letter) if args.start_letter else None
    end_letter = _normalize_letter(args.end_letter) if args.end_letter else None
    asyncio.run(main(start_letter, end_letter))
