"""Company logos, keyed by domain, via public favicon services.

Affinity's API doesn't expose a logo field, but every company has a domain, so
this fetches a small icon from domain-keyed public services instead. Results are
cached to disk (config.LOGOS_DIR) so each domain is only fetched once across the
app's lifetime — the UI decides how to render the bytes.

Providers are tried in order (a fallback chain): Google's favicon service is
fast/reliable but misses some domains entirely (404), so DuckDuckGo and icon.horse
back it up — they often have logos Google lacks.
"""

import re

import httpx

from config import LOGOS_DIR

# Tried in order; first one to return image bytes wins. {domain} is substituted.
FAVICON_PROVIDERS = (
    "https://www.google.com/s2/favicons?domain={domain}&sz=64",
    "https://icons.duckduckgo.com/ip3/{domain}.ico",
    "https://icon.horse/icon/{domain}",
)


def _cache_path(domain: str):
    safe = re.sub(r"[^a-zA-Z0-9.\-]", "_", domain)
    return LOGOS_DIR / f"{safe}.png"


async def get_logo_bytes(domain: str | None) -> bytes | None:
    """Logo image bytes for a domain, from disk cache or a fresh fetch, or None.

    Tries each provider in FAVICON_PROVIDERS until one returns image bytes; caches
    the result (an empty file for a total miss, so it isn't retried every launch)."""
    if not domain:
        return None
    path = _cache_path(domain)
    if path.exists():
        data = path.read_bytes()
        return data or None    # empty file = a cached "no logo" miss

    data = b""
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for provider in FAVICON_PROVIDERS:
            try:
                resp = await client.get(provider.format(domain=domain))
                resp.raise_for_status()
                if resp.content and resp.headers.get("content-type", "").startswith("image/"):
                    data = resp.content
                    break
            except Exception:
                continue       # provider failed/404 → try the next one

    path.write_bytes(data)     # cache the miss too (empty file) so it isn't retried every launch
    return data or None
