"""Human-like pacing helpers shared across crawlers."""

import asyncio
import random

async def human_delay(min_seconds: float = 2.0, max_seconds: float = 5.0) -> None:
    """
        Sleep a random interval between page transitions.

        Cloudflare (and most anti-bot layers) fingerprint request cadence as
        much as IP reputation. This keeps page-to-page velocity irregular.
    """
    await asyncio.sleep(random.uniform(min_seconds, max_seconds))
