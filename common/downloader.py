"""
    Simple file downloader shared across crawlers.

    Plain stdlib urllib, run off the event loop via asyncio.to_thread -
    no extra dependency (aiohttp etc.) needed for what is just "save this
    image to disk".
"""

import asyncio
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def download_file(url: str, dest_path: str, referer: str | None = None) -> bool:
    """
        Download url to dest_path. Returns True on success, False on
        failure - a missing/broken image shouldn't kill the crawl.

        referer matters for CDNs with hotlink protection (many will
        403 a request that doesn't claim to come from their own site).
        Try this before assuming you need a full browser - it's a much
        simpler fix if it's all that's blocking the request.

        Confirmed NOT sufficient for animepahe's image CDN
        (i.animepahe.pw): a real run still got HTTP 403 with the correct
        Referer set, which rules out simple hotlink protection and points
        to real Cloudflare-level bot protection instead. Use
        download_via_tab() for that case.
    """
    if not url:
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    headers = {"User-Agent": _DEFAULT_USER_AGENT}
    if referer:
        headers["Referer"] = referer

    def _fetch():
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response, open(dest_path, "wb") as f:
            f.write(response.read())

    try:
        await asyncio.to_thread(_fetch)
        return True
    except urllib.error.HTTPError as e:
        print(f"<warn> download_file got HTTP {e.code} for {url}")
        return False
    except Exception as e:
        print(f"<warn> download_file failed for {url}: {e}")
        return False


async def download_via_tab(tab, url: str, dest_path: str) -> bool:
    """
        Download url using the browser tab's own HTTP client
        (`tab.request`), for hosts that sit behind the same bot-protection
        as the page itself - animepahe's image CDN does (see
        download_file()'s docstring for the confirmed 403-even-with-
        referer result that ruled out a simpler fix).

        `tab.request` is pydoll's browser-context HTTP client
        (https://pydoll.tech/docs/guides/http-requests/): it runs through
        the browser's own fetch() implementation, so it carries the
        browser's real cookies/session/TLS fingerprint automatically -
        that's what should get it past Cloudflare where a bare urllib
        request couldn't.

        One real wrinkle, confirmed by reading pydoll's own source
        (pydoll/browser/requests/request.py): tab.request still executes
        inside the *currently loaded page's* JS context, and its own
        docstring says plainly that it "preserves browser's security
        context and CORS policies". That means a cross-origin call - the
        anime page (animepahe.pw) fetching an image on a different host
        (i.animepahe.pw) - can be blocked by the browser itself if that
        CDN doesn't send an Access-Control-Allow-Origin header for fetch
        reads, which is a separate failure mode from Cloudflare's 403 and
        common for CDNs that only ever expected <img src="..."> usage.

        There's no way to know which wall (if either) we'll hit without a
        live run against the real site, so this tries the cheap path
        first and falls back automatically:

        1. tab.request.get(url) as-is. Works immediately if the CDN
           allows cross-origin fetch reads.
        2. If that raises, navigate the tab directly to that image's own
           host first (a plain page load, not a fetch - navigation isn't
           subject to CORS at all). Retry tab.request from there: it's
           now a same-origin request, which browsers never block for
           CORS. Once that's worked once, the tab stays parked on that
           host, so later calls to other URLs on the same host skip the
           extra navigation and go straight to tab.request.
    """
    if not url:
        return False

    host = urlsplit(url).netloc
    parked_host = getattr(tab, "_download_via_tab_parked_host", None)

    if parked_host != host:
        try:
            response = await tab.request.get(url)
            return _write_response(response, dest_path, url)
        except Exception as e:
            print(
                f"<info> download_via_tab: fetch from the current page failed for {url} "
                f"({e}) - retrying via a same-origin navigation instead"
            )
            try:
                await tab.go_to(url)
            except Exception as nav_e:
                print(f"<warn> download_via_tab: navigation fallback failed for {url}: {nav_e}")
                return False
            setattr(tab, "_download_via_tab_parked_host", host)

    try:
        response = await tab.request.get(url)
    except Exception as e:
        print(f"<warn> download_via_tab request failed for {url}: {e}")
        return False

    return _write_response(response, dest_path, url)


def _write_response(response, dest_path: str, url: str) -> bool:
    if not response.ok:
        print(f"<warn> download_via_tab got HTTP {response.status_code} for {url}")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(response.content)

    return True
