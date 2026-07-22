"""Raylu enrichment via a local MCP client with OAuth.

Service layer: the UI calls authorize()/enrich() and never touches MCP or the
Anthropic SDK directly.

Flow: connect to Raylu's hosted MCP over Streamable HTTP, authenticating with the
MCP OAuth spec — the `mcp` SDK handles discovery, dynamic client registration, the
browser authorize step, and token refresh. Tokens + client registration persist in
keyring. We call get_company (raw output) and get_score_breakdown directly, then use
Haiku 4.5 to pull just the score out of the breakdown.

Requires (Settings → Credentials): the Raylu MCP URL, a one-time "Authorize Raylu",
and an Anthropic API key.
"""

import asyncio
import webbrowser
from dataclasses import dataclass
from urllib.parse import urlparse, parse_qs

from auth.secrets import (get_secret, set_secret, ANTHROPIC_API_KEY,
                          RAYLU_MCP_URL, RAYLU_OAUTH_TOKENS, RAYLU_OAUTH_CLIENT)

MODEL = "claude-haiku-4-5"
SCORING_DEFINITION = "Highland Capital Partners Deal Score"
_CALLBACK_PORT = 33418
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}/callback"


@dataclass
class Usage:
    input: int
    output: int

    @property
    def total(self) -> int:
        return self.input + self.output


@dataclass
class EnrichResult:
    text: str
    usage: Usage


class _KeyringTokenStorage:
    """Persists the OAuth tokens + dynamic client registration in keyring."""

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        raw = get_secret(RAYLU_OAUTH_TOKENS)
        return OAuthToken.model_validate_json(raw) if raw else None

    async def set_tokens(self, tokens) -> None:
        set_secret(RAYLU_OAUTH_TOKENS, tokens.model_dump_json())

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        raw = get_secret(RAYLU_OAUTH_CLIENT)
        return OAuthClientInformationFull.model_validate_json(raw) if raw else None

    async def set_client_info(self, client_info) -> None:
        set_secret(RAYLU_OAUTH_CLIENT, client_info.model_dump_json())


async def _redirect_handler(auth_url: str) -> None:
    """Open the system browser so the user can approve access."""
    webbrowser.open(auth_url)


async def _callback_handler() -> tuple[str, str | None]:
    """One-shot localhost server that catches the OAuth redirect and reads the code."""
    loop = asyncio.get_event_loop()
    fut: asyncio.Future = loop.create_future()

    async def handle(reader, writer):
        try:
            request_line = await reader.readline()   # GET /callback?code=..&state=.. HTTP/1.1
            target = request_line.decode("latin1").split(" ")[1]
            qs = parse_qs(urlparse(target).query)
            code = (qs.get("code") or [""])[0]
            state = (qs.get("state") or [None])[0]
            body = b"<html><body>Raylu authorized. You can close this tab.</body></html>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
            writer.close()
            if not fut.done():
                fut.set_result((code, state))
        except Exception as e:                       # surface parse/IO errors to the awaiter
            if not fut.done():
                fut.set_exception(e)

    server = await asyncio.start_server(handle, "127.0.0.1", _CALLBACK_PORT)
    async with server:
        return await fut


def _make_provider(url: str):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata
    metadata = OAuthClientMetadata(
        client_name="HighlandX",
        redirect_uris=[_REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",   # public client (no secret)
    )
    return OAuthClientProvider(
        server_url=url,
        client_metadata=metadata,
        storage=_KeyringTokenStorage(),
        redirect_handler=_redirect_handler,
        callback_handler=_callback_handler,
    )


def _require(*pairs) -> None:
    missing = [label for label, val in pairs if not val]
    if missing:
        raise RuntimeError("Missing: " + ", ".join(missing)
                           + ".\nSet them under Settings → Credentials.")


async def authorize() -> None:
    """Run the OAuth flow (opens the browser). Call once from Settings before enriching."""
    url = get_secret(RAYLU_MCP_URL)
    _require(("Raylu MCP URL", url))
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    provider = _make_provider(url)
    async with streamablehttp_client(url, auth=provider) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()          # forces auth; tokens land in keyring


def _result_text(result) -> str:
    """Flatten a CallToolResult's content blocks into text."""
    parts = []
    for item in getattr(result, "content", None) or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else str(item))
    return "\n".join(parts)


async def enrich(name: str, domain: str | None) -> EnrichResult:
    """Run get_company + get_score_breakdown through Raylu's MCP and return the raw
    company output plus the deal score (extracted by Haiku)."""
    url = get_secret(RAYLU_MCP_URL)
    api_key = get_secret(ANTHROPIC_API_KEY)
    _require(("Raylu MCP URL", url), ("Anthropic API key", api_key))
    if not get_secret(RAYLU_OAUTH_TOKENS):
        raise RuntimeError('Not connected to Raylu yet — click "Authorize Raylu" in Settings.')

    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    provider = _make_provider(url)
    async with streamablehttp_client(url, auth=provider) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            company = await session.call_tool("get_company", {"query": domain or name})
            breakdown = await session.call_tool(
                "get_score_breakdown",
                {"definition_name": SCORING_DEFINITION,
                 "domains": [domain] if domain else []})

    company_raw = _result_text(company)
    breakdown_raw = _result_text(breakdown)

    # Haiku pulls just the score out of the (large) breakdown DAG.
    from anthropic import AsyncAnthropic
    async with AsyncAnthropic(api_key=api_key) as client:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content":
                "Below is the JSON output of Raylu's get_score_breakdown for one company "
                f'under the "{SCORING_DEFINITION}" definition. Reply with ONLY the overall '
                'score value (the number or label) — nothing else. If there is no score, '
                'reply "(not scored)".\n\n' + breakdown_raw}],
        )
    score = "".join(getattr(b, "text", "") for b in resp.content
                    if getattr(b, "type", "") == "text").strip()

    text = (
        "=== get_company (raw) ===\n"
        f"{company_raw or '(no result)'}\n\n"
        f"=== {SCORING_DEFINITION} ===\n"
        f"{score or '(no score)'}"
    )
    u = resp.usage
    return EnrichResult(text=text, usage=Usage(input=u.input_tokens, output=u.output_tokens))
