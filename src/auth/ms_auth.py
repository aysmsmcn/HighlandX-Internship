"""Microsoft authentication via azure-identity (OAuth).

Auth layer — no Qt, UI, or service imports here.
"""

from azure.identity import InteractiveBrowserCredential, TokenCachePersistenceOptions

from config import MS_CLIENT_ID, MS_TENANT_ID


_credential: InteractiveBrowserCredential | None = None


def get_credential() -> InteractiveBrowserCredential:
    """Return a shared interactive-browser credential with a persistent token cache.

    Built once and reused for the app's lifetime, so the user signs in at most
    once per session (the persistent cache stores the refresh token in OS-native
    secure storage, so later launches reuse the login too).
    """
    global _credential
    if _credential is None:
        _credential = InteractiveBrowserCredential(
            tenant_id=MS_TENANT_ID,
            client_id=MS_CLIENT_ID,
            cache_persistence_options=TokenCachePersistenceOptions(name="highlandx"),
        )
    return _credential


if __name__ == "__main__":
    cred = get_credential()
    token = cred.get_token("https://graph.microsoft.com/User.Read")
    print("Login OK — token expires at epoch:", token.expires_on)
