"""Affinity CRM data via the Affinity REST API.

Uses v1 for simple lookups (whoami, person search) and v2 for list entries
(v2 returns field values inline, so we get a company's "Owners" in the same
call instead of one request per company).

Service layer — wraps httpx. The UI talks to THIS, never to httpx directly.
The API key comes from keyring (auth.secrets), never from source/config.
"""

from dataclasses import dataclass

import httpx

from auth.secrets import get_secret, AFFINITY_API_KEY

AFFINITY_BASE = "https://api.affinity.co"          # v1
AFFINITY_V2_BASE = "https://api.affinity.co/v2"    # v2

# Discovered from the Highland tenant (recorded in project notes):
DEALS_LIST_ID = 114028                 # the "Deals" company list
OWNERS_FIELD_ID = "field-1928513"      # the "Owners" (person-multi) field on it
STATUS_FIELD_ID = "field-1928512"      # the "Status" (ranked-dropdown) field on it
EMAIL_FIELD_ID = "last-email"          # relationship-intelligence: any email logged?
EVENT_FIELD_ID = "last-event"          # relationship-intelligence: any meeting logged?

# Only show deals at these early stages (matched case-insensitively):
ALLOWED_STATUSES = {"new companies", "reached out", "tracking"}


@dataclass
class Company:
    id: int
    name: str
    domain: str | None        # join key for Outlook + PitchBook later
    status: str | None = None
    emailed: bool = False     # has a logged email (relationship intelligence)
    met: bool = False         # has a logged meeting/event


@dataclass
class Note:
    id: int
    content: str              # may contain HTML / Markdown
    created_at: str
    creator_id: int | None = None
    is_meeting: bool = False


@dataclass
class CompanySummary:
    """Firm-wide interaction summary dates (ISO strings or None)."""
    last_contact: str | None = None
    last_email: str | None = None
    first_email: str | None = None
    next_event: str | None = None
    last_event: str | None = None
    first_event: str | None = None


# --- auth helpers -----------------------------------------------------------

def _basic_auth() -> httpx.BasicAuth:
    """v1 auth: HTTP Basic with a blank username and the API key as the password."""
    key = get_secret(AFFINITY_API_KEY)
    if not key:
        raise RuntimeError("Affinity API key not in keyring — run the seed step first.")
    return httpx.BasicAuth("", key)


def _bearer_headers() -> dict:
    """v2 auth: the same key sent as a Bearer token."""
    key = get_secret(AFFINITY_API_KEY)
    if not key:
        raise RuntimeError("Affinity API key not in keyring — run the seed step first.")
    return {"Authorization": f"Bearer {key}"}


# --- v1 lookups -------------------------------------------------------------

async def whoami() -> dict:
    """Confirm the key works and identify the current user."""
    async with httpx.AsyncClient(base_url=AFFINITY_BASE, auth=_basic_auth(), timeout=30) as client:
        resp = await client.get("/auth/whoami")
        resp.raise_for_status()
        return resp.json()


async def my_owner_id() -> int:
    """The id used to match the Owners field — i.e. the API key's owner.

    The key identifies whose account it is; whoami returns that user's id,
    which (for internal users) is the same id stored in the Owners person
    field. So a deal is "ours" when its Owners include this id.
    """
    me = await whoami()
    return me["user"]["id"]


async def get_company_notes(company_id: int) -> list[Note]:
    """Notes attached to a company (Affinity v1 /notes?organization_id=...)."""
    async with httpx.AsyncClient(base_url=AFFINITY_BASE, auth=_basic_auth(), timeout=30) as client:
        resp = await client.get("/notes", params={"organization_id": company_id})
        resp.raise_for_status()
        data = resp.json()

    # v1 may return a bare list or a wrapper; handle both defensively.
    raw = data if isinstance(data, list) else data.get("notes", [])
    return [
        Note(
            id=n.get("id"),
            content=n.get("content", "") or "",
            created_at=str(n.get("created_at", "")),
            creator_id=n.get("creator_id"),
            is_meeting=bool(n.get("is_meeting", False)),
        )
        for n in raw
    ]


async def get_company_summary(company_id: int) -> CompanySummary:
    """Firm-wide interaction summary for a company (v1 with_interaction_dates).

    These are aggregated across the team's connected accounts, so they're
    populated even when the signed-in user's own mailbox has nothing.
    """
    async with httpx.AsyncClient(base_url=AFFINITY_BASE, auth=_basic_auth(), timeout=30) as client:
        resp = await client.get(f"/organizations/{company_id}",
                                params={"with_interaction_dates": "true"})
        resp.raise_for_status()
        d = (resp.json() or {}).get("interaction_dates", {}) or {}

    return CompanySummary(
        last_contact=d.get("last_interaction_date"),
        last_email=d.get("last_email_date"),
        first_email=d.get("first_email_date"),
        next_event=d.get("next_event_date"),
        last_event=d.get("last_event_date"),
        first_event=d.get("first_event_date"),
    )


# --- web links --------------------------------------------------------------

AFFINITY_SUBDOMAIN = "hx"   # from the tenant info (Highland, subdomain "hx")


def company_url(company_id: int) -> str:
    """Link to a company's page in the Affinity web app."""
    return f"https://{AFFINITY_SUBDOMAIN}.affinity.co/companies/{company_id}"


# --- v2 list entries (companies on the Deals list, with field values) -------

def _owner_ids(entry: dict) -> list[int]:
    """Pull the person ids out of the Owners field on a v2 list entry.

    Note: on a company list, the fields live under entry["entity"]["fields"].
    """
    ent = entry.get("entity") or {}
    for field in ent.get("fields", []):
        if field.get("id") == OWNERS_FIELD_ID:
            data = (field.get("value") or {}).get("data") or []
            return [p.get("id") for p in data if isinstance(p, dict) and p.get("id") is not None]
    return []


def _field_has_data(entry: dict, field_id: str) -> bool:
    """True if the given field on a v2 list entry has a non-null value."""
    ent = entry.get("entity") or {}
    for field in ent.get("fields", []):
        if field.get("id") == field_id:
            return (field.get("value") or {}).get("data") is not None
    return False


def _status_text(entry: dict) -> str | None:
    """Pull the Status dropdown label off a v2 list entry."""
    ent = entry.get("entity") or {}
    for field in ent.get("fields", []):
        if field.get("id") == STATUS_FIELD_ID:
            data = (field.get("value") or {}).get("data")
            if isinstance(data, dict):
                return data.get("text")
            if isinstance(data, list) and data:
                return data[0].get("text")
    return None


def _entity_to_company(entry: dict) -> Company:
    """Map a v2 list entry's company entity to our plain Company dataclass."""
    ent = entry.get("entity") or {}
    domain = ent.get("domain")
    if not domain:
        domains = ent.get("domains") or []
        domain = domains[0] if domains else None
    return Company(id=ent.get("id"), name=ent.get("name", ""), domain=domain,
                   status=_status_text(entry),
                   emailed=_field_has_data(entry, EMAIL_FIELD_ID),
                   met=_field_has_data(entry, EVENT_FIELD_ID))


async def list_my_companies(my_pid: int) -> list[Company]:
    """Deals owned by my_pid whose Status is one of ALLOWED_STATUSES."""
    out: list[Company] = []
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{AFFINITY_V2_BASE}/lists/{DEALS_LIST_ID}/list-entries"
        params = {"fieldIds": f"{OWNERS_FIELD_ID},{STATUS_FIELD_ID},{EMAIL_FIELD_ID},{EVENT_FIELD_ID}",
                  "limit": 100}
        while url:
            resp = await client.get(url, params=params, headers=_bearer_headers())
            resp.raise_for_status()
            payload = resp.json()

            for entry in payload.get("data", []):
                if my_pid not in _owner_ids(entry):
                    continue
                if (_status_text(entry) or "").lower() not in ALLOWED_STATUSES:
                    continue
                out.append(_entity_to_company(entry))

            url = (payload.get("pagination") or {}).get("nextUrl")
            params = None      # nextUrl already carries the cursor + params
    return out
