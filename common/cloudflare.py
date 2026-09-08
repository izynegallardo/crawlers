"""
    Cloudflare challenge detection/handling shared across every crawler in
    this repo, not just animepahe. Nothing in here is site-specific: the
    markers are generic Cloudflare interstitial strings, and the wait/
    resolve helpers only need a pydoll `tab`.

    Originally lived inside animepahe/scraper.py - pulled out here once a
    second consumer (the browser-backed image downloader) needed the same
    "wait for Cloudflare to clear, prompt for a manual solve if it
    doesn't" logic. Behavior is unchanged from the animepahe version.
"""

import asyncio

# Substrings that reliably show up in Cloudflare's interstitial/challenge
# HTML ("managed challenge", Turnstile checkbox, or the old "checking your
# browser" page) but never in a real page. Confirmed present in a real
# stuck run against animepahe.pw - see resolve_cloudflare_if_present()'s
# docstring.
CLOUDFLARE_MARKERS = [
    "just a moment",
    "cf-turnstile",
    "cf_chl_opt",
    "checking your browser",
    "challenges.cloudflare.com",
]


def has_cloudflare_marker(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in CLOUDFLARE_MARKERS)


async def safe_page_source(tab, retries: int = 1, retry_delay: float = 1.0) -> str | None:
    """
        pydoll's `tab.page_source` property (browser/tab.py) does
        `response['result']['result']['value']` with no error checking, so
        it raises a raw KeyError whenever the underlying CDP
        Runtime.evaluate call comes back without a 'value' - which happens
        when the JS execution context is torn down mid-call, e.g. because
        Cloudflare's challenge script is actively navigating/reloading the
        page at that exact moment. That's a pydoll-internal gap we can't
        patch directly, so we catch it here and treat it as "not readable
        right now" instead of crashing the whole run over a one-frame
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


async def wait_for_cloudflare_clearance(tab, timeout: float = 20.0, poll_interval: float = 1.0) -> bool:
    """
        Cloudflare's "managed challenge" sometimes clears itself after a
        few seconds of the browser just sitting there passively (no click
        needed) - poll page_source for the challenge markers to disappear
        before concluding we're actually stuck and need a human.

        A failed read (see safe_page_source) is treated as "not cleared
        yet, try again next tick" rather than an error - it's expected
        noise while the challenge page is doing its own thing.
    """
    elapsed = 0.0
    while elapsed < timeout:
        html = await safe_page_source(tab)
        if html is not None and not has_cloudflare_marker(html):
            return True
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    return False


async def resolve_cloudflare_if_present(tab, html: str | None, context: str) -> str | None:
    """
        Returns the real page HTML once we're past any Cloudflare
        challenge, or None if we're still stuck after a manual-solve
        attempt.

        An unreadable page_source (see safe_page_source) is treated the
        same as a detected challenge - we don't actually know what's on
        the page in that case, so waiting/prompting is the safer default
        over assuming it's fine.
    """
    if html is not None and not has_cloudflare_marker(html):
        return html

    reason = "page_source unreadable" if html is None else "Cloudflare challenge page detected"
    print(f"<warn> {reason} ({context}). Waiting up to 20s to see if it clears on its own...")
    cleared = await wait_for_cloudflare_clearance(tab, timeout=20.0)

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

    html = await safe_page_source(tab, retries=3, retry_delay=1.5)
    if html is None or has_cloudflare_marker(html):
        print(f"<error> Still stuck ({context}) after manual attempt.")
        return None
    return html
