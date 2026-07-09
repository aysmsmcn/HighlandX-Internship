"""Affinity CRM data via the Affinity REST API.

Uses v1 for simple lookups (whoami, notes, interaction dates) and v2 for list
entries (v2 returns field values — owners, status, and relationship-intelligence
interactions — inline, so we get them in one paginated call).

Service layer — wraps httpx. The UI talks to THIS, never to httpx directly.
The API key comes from keyring (auth.secrets), never from source/config.
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

import httpx

from auth.secrets import get_secret, AFFINITY_API_KEY

AFFINITY_BASE = "https://api.affinity.co"          # v1
AFFINITY_V2_BASE = "https://api.affinity.co/v2"    # v2

# Discovered from the Highland tenant (recorded in project notes):
DEALS_LIST_ID = 114028                 # the "Deals" company list
OWNERS_FIELD_ID = "field-1928513"      # the "Owners" (person-multi) field on it
STATUS_FIELD_ID = "field-1928512"      # the "Status" (ranked-dropdown) field on it
EMAIL_FIELD_ID = "last-email"          # relationship-intelligence: last email
EVENT_FIELD_ID = "last-event"          # relationship-intelligence: last meeting
FIRST_EMAIL_FIELD_ID = "first-email"   # relationship-intelligence: first email
NEXT_EVENT_FIELD_ID = "next-event"     # relationship-intelligence: next meeting
FUNDING_DATE_FIELD_ID = "affinity-data-last-funding-date"      # enriched: last raise date
FUNDING_AMOUNT_FIELD_ID = "affinity-data-last-funding-amount"  # enriched: last raise amount (USD)
TOTAL_FUNDING_FIELD_ID = "affinity-data-total-funding-amount"  # enriched: total raised to date (USD)
STAGE_FIELD_ID = "affinity-data-investment-stage"              # enriched: e.g. "Seed", "Series A"
EMPLOYEES_FIELD_ID = "affinity-data-employees-current"                    # enriched: headcount
EMPLOYEES_GROWTH_FIELD_ID = "affinity-data-employees-growth-yoy-percentage"  # enriched: YoY headcount growth %
HIRES_3MO_FIELD_ID = "affinity-data-employee-hires-last-3-months-percentage"  # enriched: hires last 3mo, % of headcount

# Only show deals at these early stages (matched case-insensitively):
ALLOWED_STATUSES = {"new companies", "reached out", "tracking"}
# Status dropdown option IDs for the ALLOWED_STATUSES labels (from the Deals list's Status
# field), used to filter server-side in list_my_companies. Keep in sync with ALLOWED_STATUSES.
STATUS_OPTION_IDS = {
    "new companies": 11428717,
    "reached out": 4372360,
    "tracking": 4372367,
}

# Reach-out heuristic: typical months between rounds by stage (rough industry
# averages), minus a lead time so the suggestion lands before the next round.
STAGE_CADENCE_MONTHS = {
    "Pre-Seed": 12,
    "Seed": 18,
    "Series A": 18,
    "Series B": 15,
    "Series C": 15,
    "Series D": 18,
    "Series E": 18,
}
DEFAULT_CADENCE_MONTHS = 18
REACHOUT_LEAD_MONTHS = 3
_DAYS_PER_MONTH = 30.44

# Headcount-growth modulation of cadence (from Affinity's YoY headcount-growth data,
# ~89% coverage). Fast-growing companies burn faster and tend to raise sooner → shorter
# cadence; flat/shrinking companies raise later → longer cadence. All tunable.
GROWTH_NEUTRAL_PCT = 40      # ~typical YoY headcount growth → no adjustment (factor 1.0)
GROWTH_FAST_PCT = 150        # at/above this YoY growth → maximum speed-up
CADENCE_MIN_FACTOR = 0.6     # fastest growers: cadence × 0.6 (raise ~40% sooner)
CADENCE_MAX_FACTOR = 1.4     # flat/shrinking: cadence × 1.4 (raise later)

# Runway proxy: last round size per employee (a rough burn/runway signal). More capital
# per head → longer runway → they raise later; thin capital per head → sooner. Tunable.
RUNWAY_LOW_PER_HEAD = 50_000       # at/below this $/employee → maximum "sooner" pull
RUNWAY_NEUTRAL_PER_HEAD = 200_000  # ~$200K/employee → no adjustment (factor 1.0)
RUNWAY_HIGH_PER_HEAD = 500_000     # at/above this $/employee → maximum "later" push
RUNWAY_MIN_FACTOR = 0.85           # thin runway: cadence × 0.85 (sooner)
RUNWAY_MAX_FACTOR = 1.30           # fat runway: cadence × 1.30 (later)

# Hiring momentum: hires in the last 3 months as % of headcount. Active hiring → scaling/
# burning → raise sooner. Kept a gentle nudge since it overlaps with YoY growth. Tunable.
HIRING_NEUTRAL_PCT = 7       # ~7% of headcount hired in 3mo → no adjustment
HIRING_FAST_PCT = 20         # at/above this → maximum "sooner" pull
HIRING_MIN_FACTOR = 0.85     # hot hiring: cadence × 0.85 (sooner)
HIRING_MAX_FACTOR = 1.15     # no hiring: cadence × 1.15 (later)

# Overall clamp on the combined multiplier (growth × runway × hiring) so three factors
# stacking can't produce an absurd cadence.
CADENCE_COMBINED_MIN = 0.5
CADENCE_COMBINED_MAX = 1.7

# Accelerator / micro-round detection. Enrichment often tags a small accelerator or
# pre-seed round (e.g. YC/Techstars, ~$500K) as "Seed", which would otherwise anchor the
# full 18-month Seed cadence and overshoot. A round at/below this size on a non-late-stage
# label is treated as an accelerator round with a short cadence — they raise their real
# seed fast. Tunable.
ACCELERATOR_MAX_AMOUNT = 750_000
ACCELERATOR_CADENCE_MONTHS = 9
LATE_STAGES = {"Series A", "Series B", "Series C", "Series D", "Series E"}

# Fit score heuristic: is this the kind of company (size + funding profile) the
# firm invests in? Separate from reach-out TIMING (see ReachOutSuggestion above) —
# a company can be a great fit but simply the wrong moment to reach out.
EMPLOYEE_SWEET_MIN = 25         # sweet spot: 25-200 employees
EMPLOYEE_SWEET_MAX = 200
STAGE_FIT_SCORE = {             # Series B is the sweet spot; tapers off either side
    "Pre-Seed": 40,
    "Seed": 55,
    "Series A": 85,
    "Series B": 100,
    "Series C": 85,
    "Series D": 55,
    "Series E": 40,
}
DEFAULT_STAGE_SCORE = 60        # unknown/unmapped stage — neutral, not penalized
FUNDING_SWEET_CEILING = 100_000_000    # "the less raised, the better", but fine up to here
FUNDING_TOO_FAR_GONE = 150_000_000     # beyond this, the company is "too far gone" for us

# "How to proceed" bands, keyed off the fit score. Tunable via Settings (the UI passes
# the user's saved values into proceed_recommendation); these are the defaults.
DEFAULT_GOOD_FIT_THRESHOLD = 70   # score >= this → good fit, pursue
DEFAULT_PASS_THRESHOLD = 45       # score < this → likely pass; between the two → marginal


@dataclass
class Interaction:
    kind: str                 # "email" or "meeting"
    date: str                 # ISO (email sentAt / meeting startTime)
    subject: str              # email subject or meeting title
    who: str                  # email sender name, or joined meeting attendees
    from_address: str | None = None   # email sender address (for Outlook lookup)


@dataclass
class Company:
    id: int
    name: str
    domain: str | None        # join key for Outlook + PitchBook later
    status: str | None = None
    added: str | None = None  # ISO date this company was added to the Deals list
    emailed: bool = False     # has a logged email (relationship intelligence)
    met: bool = False         # has a logged meeting/event
    first_email: Interaction | None = None
    last_email: Interaction | None = None
    last_event: Interaction | None = None
    next_event: Interaction | None = None
    last_funding_date: str | None = None     # ISO, from Affinity's enrichment data
    last_funding_amount: float | None = None  # USD
    total_funding_amount: float | None = None  # USD, total raised to date
    investment_stage: str | None = None       # e.g. "Seed", "Series A"
    employees_current: int | None = None      # current headcount
    employees_growth_yoy: float | None = None  # YoY headcount growth, %
    hires_3mo_pct: float | None = None         # hires in last 3 months, % of headcount


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
    """The id used to match the Owners field — i.e. the API key's owner."""
    me = await whoami()
    return me["user"]["id"]


async def get_company_notes(company_id: int) -> list[Note]:
    """Notes attached to a company (Affinity v1 /notes?organization_id=...)."""
    async with httpx.AsyncClient(base_url=AFFINITY_BASE, auth=_basic_auth(), timeout=30) as client:
        resp = await client.get("/notes", params={"organization_id": company_id})
        resp.raise_for_status()
        data = resp.json()

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


async def company_has_notes(company_id: int) -> bool:
    """True if a company has at least one note (cheap — fetches a single note)."""
    async with httpx.AsyncClient(base_url=AFFINITY_BASE, auth=_basic_auth(), timeout=30) as client:
        resp = await client.get("/notes", params={"organization_id": company_id, "page_size": 1})
        resp.raise_for_status()
        data = resp.json()
        notes = data if isinstance(data, list) else data.get("notes", [])
        return len(notes) > 0


async def get_company_summary(company_id: int) -> CompanySummary:
    """Firm-wide interaction summary dates for a company (v1 with_interaction_dates)."""
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


def _person_name(person: dict, fallback_email: str) -> str:
    name = f"{person.get('firstName', '')} {person.get('lastName', '')}".strip()
    return name or fallback_email


def _enriched_scalar(entry: dict, field_id: str):
    """Pull a scalar enriched-field value (date/number/text) off a v2 entity, or None."""
    ent = entry.get("entity") or {}
    for field in ent.get("fields", []):
        if field.get("id") == field_id:
            return (field.get("value") or {}).get("data")
    return None


@dataclass
class ReachOutSuggestion:
    """The suggested reach-out date, plus the math behind it (for display)."""
    date: str | None = None                  # the suggestion itself
    last_funding_date: str | None = None
    investment_stage: str | None = None
    base_cadence_months: int = DEFAULT_CADENCE_MONTHS   # from the stage table, before adjustment
    cadence_months: int = DEFAULT_CADENCE_MONTHS        # effective cadence, after all factors
    growth_yoy: float | None = None          # YoY headcount growth % (None = not available)
    growth_factor: float = 1.0               # cadence multiplier from headcount growth
    runway_per_head: float | None = None     # last round $ / employee (None = not computable)
    runway_factor: float = 1.0               # cadence multiplier from runway proxy
    hires_3mo_pct: float | None = None        # hires last 3mo, % of headcount (None = not available)
    hiring_factor: float = 1.0               # cadence multiplier from hiring momentum
    combined_factor: float = 1.0             # clamped product of the three factors
    used_default_cadence: bool = False       # True if stage wasn't in STAGE_CADENCE_MONTHS
    is_accelerator: bool = False             # small early round → accelerator cadence used
    last_funding_amount: float | None = None  # for display of the accelerator note
    lead_months: int = REACHOUT_LEAD_MONTHS
    projected_next_round: str | None = None  # last raise + cadence, before the lead-time is subtracted


def _cadence_growth_factor(growth_yoy: float | None) -> float:
    """Multiplier on the base cadence from YoY headcount growth, clamped to
    [CADENCE_MIN_FACTOR, CADENCE_MAX_FACTOR]. Faster growth → smaller factor (sooner)."""
    if growth_yoy is None:
        return 1.0
    if growth_yoy >= GROWTH_FAST_PCT:
        return CADENCE_MIN_FACTOR
    if growth_yoy <= 0:
        return CADENCE_MAX_FACTOR
    if growth_yoy < GROWTH_NEUTRAL_PCT:
        # 0% growth → MAX_FACTOR, up to neutral → 1.0
        return CADENCE_MAX_FACTOR + (1.0 - CADENCE_MAX_FACTOR) * (growth_yoy / GROWTH_NEUTRAL_PCT)
    # neutral → 1.0, up to fast → MIN_FACTOR
    span = GROWTH_FAST_PCT - GROWTH_NEUTRAL_PCT
    return 1.0 + (CADENCE_MIN_FACTOR - 1.0) * ((growth_yoy - GROWTH_NEUTRAL_PCT) / span)


def _cadence_runway_factor(last_funding_amount: float | None, employees: int | None) -> float:
    """Multiplier from the runway proxy (last round $ per employee). More $/head → longer
    runway → larger factor (later). Returns 1.0 if either input is missing."""
    if not last_funding_amount or not employees:
        return 1.0
    per_head = last_funding_amount / employees
    if per_head >= RUNWAY_HIGH_PER_HEAD:
        return RUNWAY_MAX_FACTOR
    if per_head <= RUNWAY_LOW_PER_HEAD:
        return RUNWAY_MIN_FACTOR
    if per_head < RUNWAY_NEUTRAL_PER_HEAD:
        # LOW → MIN_FACTOR, up to neutral → 1.0
        span = RUNWAY_NEUTRAL_PER_HEAD - RUNWAY_LOW_PER_HEAD
        return RUNWAY_MIN_FACTOR + (1.0 - RUNWAY_MIN_FACTOR) * ((per_head - RUNWAY_LOW_PER_HEAD) / span)
    # neutral → 1.0, up to high → MAX_FACTOR
    span = RUNWAY_HIGH_PER_HEAD - RUNWAY_NEUTRAL_PER_HEAD
    return 1.0 + (RUNWAY_MAX_FACTOR - 1.0) * ((per_head - RUNWAY_NEUTRAL_PER_HEAD) / span)


def _cadence_hiring_factor(hires_3mo_pct: float | None) -> float:
    """Multiplier from hiring momentum (hires last 3mo as % of headcount). More hiring →
    smaller factor (sooner). Gentle band since it overlaps with YoY growth."""
    if hires_3mo_pct is None:
        return 1.0
    if hires_3mo_pct >= HIRING_FAST_PCT:
        return HIRING_MIN_FACTOR
    if hires_3mo_pct <= 0:
        return HIRING_MAX_FACTOR
    if hires_3mo_pct < HIRING_NEUTRAL_PCT:
        # 0% → MAX_FACTOR, up to neutral → 1.0
        return HIRING_MAX_FACTOR + (1.0 - HIRING_MAX_FACTOR) * (hires_3mo_pct / HIRING_NEUTRAL_PCT)
    # neutral → 1.0, up to fast → MIN_FACTOR
    span = HIRING_FAST_PCT - HIRING_NEUTRAL_PCT
    return 1.0 + (HIRING_MIN_FACTOR - 1.0) * ((hires_3mo_pct - HIRING_NEUTRAL_PCT) / span)


def reach_out_suggestion(company: Company) -> ReachOutSuggestion:
    """Heuristic next-reach-out date: last raise + stage cadence, adjusted by headcount
    growth, runway, and hiring momentum, minus lead time."""
    if not company.last_funding_date:
        return ReachOutSuggestion()
    last = datetime.fromisoformat(company.last_funding_date.replace("Z", "+00:00"))
    stage = company.investment_stage

    # A small early-stage round is very likely an accelerator/pre-seed round mislabeled
    # "Seed" — use a short cadence instead of the full stage cadence.
    is_accelerator = (company.last_funding_amount is not None
                      and company.last_funding_amount <= ACCELERATOR_MAX_AMOUNT
                      and stage not in LATE_STAGES)
    if is_accelerator:
        base_cadence = ACCELERATOR_CADENCE_MONTHS
    else:
        base_cadence = STAGE_CADENCE_MONTHS.get(stage, DEFAULT_CADENCE_MONTHS)

    growth_factor = _cadence_growth_factor(company.employees_growth_yoy)
    runway_factor = _cadence_runway_factor(company.last_funding_amount, company.employees_current)
    hiring_factor = _cadence_hiring_factor(company.hires_3mo_pct)
    combined = max(CADENCE_COMBINED_MIN,
                   min(CADENCE_COMBINED_MAX, growth_factor * runway_factor * hiring_factor))
    cadence = max(1, round(base_cadence * combined))

    per_head = (company.last_funding_amount / company.employees_current
                if company.last_funding_amount and company.employees_current else None)
    projected_next = last + timedelta(days=cadence * _DAYS_PER_MONTH)
    suggestion = projected_next - timedelta(days=REACHOUT_LEAD_MONTHS * _DAYS_PER_MONTH)
    return ReachOutSuggestion(
        date=suggestion.date().isoformat(),
        last_funding_date=company.last_funding_date,
        investment_stage=stage,
        base_cadence_months=base_cadence,
        cadence_months=cadence,
        growth_yoy=company.employees_growth_yoy,
        growth_factor=round(growth_factor, 2),
        runway_per_head=per_head,
        runway_factor=round(runway_factor, 2),
        hires_3mo_pct=company.hires_3mo_pct,
        hiring_factor=round(hiring_factor, 2),
        combined_factor=round(combined, 2),
        used_default_cadence=stage not in STAGE_CADENCE_MONTHS,
        is_accelerator=is_accelerator,
        last_funding_amount=company.last_funding_amount,
        lead_months=REACHOUT_LEAD_MONTHS,
        projected_next_round=projected_next.date().isoformat(),
    )


@dataclass
class FitScore:
    """0-100 investment-fit heuristic (headcount + funding profile), or None ('N/A')
    if there isn't enough data (currently: employee headcount) to compute one."""
    score: int | None = None
    employee_score: float | None = None
    funding_score: float | None = None


def _employee_score(employees: int | None, growth_yoy: float | None) -> float | None:
    if employees is None:
        return None
    if employees < 20:
        base = (employees / 20) * 60                                            # 0 -> 60
    elif employees < EMPLOYEE_SWEET_MIN:
        base = 60 + (employees - 20) / (EMPLOYEE_SWEET_MIN - 20) * 40            # 60 -> 100
    elif employees <= EMPLOYEE_SWEET_MAX:
        base = 100.0
    elif employees <= 300:
        base = 100 - (employees - EMPLOYEE_SWEET_MAX) / 100 * 60                 # 100 -> 40
    else:
        base = max(10.0, 40 - (employees - 300) / 50 * 5)
    if growth_yoy is not None:
        base += max(-10.0, min(10.0, growth_yoy / 5))   # small nudge for growing/shrinking headcount
    return max(0.0, min(100.0, base))


def _funding_amount_multiplier(total_funding: float | None) -> float:
    """Penalty multiplier for how much has been raised: 1.0 up to the sweet ceiling,
    easing down through the "too far gone" threshold, then crashing well past it.
    Applied to the WHOLE score (not just averaged in) — a company can look great on
    headcount alone and still be "too far gone" once it's raised way too much."""
    if total_funding is None:
        return 1.0    # no enrichment data — neutral, not penalized
    if total_funding <= FUNDING_SWEET_CEILING:
        return 1.0
    if total_funding <= FUNDING_TOO_FAR_GONE:
        span = FUNDING_TOO_FAR_GONE - FUNDING_SWEET_CEILING
        return 1.0 - (total_funding - FUNDING_SWEET_CEILING) / span * 0.5   # 1.0 -> 0.5
    return max(0.1, 0.5 - (total_funding - FUNDING_TOO_FAR_GONE) / FUNDING_TOO_FAR_GONE * 0.4)


def fit_score(company: Company) -> FitScore:
    """Investment-fit heuristic — separate from reach-out TIMING (see reach_out_suggestion)."""
    emp_score = _employee_score(company.employees_current, company.employees_growth_yoy)
    if emp_score is None:
        return FitScore()
    stage_score = STAGE_FIT_SCORE.get(company.investment_stage, DEFAULT_STAGE_SCORE)
    multiplier = _funding_amount_multiplier(company.total_funding_amount)
    overall = (0.5 * emp_score + 0.5 * stage_score) * multiplier
    return FitScore(
        score=round(max(0.0, min(100.0, overall))),
        employee_score=round(emp_score, 1),
        funding_score=round(stage_score * multiplier, 1),
    )


@dataclass
class ProceedRecommendation:
    """A "how to proceed" verdict for a company, driven by its fit score."""
    score: int | None       # None if fit score is N/A
    band: str               # "good" | "marginal" | "pass" | "unknown"
    headline: str           # short recommended action
    reasons: list[str]      # plain-language explanation of what drove the score


def _fmt_usd(amount: float) -> str:
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.0f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:,.0f}"


def _fit_reasons(company: Company) -> list[str]:
    """Plain-language notes on the signals behind a company's fit score."""
    reasons: list[str] = []

    emp = company.employees_current
    if emp is None:
        reasons.append("No employee headcount on file.")
    elif emp < 20:
        reasons.append(f"Only {emp} employees — likely too early (below the ~20 minimum).")
    elif emp < EMPLOYEE_SWEET_MIN:
        reasons.append(f"{emp} employees — just under the 25–200 sweet spot.")
    elif emp <= EMPLOYEE_SWEET_MAX:
        reasons.append(f"{emp} employees — squarely in the 25–200 sweet spot.")
    elif emp <= 300:
        reasons.append(f"{emp} employees — a bit above the 200 sweet-spot ceiling.")
    else:
        reasons.append(f"{emp} employees — well above the 200 ceiling; likely too large.")

    growth = company.employees_growth_yoy
    if growth is not None:
        if growth > 0:
            reasons.append(f"Headcount up {growth:.0f}% YoY — growing.")
        elif growth < 0:
            reasons.append(f"Headcount down {abs(growth):.0f}% YoY — shrinking.")

    stage = company.investment_stage
    if not stage:
        reasons.append("Investment stage unknown.")
    elif stage == "Series B":
        reasons.append(f"{stage} — your sweet-spot stage.")
    elif stage in ("Series A", "Series C"):
        reasons.append(f"{stage} — close to your Series B sweet spot.")
    elif stage in ("Seed", "Pre-Seed"):
        reasons.append(f"{stage} — earlier than your typical entry.")
    else:
        reasons.append(f"{stage} — later than your typical entry.")

    funding = company.total_funding_amount
    if funding is not None:
        if funding <= FUNDING_SWEET_CEILING:
            reasons.append(f"Raised {_fmt_usd(funding)} total — within range (under $100M).")
        elif funding <= FUNDING_TOO_FAR_GONE:
            reasons.append(f"Raised {_fmt_usd(funding)} total — above $100M; score reduced.")
        else:
            reasons.append(f"Raised {_fmt_usd(funding)} total — above $150M; likely too far gone.")

    return reasons


def proceed_recommendation(company: Company,
                           good_threshold: int = DEFAULT_GOOD_FIT_THRESHOLD,
                           pass_threshold: int = DEFAULT_PASS_THRESHOLD,
                           override_score: int | None = None) -> ProceedRecommendation:
    """Turn a company's fit score into a how-to-proceed band + reasons.
    override_score, if given, is a manually-set score that stands in for the computed one."""
    score = override_score if override_score is not None else fit_score(company).score
    if score is None:
        return ProceedRecommendation(
            None, "unknown", "Not enough data to evaluate",
            ["No employee headcount on file, so a fit score can't be computed. "
             "Refresh from Affinity to pull enrichment data."])

    reasons = _fit_reasons(company)
    if override_score is not None:
        reasons.insert(0, f"Using your manual score override of {override_score}.")

    if score >= good_threshold:
        band, headline = "good", "Good fit — worth pursuing"
    elif score >= pass_threshold:
        band, headline = "marginal", "Marginal — track and revisit as they grow or raise"
    else:
        band, headline = "pass", "Likely pass — deprioritize"
    return ProceedRecommendation(score, band, headline, reasons)


def _interaction(entry: dict, field_id: str) -> Interaction | None:
    """Build an Interaction from a relationship-intelligence field, or None."""
    ent = entry.get("entity") or {}
    for field in ent.get("fields", []):
        if field.get("id") != field_id:
            continue
        data = (field.get("value") or {}).get("data")
        if not data:
            return None
        if data.get("type") == "email":
            frm = data.get("from") or {}
            addr = frm.get("emailAddress", "")
            return Interaction(
                kind="email",
                date=data.get("sentAt", ""),
                subject=data.get("subject") or "(no subject)",
                who=_person_name(frm.get("person") or {}, addr),
                from_address=addr or None,
            )
        if data.get("type") == "meeting":
            names = [_person_name(a.get("person") or {}, a.get("emailAddress", ""))
                     for a in (data.get("attendees") or [])]
            return Interaction(
                kind="meeting",
                date=data.get("startTime", ""),
                subject=data.get("title") or "(meeting)",
                who=", ".join(n for n in names if n),
            )
        return None
    return None


def _entity_to_company(entry: dict) -> Company:
    """Map a v2 list entry's company entity to our plain Company dataclass."""
    ent = entry.get("entity") or {}
    domain = ent.get("domain")
    if not domain:
        domains = ent.get("domains") or []
        domain = domains[0] if domains else None

    last_email = _interaction(entry, EMAIL_FIELD_ID)
    last_event = _interaction(entry, EVENT_FIELD_ID)
    return Company(
        id=ent.get("id"), name=ent.get("name", ""), domain=domain,
        status=_status_text(entry),
        added=entry.get("createdAt"),     # when added to the Deals list
        emailed=last_email is not None,
        met=last_event is not None,
        first_email=_interaction(entry, FIRST_EMAIL_FIELD_ID),
        last_email=last_email,
        last_event=last_event,
        next_event=_interaction(entry, NEXT_EVENT_FIELD_ID),
        last_funding_date=_enriched_scalar(entry, FUNDING_DATE_FIELD_ID),
        last_funding_amount=_enriched_scalar(entry, FUNDING_AMOUNT_FIELD_ID),
        total_funding_amount=_enriched_scalar(entry, TOTAL_FUNDING_FIELD_ID),
        investment_stage=_enriched_scalar(entry, STAGE_FIELD_ID),
        employees_current=_enriched_scalar(entry, EMPLOYEES_FIELD_ID),
        employees_growth_yoy=_enriched_scalar(entry, EMPLOYEES_GROWTH_FIELD_ID),
        hires_3mo_pct=_enriched_scalar(entry, HIRES_3MO_FIELD_ID),
    )


# --- (de)serialization for the local cache ----------------------------------

def companies_to_json(companies: list[Company]) -> str:
    """Serialize companies (with their nested Interactions) to a JSON string."""
    return json.dumps([asdict(c) for c in companies])


def _interaction_from(d: dict | None) -> Interaction | None:
    return Interaction(**d) if d else None


def companies_from_json(text: str) -> list[Company]:
    """Rebuild companies from a JSON string produced by companies_to_json."""
    out: list[Company] = []
    for d in json.loads(text):
        out.append(Company(
            id=d["id"], name=d["name"], domain=d.get("domain"),
            status=d.get("status"), added=d.get("added"),
            emailed=d.get("emailed", False), met=d.get("met", False),
            first_email=_interaction_from(d.get("first_email")),
            last_email=_interaction_from(d.get("last_email")),
            last_event=_interaction_from(d.get("last_event")),
            next_event=_interaction_from(d.get("next_event")),
            last_funding_date=d.get("last_funding_date"),
            last_funding_amount=d.get("last_funding_amount"),
            total_funding_amount=d.get("total_funding_amount"),
            investment_stage=d.get("investment_stage"),
            employees_current=d.get("employees_current"),
            employees_growth_yoy=d.get("employees_growth_yoy"),
            hires_3mo_pct=d.get("hires_3mo_pct"),
        ))
    return out


async def list_my_companies(my_pid: int) -> list[Company]:
    """Deals owned by my_pid whose Status is one of ALLOWED_STATUSES.

    Uses the v2 filtered-search endpoint so Affinity applies the Owners + Status
    filters server-side: we fetch only the ~4k matching entries instead of paging
    the entire ~23k-row Deals list and filtering client-side (~10x faster).
    Note: this endpoint is POST-only (its nextUrl rejects GET), and the nextUrl
    carries the fieldIds + cursor, so later pages re-POST to it with the same body.
    """
    field_ids = ",".join([OWNERS_FIELD_ID, STATUS_FIELD_ID, EMAIL_FIELD_ID,
                          EVENT_FIELD_ID, FIRST_EMAIL_FIELD_ID, NEXT_EVENT_FIELD_ID,
                          FUNDING_DATE_FIELD_ID, FUNDING_AMOUNT_FIELD_ID,
                          TOTAL_FUNDING_FIELD_ID, STAGE_FIELD_ID,
                          EMPLOYEES_FIELD_ID, EMPLOYEES_GROWTH_FIELD_ID,
                          HIRES_3MO_FIELD_ID])
    option_ids = [STATUS_OPTION_IDS[s] for s in ALLOWED_STATUSES if s in STATUS_OPTION_IDS]
    body = {
        "filters": {"operator": "and", "filters": [
            {"fieldId": OWNERS_FIELD_ID, "valueType": "person-multi",
             "operator": "has-any-of", "value": [{"id": my_pid}]},
            {"fieldId": STATUS_FIELD_ID, "valueType": "ranked-dropdown",
             "operator": "is-any-of",
             "value": [{"dropdownOptionId": oid} for oid in option_ids]},
        ]},
    }
    out: list[Company] = []
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{AFFINITY_V2_BASE}/lists/{DEALS_LIST_ID}/list-entries/search"
        params = {"fieldIds": field_ids, "limit": 100}
        while url:
            resp = await client.post(url, params=params, json=body, headers=_bearer_headers())
            resp.raise_for_status()
            payload = resp.json()

            for entry in payload.get("data", []):
                out.append(_entity_to_company(entry))

            url = (payload.get("pagination") or {}).get("nextUrl")
            params = None      # nextUrl already carries fieldIds + cursor
    return out
