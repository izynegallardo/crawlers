import asyncio

from pydoll.browser.chromium import Chrome

from common.browser import build_browser_options
from common.csv_store import ResumableCsvStore
from common.throttle import human_delay

from .config import CSV_PATH, FIELD_NAMES, IMAGE_URLS_CSV, LETTER_ORDER
from .scraper import ensure_letter_loaded, load_index, scrape_anime


async def main():
    store = ResumableCsvStore(CSV_PATH, FIELD_NAMES, id_field="pahe_id")
    image_store = ResumableCsvStore(
        IMAGE_URLS_CSV, ["pahe_id", "image_url"], id_field="pahe_id"
    )
    new_count = 0

    options = build_browser_options()

    async with Chrome(options=options) as browser:
        tab = await browser.start()
        await tab.enable_auto_solve_cloudflare_captcha()

        await asyncio.sleep(5)

        index = await load_index(tab)

        for letter in LETTER_ORDER:
            entries = await ensure_letter_loaded(tab, letter, index.get(letter, []))
            print(f"[{letter}] {len(entries)} anime found")

            for entry in entries:
                if store.exists(entry["pahe_id"]):
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
    asyncio.run(main())
