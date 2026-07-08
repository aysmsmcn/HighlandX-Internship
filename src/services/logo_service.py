"""Company logos, keyed by domain, via Google's public favicon service.

Affinity's API doesn't expose a logo field, but every company has a domain, so
this fetches a small icon from a domain-keyed public service instead. Results
are cached to disk (config.LOGOS_DIR) so each domain is only fetched once
across the app's lifetime — the UI decides how to render the bytes.
"""

import re

import httpx

from config import LOGOS_DIR

FAVICON_URL = "https://www.google.com/s2/favicons?domain={domain}&sz=64"


def _cache_path(domain: str):
    safe = re.sub(r"[^a-zA-Z0-9.\-]", "_", domain)
    return LOGOS_DIR / f"{safe}.png"


async def get_logo_bytes(domain: str | None) -> bytes | None:
    """Logo image bytes for a domain, from disk cache or a fresh fetch, or None."""
    if not domain:
        return None
    path = _cache_path(domain)
    if path.exists():
        data = path.read_bytes()
        return data or None    # empty file = a cached "no logo" miss

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        try:
            resp = await client.get(FAVICON_URL.format(domain=domain))
            resp.raise_for_status()
            data = resp.content
        except Exception:
            data = b""

    path.write_bytes(data)     # cache the miss too (empty file) so it isn't retried every launch
    return data or None
