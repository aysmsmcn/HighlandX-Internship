"""Microsoft authentication via azure-identity (OAuth).

Auth layer — no Qt, UI, or service imports here.

The interactive browser credential is synchronous (azure-identity has no async
interactive credential). Calling its blocking get_token directly on the qasync
event loop freezes the UI during sign-in. So callers should `await
ensure_authenticated()` first, which runs the (possibly browser-based) sign-in
in a worker thread — the loop stays responsive, and a cancelled sign-in fails
after `timeout` seconds instead of hanging the app for ~5 minutes.
"""

import asyncio

from azure.identity import InteractiveBrowserCredential, TokenCachePersistenceOptions

from config import MS_CLIENT_ID, MS_TENANT_ID

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_credential: InteractiveBrowserCredential | None = None


def get_credential() -> InteractiveBrowserCredential:
    """Return a shared interactive-browser credential with a persistent token cache.

    Built once and reused for the app's lifetime, so the user signs in at most
    once per session (the persistent cache also carries the login across launches).
    """
    global _credential
    if _credential is None:
        _credential = InteractiveBrowserCredential(
            tenant_id=MS_TENANT_ID,
            client_id=MS_CLIENT_ID,
            cache_persistence_options=TokenCachePersistenceOptions(name="highlandx"),
            timeout=120,   # seconds to wait for sign-in before giving up
        )
    return _credential

def sign_out() -> None:
    """Forget the in-memory credential so the next sign-in re-prompts.

    Note: this clears the session-cached credential, not the on-disk MSAL
    token cache (azure-identity exposes no clean API for that), so the OS may
    still silently re-authenticate the same account. Switching accounts is
    available at the next interactive prompt.
    """
    global _credential
    _credential = None


async def ensure_authenticated() -> None:
    """Acquire a Graph token off the event loop, so interactive sign-in never freezes the UI.

    Call before any Graph request: if a fresh sign-in is needed the browser-wait
    happens in a worker thread (UI stays responsive); if a cached token already
    exists this returns quickly. Raises on failure/cancellation so callers can
    surface a message instead of hanging.
    """
    cred = get_credential()
    await asyncio.to_thread(cred.get_token, _GRAPH_SCOPE)


if __name__ == "__main__":
    async def _test() -> None:
        await ensure_authenticated()
        token = await asyncio.to_thread(get_credential().get_token, _GRAPH_SCOPE)
        print("Login OK — token expires at epoch:", token.expires_on)

    asyncio.run(_test())
