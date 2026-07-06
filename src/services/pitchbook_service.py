"""PitchBook funding-data source — THE PIVOT POINT.

Today this is a stub: we have no PitchBook API access, so get_funding_history()
returns None and the fundraising estimate falls back to knowledge-only.

WHEN PITCHBOOK API ACCESS IS GRANTED, the whole pivot happens here:
  1. Store the PitchBook key in keyring (add PITCHBOOK_API_KEY to auth/secrets.py
     and a field in the Settings dialog, mirroring the Affinity/Anthropic keys).
  2. Implement get_funding_history() with httpx against the PitchBook API — resolve
     the company, fetch its rounds, and return a readable text summary (stage, date,
     amount, lead investors per round).
  3. Set AVAILABLE = True.
Nothing else changes: ai_service.estimate_fundraising already accepts this text as
authoritative `funding_data`, and the dialog already routes through here.
"""

AVAILABLE = False


async def get_funding_history(company_name: str, domain: str | None = None) -> str | None:
    """A text summary of the company's known funding rounds, or None if no data
    source is available yet (→ estimation falls back to knowledge-only)."""
    if not AVAILABLE:
        return None
    # TODO(pitchbook): resolve the company + fetch rounds via the PitchBook API,
    # then format them as text (one line per round: stage, date, amount, investors).
    raise NotImplementedError("PitchBook API integration not implemented yet.")
