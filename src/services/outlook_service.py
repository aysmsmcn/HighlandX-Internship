from msgraph import GraphServiceClient
from dataclasses import dataclass
from auth.ms_auth import get_credential
from config import GRAPH_SCOPES

@dataclass
class EmailSummary:
    subject: str
    sender: str
    received: str

def _client() -> GraphServiceClient:
    return GraphServiceClient(credentials=get_credential(), scopes=GRAPH_SCOPES)

async def get_recent_messages(top: int = 10) -> list[EmailSummary]:
    client = _client()
    page = await client.me.messages.get()      # async call — must be awaited
    out = []
    for m in (page.value or [])[:top]:
        sender = m.from_.email_address.address if m.from_ and m.from_.email_address else "(unknown)"
        out.append(EmailSummary(
            subject=m.subject or "(no subject)",
            sender=sender,
            received=str(m.received_date_time),
        ))
    return out