"""AI features via the Anthropic (Claude) API.

Service layer — the ONLY module that imports `anthropic`. The UI calls this,
never the SDK directly. The API key comes from keyring (auth.secrets), never
from source or config.
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime

from anthropic import AsyncAnthropic

from auth.secrets import get_secret, ANTHROPIC_API_KEY
from services.cache_service import read_cache, write_cache
from services.affinity_service import get_company_notes
from services import pitchbook_service

MODEL = "claude-opus-4-8"


def _client() -> AsyncAnthropic:
    """A fresh async client built from the keyring-stored key (read per call so a
    newly-saved key takes effect without a restart)."""
    key = get_secret(ANTHROPIC_API_KEY)
    if not key:
        raise RuntimeError("No Anthropic API key — add one in Settings.")
    return AsyncAnthropic(api_key=key)


async def ping() -> str:
    """Trivial round-trip to confirm the key + SDK actually reach Claude."""
    async with _client() as client:
        resp = await client.messages.create(
            model=MODEL,
            max_tokens=64,
            messages=[{"role": "user",
                       "content": "Reply with exactly: HighlandX connectivity OK"}],
        )
    return "".join(b.text for b in resp.content if b.type == "text")


# --- fundraising timeline estimate ------------------------------------------

@dataclass
class FundraisingEstimate:
    last_known_round: str
    projected_stage: str
    projected_window: str
    confidence: str                # "low" | "medium" | "high"
    rationale: str
    sources: list[dict]            # [{"title", "url"}] — may be empty (no live web data)
    landmark_dates: list[dict]     # [{"date", "label", "reason"}]


def _parse_json_object(text: str) -> dict | None:
    """Pull the first {...} JSON object out of a model reply (tolerates prose/code fences)."""
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


async def estimate_fundraising(company_name: str, domain: str | None = None,
                               notes_text: str = "",
                               funding_data: str | None = None) -> FundraisingEstimate:
    """Estimate a company's next fundraising round + landmark outreach dates.

    The data source is pluggable via `funding_data`:
      * if provided (e.g. from PitchBook once we have API access), Claude treats it
        as authoritative ground truth for past rounds;
      * if None/empty, it falls back to knowledge-only — Claude's training knowledge
        (which has a cutoff) plus the CRM notes, with lower confidence.
    Raises on API/network/parse errors so the caller can surface a message.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    who = f'"{company_name}"' + (f" (domain {domain})" if domain else "")
    if funding_data and funding_data.strip():
        basis = ("Use the authoritative fundraising data below as ground truth for past rounds; "
                 "use the CRM notes for extra context.\n\n"
                 f"FUNDING DATA:\n{funding_data}\n")
    else:
        basis = ("You do NOT have web access or a funding database — rely on your own knowledge of "
                 "the company (which may be out of date) and the CRM notes. If you lack recent or "
                 "reliable information, say so and set confidence to \"low\".\n")
    notes_block = (f"\nCRM notes (may mention fundraising):\n{notes_text}\n"
                   if notes_text.strip() else "\n(No CRM notes provided.)\n")
    prompt = (
        f"Today is {today}. Estimate the fundraising trajectory of {who}.\n\n"
        + basis + notes_block
        + "\nReply with ONLY a JSON object (no prose, no code fences) with exactly these keys:\n"
        '  "last_known_round": string,\n'
        '  "projected_stage": string,\n'
        '  "projected_window": string   (e.g. "Q2-Q3 2028" or "unknown"),\n'
        '  "confidence": "low" | "medium" | "high",\n'
        '  "rationale": string,\n'
        '  "sources": [ {"title": string, "url": string} ]   (may be empty — you have no web access),\n'
        '  "landmark_dates": [ {"date": "YYYY-MM-DD", "label": string, "reason": string} ]  '
        "(2-4 entries, working backward from the projected raise; each date MUST be a concrete "
        "YYYY-MM-DD — approximate a day if only a quarter is known)."
    )

    async with _client() as client:
        resp = await client.messages.create(
            model=MODEL, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
    text = "".join(b.text for b in resp.content if b.type == "text")

    data = _parse_json_object(text)
    if data is None:
        raise RuntimeError("Couldn't parse the estimate from Claude's response.")
    return FundraisingEstimate(
        last_known_round=data.get("last_known_round", "unknown"),
        projected_stage=data.get("projected_stage", "unknown"),
        projected_window=data.get("projected_window", "unknown"),
        confidence=data.get("confidence", "low"),
        rationale=data.get("rationale", ""),
        sources=data.get("sources", []),
        landmark_dates=data.get("landmark_dates", []),
    )


# --- persistence: store estimates so reminders can show a reach-out date -----

def save_estimate(company_id: int, est: FundraisingEstimate) -> None:
    """Cache a company's estimate (keyed by id) so the Reminders pane can surface it."""
    write_cache(f"fundraising.{company_id}", json.dumps(asdict(est)))


async def estimate_company(company_id: int, name: str,
                           domain: str | None = None) -> FundraisingEstimate:
    """Gather a company's notes + funding data, run the estimate, persist it, return it.
    Shared by the dialog and the 'Estimate all' batch."""
    notes = await get_company_notes(company_id)
    notes_text = "\n\n".join((n.content or "") for n in notes[:10])[:4000]
    funding = await pitchbook_service.get_funding_history(name, domain)
    est = await estimate_fundraising(name, domain, notes_text, funding)
    save_estimate(company_id, est)
    return est


def next_reachout_date(company_id: int) -> str | None:
    """The soonest upcoming (>= today) landmark reach-out date from a stored estimate,
    as an ISO 'YYYY-MM-DD' string, or None if there's no estimate / nothing upcoming."""
    text, _ = read_cache(f"fundraising.{company_id}")
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    today = datetime.now().strftime("%Y-%m-%d")
    upcoming = []
    for d in data.get("landmark_dates", []):
        ds = (d.get("date") or "").strip()
        try:                                   # only trust well-formed ISO dates
            datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            continue
        if ds >= today:                        # ISO strings sort chronologically
            upcoming.append(ds)
    return min(upcoming) if upcoming else None


def has_estimate(company_id: int) -> bool:
    """True if any fundraising estimate is cached for this company."""
    text, _ = read_cache(f"fundraising.{company_id}")
    return bool(text)


def is_pass_candidate(company_id: int) -> bool:
    """True if the company HAS an estimate whose landmark reach-out dates are ALL in
    the past — the outreach windows were missed, so it's a candidate to pass on."""
    text, _ = read_cache(f"fundraising.{company_id}")
    if not text:
        return False
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return False
    today = datetime.now().strftime("%Y-%m-%d")
    valid = []
    for d in data.get("landmark_dates", []):
        ds = (d.get("date") or "").strip()
        try:
            datetime.strptime(ds, "%Y-%m-%d")
        except ValueError:
            continue
        valid.append(ds)
    return bool(valid) and all(ds < today for ds in valid)
