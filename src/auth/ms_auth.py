"""Microsoft authentication via azure-identity (OAuth).

Auth layer — no Qt, UI, or service imports here.
"""

from azure.identity import InteractiveBrowserCredential, TokenCachePersistenceOptions

from config import MS_CLIENT_ID, MS_TENANT_ID


def get_credential() -> InteractiveBrowserCredential:
    """Return an interactive-browser credential with a persistent token cache.

    The cache stores the refresh token in OS-native secure storage, so the user
    is only prompted to sign in once; later launches reuse the cached login.
    """
    return InteractiveBrowserCredential(
        tenant_id=MS_TENANT_ID,
        client_id=MS_CLIENT_ID,
        cache_persistence_options=TokenCachePersistenceOptions(name="highlandx"),
    )


if __name__ == "__main__":
    cred = get_credential()
    token = cred.get_token("https://graph.microsoft.com/User.Read")
    print("Login OK — token expires at epoch:", token.expires_on)
