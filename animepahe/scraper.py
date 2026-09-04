"""
    Browser-facing glue: drives the pydoll tab, hands raw HTML to
    parsers.py, gets structured dicts back. No BeautifulSoup/selector
    knowledge lives here - that's parsers.py's job.

    Image downloading is deliberately NOT here - this module only
    records image_url as part of the parsed row (see parsers.py). Actual
    downloading is a separate, reusable concern: see tools/download_from_csv.py.
"""

import asyncio

from .config import INDEX_URL, LETTER_ORDER
from .parsers import normalize_youtube_url, parse_anime_page, parse_index_page

# Substrings that reliably show up in Cloudflare's interstitial/challenge
# HTML ("managed challenge", Turnstile checkbox, or the old "checking your
# browser" page) but never in animepahe's real pages. Confirmed present in
# a real stuck run - see load_index()'s docstring.
_CLOUDFLARE_MARKERS = [
    "just a moment",
    "cf-turnstile",
    "cf_chl_opt",
    "checking your browser",
    "challenges.cloudflare.com",
]


def _has_cloudflare_marker(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in _CLOUDFLARE_MARKERS)


async def _safe_page_source(tab, retries: int = 1, retry_delay: float = 1.0) -> str | None:
    """
        pydoll's `tab.page_source` property (browser/tab.py) does
        `response['result']['result']['value']` with no error checking, so
        it raises a raw KeyError whenever the underlying CDP
        Runtime.evaluate call comes back without a 'value' - which happens
        when the JS execution context is torn down mid-call, e.g. because
        Cloudflare's challenge script is actively navigating/reloading the
        page at that exact moment. That's a pydoll-internal gap we can't
        patch directly, so we catch it here and treat it as "not readable
        right now" instead of crashing the whole crawl over a one-frame
        timing hiccup.
    """
    last_error = None
    for attempt in range(1, retries + 1):
        try:
            return await tab.page_source
        except Exception as e:
            last_error = e
            if attempt < retries:
                await asyncio.sleep(retry_delay)
    print(f"<warn> tab.page_source failed ({type(last_error).__name__}: {last_error})")
    return None


async def _wait_for_cloudflare_clearance(tab, timeout: float = 20.0, poll_interval: float = 1.0) -> bool:
    """
        Cloudflare's "managed challenge" sometimes clears itself after a
        few seconds of the browser just sitting there passively (no click
        needed) - poll page_source for the challenge markers to disappear
        before concluding we're actually stuck and need a human.

        A failed read (see _safe_page_source) is treated as "not cleared
        yet, try again next tick" rather than an error - it's expected
        noise while the challenge page is doing its own thing.
    """
    elapsed = 0.0
    while elapsed < timeout:
        html = await _safe_page_source(tab)
        if html is not None and not _has_cloudflare_marker(html):
            return True
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return False


async def _resolve_cloudflare_if_present(tab, html: str | None, context: str) -> str | None:
    """
        Shared by load_index() and scrape_anime(). Returns the real page
        HTML once we're past any Cloudflare challenge, or None if we're
        still stuck after a manual-solve attempt.

        An unreadable page_source (see _safe_page_source) is treated the
        same as a detected challenge - we don't actually know what's on
        the page in that case, so waiting/prompting is the safer default
        over assuming it's fine.
    """
    if html is not None and not _has_cloudflare_marker(html):
        return html

    reason = "page_source unreadable" if html is None else "Cloudflare challenge page detected"
    print(f"<warn> {reason} ({context}). Waiting up to 20s to see if it clears on its own...")
    cleared = await _wait_for_cloudflare_clearance(tab, timeout=20.0)

    if cleared:
        print("<info> Cleared on its own.")
    else:
        print(
            "<warn> Did not clear automatically after 20s. This matches a known pydoll "
            "limitation with iframe-based challenges - please solve it by hand in the "
            "visible browser window now."
        )
        input("Press Enter here once you've resolved it in the browser window... ")
        await asyncio.sleep(2)

    html = await _safe_page_source(tab, retries=3, retry_delay=1.5)
    if html is None or _has_cloudflare_marker(html):
        print(f"<error> Still stuck ({context}) after manual attempt.")
        return None
    return html


async def load_index(tab) -> dict[str, list[dict]]:
    """
        Confirmed via devtools: the letter nav uses the old Bootstrap 4 tab
        plugin (`data-toggle="tab"`, href="#A" etc pointing at the pane's
        id). That plugin only toggles .active/.show classes on click - it
        does not fetch or lazy-load anything. So on a normal load, every
        letter's anime list is already present in the page HTML.

        Confirmed via a real run: the "[A] 0 found" symptom can also be
        caused by Cloudflare serving a "Just a moment..." challenge page
        instead of the real index - enable_auto_solve_cloudflare_captcha()
        doesn't reliably clear this (documented pydoll limitation, see
        https://github.com/autoscrape-labs/pydoll/issues/272). See
        _resolve_cloudflare_if_present() for how that's handled.
    """
    await tab.go_to(INDEX_URL)

    current_url = await tab.current_url
    print(f"[index] current_url = {current_url}")

    html = await _safe_page_source(tab, retries=3, retry_delay=1.5)
    html = await _resolve_cloudflare_if_present(tab, html, context="index page")
    if html is None:
        print("<error> Aborting index load.")
        return {}

    total_links = await _wait_for_anime_link_count_to_settle(tab)
    print(f"[index] settled with {total_links} anime links on the page")

    html = await _safe_page_source(tab, retries=3, retry_delay=1.5) or html
    print(f"[index] page_source length = {len(html)} chars")

    letters = parse_index_page(html)

    # Diagnostic - confirms the fix actually worked, letter by letter.
    for letter in LETTER_ORDER:
        print(f"[index] {letter}: {len(letters.get(letter, []))} parsed after load")

    return letters


async def _count_anime_links(tab) -> int | None:
    script = "return document.querySelectorAll(\"a[href^='/anime/']\").length;"
    try:
        result = await tab.execute_script(script, return_by_value=True)
    except Exception as e:
        print(f"<warn> _count_anime_links script failed: {e}")
        return None
    value = _unwrap_script_value(result)
    return value if isinstance(value, int) else None


async def _wait_for_anime_link_count_to_settle(
    tab, timeout: float = 25.0, poll_interval: float = 0.75, stable_reads: int = 3
) -> int:
    """
        Poll the total `a[href^='/anime/']` count on the page until it's
        unchanged for `stable_reads` consecutive checks, or until timeout.
        Works whether the page is server-rendered up front, lazy-loads in
        the background, or renders progressively - all of those converge
        on "the count stops going up".
    """
    last_count = -1
    stable = 0
    elapsed = 0.0

    while elapsed < timeout:
        count = await _count_anime_links(tab)
        if count is None:
            count = last_count if last_count != -1 else 0

        if count == last_count:
            stable += 1
            if stable >= stable_reads:
                return count
        else:
            stable = 0
            last_count = count

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return last_count if last_count != -1 else 0


def _unwrap_script_value(result):
    """Same defensive unwrap as common/downloader.py's
    _unwrap_script_result, but without forcing the result to be a str -
    we need the raw int here.
    """
    value = result
    for _ in range(3):
        if isinstance(value, dict) and "value" in value:
            value = value["value"]
            break
        if isinstance(value, dict) and "result" in value:
            value = value["result"]
            continue
        break
    return value


async def ensure_letter_loaded(tab, letter: str, entries: list[dict]) -> list[dict]:
    """
        True backstop only. Since letter tabs use the old Bootstrap 4 tab
        plugin (plain class toggling, confirmed via devtools - see
        load_index()), every letter's data should already be in `entries`
        by the time this is called. This path exists purely in case a
        letter is genuinely missing from the page for some other reason.

        The nav-link selector below is now confirmed against real markup:
        `<a class="nav-link" data-toggle="tab" href="#B" role="tab">B</a>`.
    """
    if entries:
        return entries

    nav_link = await tab.find(
        tag_name="a", class_name="nav-link", href=f"#{letter}", raise_exc=False
    )
    if not nav_link:
        print(f"<warn> ensure_letter_loaded: no nav-link found for #{letter}")
        return entries

    await nav_link.click(humanize=True)
    settled_count = await _wait_for_pane_link_count_to_settle(tab, letter)
    print(f"<info> ensure_letter_loaded: #{letter} settled with {settled_count} links after click")

    html = await tab.page_source
    return parse_index_page(html).get(letter, [])


async def _wait_for_pane_link_count_to_settle(
    tab, letter: str, timeout: float = 12.0, poll_interval: float = 0.5, stable_reads: int = 2
) -> int:
    script = (
        "const p = document.getElementById(" + repr(letter) + ");"
        "return p ? p.querySelectorAll(\"a[href^='/anime/']\").length : -1;"
    )
    last_count = -2
    stable = 0
    elapsed = 0.0

    while elapsed < timeout:
        try:
            result = await tab.execute_script(script, return_by_value=True)
        except Exception as e:
            print(f"<warn> _wait_for_pane_link_count_to_settle script failed: {e}")
            result = None
        count = _unwrap_script_value(result)
        count = count if isinstance(count, int) else last_count

        if count == last_count:
            stable += 1
            if stable >= stable_reads:
                return max(count, 0)
        else:
            stable = 0
            last_count = count

        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

    return max(last_count, 0)


async def scrape_anime(tab, pahe_id: str, url: str) -> dict:
    """
        Summary, Relations, and Recommendations are all present in one
        page load (they're CSS-toggled tab-content, not separately fetched),
        so a single go_to() + page_source covers all three. The trailer is
        the exception - it only exists once the .youtube-preview anchor is
        clicked, so that's a separate step.

        Same Cloudflare check as load_index() - a mid-crawl challenge on an
        individual anime page would otherwise silently produce a row full
        of Nones instead of a clear signal something's wrong.
    """
    await tab.go_to(url)
    html = await tab.page_source

    if _has_cloudflare_marker(html):
        print(f"<warn> Cloudflare challenge page detected while scraping {url}. Waiting up to 20s...")
        cleared = await _wait_for_cloudflare_clearance(tab, timeout=20.0)

        if cleared:
            print("<info> Cloudflare challenge cleared on its own.")
        else:
            print(
                "<warn> Cloudflare challenge did not clear automatically after 20s. "
                "Please solve it by hand in the browser window now."
            )
            input("Press Enter here once you've solved the Cloudflare challenge in the browser window... ")
            await asyncio.sleep(2)

        html = await tab.page_source
        if _has_cloudflare_marker(html):
            raise RuntimeError(f"still on a Cloudflare challenge page for {url} after manual attempt")

    row = parse_anime_page(html, pahe_id=pahe_id)

    row["youtube_url"] = await _extract_trailer_url(tab)

    return row


async def _extract_trailer_url(tab) -> str | None:
    preview_link = await tab.find(class_name="youtube-preview", raise_exc=False)
    if not preview_link:
        return None

    await preview_link.click(humanize=True)

    # Popup fetches and injects the iframe async - give it a few seconds
    # rather than a fixed sleep.
    iframe = await tab.find(class_name="mfp-iframe", timeout=5, raise_exc=False)
    if not iframe:
        return None

    return normalize_youtube_url(iframe.get_attribute("src"))
