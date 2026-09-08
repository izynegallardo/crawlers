"""
    Browser-facing glue: drives the pydoll tab, hands raw HTML to
    parsers.py, gets structured dicts back. No BeautifulSoup/selector
    knowledge lives here - that's parsers.py's job.

    Image downloading is deliberately NOT here - this module only
    records image_url as part of the parsed row (see parsers.py). Actual
    downloading is a separate, reusable concern: see tools/download_from_csv.py.
"""

import asyncio

from common.cloudflare import resolve_cloudflare_if_present, safe_page_source

from .config import INDEX_URL, LETTER_ORDER
from .parsers import normalize_youtube_url, parse_anime_page, parse_index_page, parse_patch_fields

# Cloudflare detection/waiting/manual-solve logic now lives in
# common/cloudflare.py - it was never animepahe-specific (the markers are
# generic Cloudflare interstitial strings), and the browser-backed image
# downloader (tools/download_images_via_browser.py) needs the same logic.
# See that module's docstring for the pydoll `page_source` KeyError
# reasoning this used to document locally.
_safe_page_source = safe_page_source
_resolve_cloudflare_if_present = resolve_cloudflare_if_present


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
    try:
        html = await _load_page_html(tab, INDEX_URL, context="index page", ready_marker="tab-pane")
    except RuntimeError as e:
        print(f"<error> {e}")
        return {}

    current_url = await tab.current_url
    print(f"[index] current_url = {current_url}")

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
    """
    html = await _load_page_html(tab, url, context=url, ready_marker="anime-info")
    row = parse_anime_page(html, pahe_id=pahe_id)

    row["youtube_url"] = await _extract_trailer_url(tab)

    return row


async def scrape_patch_fields(tab, url: str) -> dict:
    """
        Used by the column-patch migration tool to backfill studios/
        themes/genres/demographics/external_links on rows scraped before
        those became list/object-array fields (see parsers.PATCH_FIELDS).

        Reuses the same page load + Cloudflare handling as scrape_anime(),
        but skips relations/recommendations/summary parsing and the
        trailer click entirely - none of that is being patched, so there's
        no reason to pay for it.
    """
    html = await _load_page_html(tab, url, context=url, ready_marker="anime-info")
    return parse_patch_fields(html)


async def _load_page_html(tab, url: str, context: str, ready_marker: str | None = None) -> str:
    """
        Shared by load_index(), scrape_anime(), and scrape_patch_fields():
        navigate, read page_source defensively (see _safe_page_source), and
        resolve any Cloudflare challenge before handing back real HTML.
        Raises if we're still stuck after a manual-solve attempt, so
        callers' per-item try/except (see main.py, patch_columns.py) can
        skip just that one entry instead of crashing the whole run.

        ready_marker: an HTML substring (a CSS class name unique to real
        content, e.g. "anime-info") that must be present before the result
        is trusted. Needed because clearing a Cloudflare challenge doesn't
        guarantee the real page has finished rendering in that same
        instant - confirmed via a real run: the first anime scraped right
        after a Cloudflare clear came back with a completely empty sidebar
        (every field, not just one), despite the anime genuinely having
        data, because we read page_source in the brief gap between "no
        challenge markers" and "real content actually present".
    """
    await tab.go_to(url)
    html = await _safe_page_source(tab, retries=3, retry_delay=1.5)
    html = await _resolve_cloudflare_if_present(tab, html, context=context)
    if html is None:
        raise RuntimeError(f"still on a Cloudflare challenge page for {url} after manual attempt")

    if ready_marker and ready_marker not in html:
        settled_html = await _wait_for_marker(tab, ready_marker, context=context)
        if settled_html is not None:
            html = settled_html

    return html


async def _wait_for_marker(
    tab, marker: str, context: str, timeout: float = 10.0, poll_interval: float = 0.5
) -> str | None:
    """
        Poll page_source until `marker` (a plain substring) appears, or
        give up after `timeout`. Returns None on timeout - callers fall
        back to whatever html they already had, since a missing marker
        after a real timeout usually means the page genuinely lacks that
        section, not that it's still loading (by then it's had far longer
        than a normal page load takes).
    """
    elapsed = 0.0
    while elapsed < timeout:
        html = await _safe_page_source(tab)
        if html is not None and marker in html:
            return html
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    print(f"<warn> marker {marker!r} did not appear within {timeout}s ({context}) - proceeding with what we have")
    return None


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
