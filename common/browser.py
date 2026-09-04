"""Shared browser factory for every crawler in this repo."""

from pydoll.browser.options import ChromiumOptions as Options

BRAVE_PATH = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"

def build_browser_options(binary_location: str = BRAVE_PATH, proxy_server: str | None = None ) -> Options:
    """
        Build ChromiumOptions shared across crawlers.

        proxy_server: e.g. "http://user:pass@host:port". Left as None for now
        (v1 has no proxy provider) but any future crawler can pass one in
        without touching this function.
    """
    options = Options()
    options.binary_location = binary_location

    if proxy_server:
        options.add_argument(f"--proxy-server={proxy_server}")

    return options
