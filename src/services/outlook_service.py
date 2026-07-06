import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from msgraph import GraphServiceClient

from auth.ms_auth import get_credential, ensure_authenticated
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


def _norm_subject(s: str) -> str:
    """Normalize a subject for comparison: strip Re:/Fw:/Fwd: prefixes, a trailing
    Affinity thread-count suffix like ' (2)', then trim and lowercase."""
    s = (s or "").strip().lower()
    # drop a trailing Affinity thread-count, e.g. "weekly sync (3)" -> "weekly sync"
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)
    # drop leading Re:/Fw:/Fwd: prefixes (possibly repeated)
    while True:
        stripped = re.sub(r"^(re|fw|fwd)\s*:\s*", "", s)
        if stripped == s:
            break
        s = stripped
    return s.strip()


def _to_utc(value) -> datetime | None:
    """Best-effort parse of an ISO string or datetime to an aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def get_message_by_interaction(
    from_address: str | None,
    sent_at: str | None,
    subject: str | None = None,
) -> EmailSummary | None:
    """Find the mailbox copy of an email logged in Affinity.

    The subject is the selective key, so we search by it (a sender like yourself
    appears in far too many emails for from+date to narrow usefully). Among the
    subject matches we disambiguate by the sender Affinity recorded and the
    interaction date. Subjects are normalized to drop Re:/Fw: prefixes and
    Affinity's trailing "(N)" thread-count. Returns None if nothing matches.
    """
    if not subject:
        return None
    want = _norm_subject(subject)
    when = _to_utc(sent_at)

    from msgraph.generated.users.item.messages.messages_request_builder import (
        MessagesRequestBuilder,
    )
    # Strip Affinity's trailing "(N)" thread-count so the phrase matches real mail.
    phrase = re.sub(r"\s*\(\d+\)\s*$", "", subject).strip()

    await ensure_authenticated()
    client = _client()
    # KQL scoped to the subject. Graph needs the whole $search value wrapped in
    # double quotes, with the phrase's own quotes escaped: "subject:\"...\""
    query = MessagesRequestBuilder.MessagesRequestBuilderGetQueryParameters(
        search=f'"subject:\\"{phrase}\\""', top=50,
    )
    config = MessagesRequestBuilder.MessagesRequestBuilderGetRequestConfiguration(
        query_parameters=query,
    )
    page = await client.me.messages.get(request_configuration=config)
    msgs = page.value or []

    # keep hits whose normalized subject matches, exact first then substring
    matches = [m for m in msgs if _norm_subject(m.subject or "") == want]
    if not matches and len(want) >= 4:
        matches = [m for m in msgs if want in _norm_subject(m.subject or "")]
    if not matches:
        return None

    # disambiguate same-subject hits: prefer the sender Affinity recorded, then
    # the message closest in time to the interaction
    def _score(m) -> tuple:
        addr = ""
        if m.from_ and m.from_.email_address:
            addr = m.from_.email_address.address or ""
        from_ok = 0 if (from_address and addr.lower() == from_address.lower()) else 1
        t = _to_utc(m.sent_date_time)
        dt = abs((t - when).total_seconds()) if (t and when) else float("inf")
        return (from_ok, dt)

    return _message_to_summary(min(matches, key=_score))


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

    await ensure_authenticated()                 # sign in off-loop if needed
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
