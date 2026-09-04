"""
    Simple file downloader shared across crawlers.

    Plain stdlib urllib, run off the event loop via asyncio.to_thread -
    no extra dependency (aiohttp etc.) needed for what is just "save this
    image to disk".
"""

import asyncio
import base64
import os
import urllib.error
import urllib.request

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
        Download url using the browser tab's own fetch() instead of a
        standalone urllib request.

        Use this instead of download_file() whenever the asset host sits
        behind the same bot-protection as the page (animepahe's image CDN
        does - a bare urllib request gets silently blocked even with a
        normal User-Agent, since it doesn't carry the browser's TLS/JS
        fingerprint or session). Running fetch() inside the already-loaded
        page reuses that trust instead of trying to fake it from outside.
    """
    if not url:
        return False

    script = (
        "return fetch(" + repr(url) + ").then(r => {"
        "if (!r.ok) throw new Error('HTTP ' + r.status);"
        "return r.arrayBuffer();"
        "}).then(buf => {"
        "const bytes = new Uint8Array(buf);"
        "let binary = '';"
        "for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);"
        "return btoa(binary);"
        "});"
    )

    try:
        result = await tab.execute_script(script, return_by_value=True, await_promise=True)
    except Exception as e:
        print(f"<warn> download_via_tab script failed for {url}: {e}")
        return False

    b64_data = _unwrap_script_result(result)
    if not b64_data:
        print(f"<warn> download_via_tab got no data for {url} (raw result: {result!r})")
        return False

    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "wb") as f:
        f.write(base64.b64decode(b64_data))

    return True


def _unwrap_script_result(result):
    """Pydoll's execute_script return shape has varied by version -
    sometimes the raw value, sometimes nested like
    {"result": {"result": {"value": ...}}}. Walk down defensively
    instead of assuming one fixed shape.
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
    return value if isinstance(value, str) else None
