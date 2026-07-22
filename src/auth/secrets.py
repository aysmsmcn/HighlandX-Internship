import keyring

SERVICE = "highlandx"
AFFINITY_API_KEY = "affinity_api_key"
ANTHROPIC_API_KEY = "anthropic_api_key"   # for the Haiku score-extraction call
RAYLU_MCP_URL = "raylu_mcp_url"            # Raylu's hosted MCP endpoint
RAYLU_OAUTH_TOKENS = "raylu_oauth_tokens"  # OAuth access/refresh tokens (JSON)
RAYLU_OAUTH_CLIENT = "raylu_oauth_client"  # dynamic client registration (JSON)

def set_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def get_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)