from dataclasses import dataclass

from msgraph import GraphServiceClient

from auth.ms_auth import get_credential
from config import GRAPH_SCOPES


@dataclass
class EmailSummary:
    id: str
    sender_name: str
    sender_address: str
    subject: str
    received: str
    preview: str          # m.body_preview — the short snippet Outlook shows
    is_read: bool
    body_content: str     # m.body.content — the full HTML or text
    body_is_html: bool


@dataclass
class CalendarEvent:
    subject: str
    start: str                  # ISO start datetime (string)
    attendees: list[str]        # lowercased email addresses (attendees + organizer)


_graph_client: GraphServiceClient | None = None


def _client() -> GraphServiceClient:
    """Shared Graph client — built once and reused so we authenticate once per session."""
    global _graph_client
    if _graph_client is None:
        _graph_client = GraphServiceClient(credentials=get_credential(), scopes=GRAPH_SCOPES)
    return _graph_client


def _message_to_summary(m) -> EmailSummary:
    """Map a msgraph Message to our plain dataclass (keeps msgraph types out of the UI)."""
    addr = m.from_.email_address if (m.from_ and m.from_.email_address) else None
    return EmailSummary(
        id=m.id,
        sender_name=(addr.name if addr else "(unknown)"),
        sender_address=(addr.address if addr else ""),
        subject=m.subject or "(no subject)",
        received=str(m.received_date_time),
        preview=(m.body_preview or ""),
        is_read=bool(m.is_read),
        body_content=(m.body.content if m.body else ""),
        body_is_html=(str(getattr(m.body, "content_type", "")).lower().endswith("html")),
    )


async def get_recent_messages(top: int = 25) -> list[EmailSummary]:
    client = _client()
    page = await client.me.messages.get()      # body comes back by default
    return [_message_to_summary(m) for m in (page.value or [])[:top]]


async def get_messages_for_domain(domain: str, top: int = 15) -> list[EmailSummary]:
    """Messages where the given email domain appears as a participant
    (sender / recipient), via Microsoft Graph $search (KQL)."""
    # Deep, version-specific import — kept local so a path mismatch doesn't
    # break the whole module.
    from msgraph.generated.users.item.messages.messages_request_builder import (
        MessagesRequestBuilder,
    )

    client = _client()
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        search=f'"{domain}"',   # free-text: matches the domain in addresses (and body)
        top=top,
    )
    config = MessagesRequestBuilder.MessagesRequestBuilderGetRequestConfiguration(
        query_parameters=query,
    )
    page = await client.me.messages.get(request_configuration=config)
    return [_message_to_summary(m) for m in (page.value or [])]


async def get_message_by_subject(subject: str) -> EmailSummary | None:
    """Find a message in the signed-in mailbox by subject (for showing its body).

    Returns None if no copy is in this mailbox (e.g. the user wasn't a
    participant) — Graph can only read the signed-in user's own mailbox.
    """
    if not subject:
        return None
    from msgraph.generated.users.item.messages.messages_request_builder import (
        MessagesRequestBuilder,
    )

    client = _client()
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        search=f'"{subject}"', top=10,
    )
    config = MessagesRequestBuilder.MessagesRequestBuilderGetRequestConfiguration(
        query_parameters=query,
    )
    page = await client.me.messages.get(request_configuration=config)
    target = subject.strip().lower()
    msgs = page.value or []
    # exact subject first, then "contains" to tolerate Re:/Fwd: prefixes
    for m in msgs:
        if (m.subject or "").strip().lower() == target:
            return _message_to_summary(m)
    for m in msgs:
        if target in (m.subject or "").strip().lower():
            return _message_to_summary(m)
    return None


async def get_calendar_events(days_back: int = 0, days_ahead: int = 90) -> list[CalendarEvent]:
    """Calendar events from (now - days_back) to (now + days_ahead), via Graph
    calendarView. days_back=0 → from now onward. Surfaces upcoming events."""
    from datetime import datetime, timedelta, timezone
    # Deep, version-specific import — kept local so a path mismatch is contained.
    from msgraph.generated.users.item.calendar_view.calendar_view_request_builder import (
        CalendarViewRequestBuilder,
    )

    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days_back)).isoformat()
    end = (now + timedelta(days=days_ahead)).isoformat()

    client = _client()
    query = CalendarViewRequestBuilder.CalendarViewRequestBuilderGetQueryParameters(
        start_date_time=start,
        end_date_time=end,
        top=250,
        orderby=["start/dateTime"],
        select=["subject", "start", "attendees", "organizer"],
    )
    config = CalendarViewRequestBuilder.CalendarViewRequestBuilderGetRequestConfiguration(
        query_parameters=query,
    )
    page = await client.me.calendar_view.get(request_configuration=config)
    out = []
    for e in (page.value or []):
        start_dt = e.start.date_time if (e.start and e.start.date_time) else ""
        addrs = []
        for a in (e.attendees or []):
            if a.email_address and a.email_address.address:
                addrs.append(a.email_address.address.lower())
        if e.organizer and e.organizer.email_address and e.organizer.email_address.address:
            addrs.append(e.organizer.email_address.address.lower())
        out.append(CalendarEvent(subject=e.subject or "(no title)", start=start_dt, attendees=addrs))
    return out
