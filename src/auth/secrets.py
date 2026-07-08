import keyring

SERVICE = "highlandx"
AFFINITY_API_KEY = "affinity_api_key"

def set_secret(name: str, value: str) -> None:
    keyring.set_password(SERVICE, name, value)


def get_secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)