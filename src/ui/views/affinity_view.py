import asyncio
import csv
import json
import os
import re
from datetime import datetime, timezone, date
from urllib.parse import quote_plus

from config import LOGOS_DIR

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
                               QListWidgetItem, QLabel, QSplitter, QTextBrowser, QLineEdit,
                               QComboBox, QApplication, QInputDialog, QMenu, QMessageBox,
                               QFileDialog, QSpinBox, QDialog, QPlainTextEdit)
from PySide6.QtCore import Qt, QUrl, QByteArray, QSize, QEvent
from PySide6.QtGui import (QShortcut, QKeySequence, QIcon, QPixmap, QColor, QPalette,
                           QFont)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from qasync import asyncSlot

from services.affinity_service import (my_owner_id, list_my_companies, list_passed_companies, Company,
                                       company_url, get_company_notes, create_note,
                                       set_status, get_pass_reasons, get_statuses, Note,
                                       get_company_summary, Interaction,
                                       companies_to_json, companies_from_json,
                                       notes_to_json, notes_from_json, reach_out_suggestion,
                                       fit_score, proceed_recommendation,
                                       discover_owners, Owner, owners_to_json, owners_from_json,
                                       AffinityQuotaExhausted, PASSED_OPTION_ID,
                                       DEFAULT_GOOD_FIT_THRESHOLD, DEFAULT_PASS_THRESHOLD)
from services.fit_service import get_all_overrides, set_override, clear_override
from services.logo_service import get_logo_bytes, _cache_path
from ui.company_row_delegate import CompanyRowDelegate, GradientPanel, set_gradient_enabled
from services.outlook_service import get_calendar_events, get_message_by_interaction
from services.settings_service import get_setting, set_setting
from services.cache_service import read_cache, write_cache
from services.lists_service import (get_lists, get_list, create_list, get_members, add_company,
                                    remove_company, delete_list, rename_list, reassign_list,
                                    assign_unowned_to)
from services.local_notes_service import add_local_note, get_local_notes, delete_local_note
from services import raylu_service

# Cache keys for the local SQLite store.
CACHE_COMPANIES = "affinity.companies"
CACHE_OWNERS = "affinity.owners"      # discovered Deals-list owners for the owner dropdown
CACHE_AFFINITY_NOTES_PREFIX = "affinity.notes."   # per-company: CACHE_AFFINITY_NOTES_PREFIX + id
CACHE_PASS_REASONS = "affinity.pass_reasons"      # discovered Pass Reason dropdown field + options
CACHE_STATUSES = "affinity.statuses"              # discovered Status dropdown options
CACHE_PASSED = "affinity.passed"                  # the owner's Passed companies (separate filter)

# Owner-dropdown sentinel: the default "load the connected user's own deals" choice.
# Uses None as its userData so load() falls back to my_owner_id() (whoami).
MY_DEALS_LABEL = "My deals (default)"

# Best-guess PitchBook search URL. If it doesn't land on a search, do a search in
# the panel, copy the address-bar URL, and replace this template ({q} = query).
PITCHBOOK_SEARCH_URL = "https://my.pitchbook.com/search-results/s/all?query={q}"
PITCHBOOK_HOME_URL = "https://my.pitchbook.com"
# Raylu company URLs are internally generated (not name-based) → no pre-search;
# just open the app and let the user navigate/search inside the panel.
RAYLU_HOME_URL = "https://app.raylu.ai/"
# LinkedIn: open the enriched company URL directly when Affinity has it; otherwise
# fall back to a company search by name.
LINKEDIN_SEARCH_URL = "https://www.linkedin.com/search/results/companies/?keywords={q}"



def _plain(text: str) -> str:
    """Strip HTML tags so content shows as a readable one-line row."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _date(d: str | None) -> str:
    """Show just the date part of an ISO timestamp, or an em-dash if missing."""
    return d[:10] if d else "—"


def _blank_date(d: str | None) -> str:
    """Show just the date part of an ISO timestamp, or '---' if missing."""
    return d[:10] if d else "---"


REMINDERS_ORDER_OPTIONS = ["Newest Added", "Oldest Added",
                           "Newest Founded", "Oldest Founded",
                           "Highest Fit Score", "Lowest Fit Score",
                           "Most urgent first", "Least urgent first",
                           "Longest Since Raised", "Most Recently Raised"]

# Live "time since last raised" buckets shown in the reminders list dropdown. Membership
# is computed on the fly from each company's last funding date, so a company moves between
# buckets as time passes (unlike the user's hand-curated watchlists). Ranges are months,
# [lo, hi); the final bucket is open-ended (hi = None). Keyed by str so the dropdown can
# tell them apart from real lists (int ids).
RAISED_BUCKETS = [
    ("raised:0-6",   "Last raised 0–6 months ago",    0,  6),
    ("raised:6-12",  "Last raised 6–12 months ago",   6,  12),
    ("raised:12-18", "Last raised 12–18 months ago",  12, 18),
    ("raised:18+",   "Last raised 18–24+ months ago", 18, None),
]
_RAISED_RANGE = {key: (lo, hi) for key, _label, lo, hi in RAISED_BUCKETS}

# Live "upcoming meetings" list in the reminders dropdown: companies with a matched
# upcoming Outlook calendar event (populated once a refresh has loaded events). Keyed
# by str so it's told apart from custom lists (int ids), like the raised buckets.
MEETINGS_KEY = "meetings:upcoming"

# Live "Founding Date" list in the reminders dropdown: companies filtered by founding
# year via the Before/After + years-ago controls. Keyed by str like the other virtual lists.
FOUNDING_KEY = "founding:date"

LOGO_ICON_SIZE = QSize(28, 28)

# Company-list categories. The five EXCLUSIVE categories partition every company into
# exactly one bucket (met > emailed > untouched). "Upcoming meetings" and "Missing financial
# data" are OVERLAPPING views: a company appears in them regardless of its exclusive bucket —
# so it can show under both "Ongoing" and "Upcoming meetings". "all" is the combined view
# (exclusive buckets only, so nothing is double-counted).
CATEGORY_DEFS = [
    ("all", "All"),
    ("ongoing", "Ongoing"),                    # a meeting is logged
    ("upcoming", "Upcoming meetings"),         # OVERLAPS: matched upcoming calendar event
    ("followup", "Follow up"),                 # emailed and they replied
    ("noresponse", "Contacted, no response"),  # only we have emailed them
    ("missed", "Missed"),                      # untouched (whether or not it has a note)
    ("missingfin", "Missing financial data"),  # OVERLAPS: any Affinity funding field absent
    ("passed", "Passed"),                      # SEPARATE: the owner's Passed deals — NOT in "All"
]
_EXCLUSIVE_KEYS = ("ongoing", "followup", "noresponse", "missed")

# Detail-pane buttons: transparent fill so the company gradient shows through, with a
# theme-neutral outline + hover so they still read as buttons. QSS on QPushButton is
# safe (unlike on QListWidget items).
_DETAIL_BTN_QSS = """
QPushButton { background: transparent; border: 1px solid rgba(128,128,128,0.55);
    border-radius: 4px; padding: 5px 8px; }
QPushButton:hover { background: rgba(128,128,128,0.22); }
QPushButton:pressed { background: rgba(128,128,128,0.38); }
QPushButton:disabled { color: rgba(128,128,128,0.55); border-color: rgba(128,128,128,0.28); }
"""


def _blank_icon() -> QIcon:
    """A transparent placeholder so rows line up consistently before/without a real logo."""
    pixmap = QPixmap(LOGO_ICON_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    return QIcon(pixmap)


def _score_sort_value(company: Company | None, overrides: dict[int, int]) -> int:
    """Numeric sort value for fit-score ordering; missing data sorts lowest."""
    if company is None:
        return -1
    override = overrides.get(company.id)
    if override is not None:
        return override
    score = fit_score(company).score
    return score if score is not None else -1


def _reachout_sort_key(company: Company | None, most_urgent: bool) -> tuple[int, int]:
    """Sort key for reach-out urgency (earlier date = more urgent). Companies with no
    projected reach-out date always sort last, regardless of direction."""
    d = reach_out_suggestion(company).date if company else None
    if not d:
        return (1, 0)                          # undated → always last
    ordv = date.fromisoformat(d).toordinal()
    return (0, ordv if most_urgent else -ordv)


def _days_since_raised(company: Company | None) -> int | None:
    """Whole days between today and the company's last funding date (None if unknown)."""
    d = company.last_funding_date if company else None
    if not d:
        return None
    return (date.today() - date.fromisoformat(d[:10])).days


def _last_raised_label(company: Company | None) -> str:
    """Row annotation: 'Last raised YYYY-MM-DD' (or '—' when no funding date on file)."""
    d = company.last_funding_date if company else None
    return f"Last raised {d[:10]}" if d else "Last raised —"


def _last_raised_sort_key(company: Company | None, longest_first: bool) -> tuple[int, int]:
    """Sort key for time-since-last-raised. Companies with no funding date always sort
    last, regardless of direction."""
    days = _days_since_raised(company)
    if days is None:
        return (1, 0)                          # undated → always last
    return (0, -days if longest_first else days)


def _founded_sort_key(company: Company | None, newest_first: bool) -> tuple[int, int]:
    """Sort key for founding year. Companies with no founding year always sort last,
    regardless of direction."""
    year = company.year_founded if company else None
    if year is None:
        return (1, 0)                          # unknown founding year → always last
    return (0, -year if newest_first else year)


def _months_since_raised(company: Company | None) -> int | None:
    """Whole calendar months between the last funding date and today (None if unknown)."""
    d = company.last_funding_date if company else None
    if not d:
        return None
    raised = date.fromisoformat(d[:10])
    today = date.today()
    months = (today.year - raised.year) * 12 + (today.month - raised.month)
    if today.day < raised.day:                 # not a full month into the current one yet
        months -= 1
    return max(months, 0)


def _months_since_date(iso: str | None) -> int | None:
    """Whole calendar months between an ISO date and today (None if no date)."""
    if not iso:
        return None
    d = date.fromisoformat(iso[:10])
    today = date.today()
    months = (today.year - d.year) * 12 + (today.month - d.month)
    if today.day < d.day:
        months -= 1
    return max(months, 0)


def _last_contact_iso(company: Company) -> str | None:
    """Most recent past-contact date for a company (last email or last meeting)."""
    dates = [it.date[:10] for it in (company.last_email, company.last_event)
             if it and it.date]
    return max(dates) if dates else None


def _in_raised_bucket(company: Company | None, lo: int, hi: int | None) -> bool:
    """True if months-since-last-raised falls in [lo, hi) (hi None = open-ended)."""
    m = _months_since_raised(company)
    if m is None:
        return False
    return m >= lo and (hi is None or m < hi)


def _missing_financials(company: Company) -> bool:
    """True if any of Affinity's funding fields is absent: last raised date, last raised
    amount, or total amount raised."""
    return (not company.last_funding_date
            or company.last_funding_amount is None
            or company.total_funding_amount is None)


def sort_companies(items, mode: str, overrides: dict[int, int], key=lambda c: c):
    """Order items by a REMINDERS_ORDER_OPTIONS mode (shared by the reminders pane
    and the main company list so both stay consistent). `key` maps each item to its
    Company — identity for a bare company list; the reminders pane passes tuples and
    supplies a key that pulls out the (possibly None) company."""
    by_score = mode in ("Highest Fit Score", "Lowest Fit Score")
    by_reachout = mode in ("Most urgent first", "Least urgent first")
    by_raised = mode in ("Longest Since Raised", "Most Recently Raised")
    by_founded = mode in ("Newest Founded", "Oldest Founded")
    most_urgent = mode == "Most urgent first"
    longest_first = mode == "Longest Since Raised"
    newest_founded = mode == "Newest Founded"
    reverse = mode in ("Newest Added", "Highest Fit Score")
    items = list(items)
    if by_score:
        items.sort(key=lambda x: _score_sort_value(key(x), overrides), reverse=reverse)
    elif by_reachout:
        items.sort(key=lambda x: _reachout_sort_key(key(x), most_urgent))
    elif by_raised:
        items.sort(key=lambda x: _last_raised_sort_key(key(x), longest_first))
    elif by_founded:
        items.sort(key=lambda x: _founded_sort_key(key(x), newest_founded))
    else:                                       # "Newest Added" / "Oldest Added": by date added
        items.sort(key=lambda x: (key(x).added or "") if key(x) else "", reverse=reverse)
    return items


def _score_label(company: Company, overrides: dict[int, int]) -> str:
    """The fit score to display: a manual override (marked with *) if one is set,
    else the computed heuristic, or 'N/A' if there isn't enough data."""
    override = overrides.get(company.id)
    if override is not None:
        return f"{override}*"
    score = fit_score(company).score
    return str(score) if score is not None else "N/A"


def _factor_effect(factor: float) -> str:
    """Describe a cadence multiplier's direction: <1 pulls sooner, >1 pushes later."""
    if factor < 0.99:
        return "sooner"
    if factor > 1.01:
        return "later"
    return "no change"


def _reachout_calc_lines(r) -> list[str]:
    """The '- ...' bullet breakdown of how a reach-out date was derived (shared by both
    the timeline detail and the How-to-proceed panel)."""
    stage_note = r.investment_stage or "unknown stage"
    default_note = (" (no cadence set for this stage — used the default)"
                    if r.used_default_cadence else "")
    lines = [f"- Last raised: {_blank_date(r.last_funding_date)} ({stage_note})"]
    if r.is_accelerator:
        lines.append(f"- Small round ({_money(r.last_funding_amount)}) looks like an "
                     f"accelerator/pre-seed → base cadence {r.base_cadence_months} months "
                     f"(not the full {stage_note} cadence)")
    else:
        lines.append(f"- Base cadence for this stage: {r.base_cadence_months} months{default_note}")
    # Per-factor adjustments (only shown when the underlying data is present).
    if r.growth_yoy is not None:
        lines.append(f"- Headcount growth {r.growth_yoy:.0f}% YoY → "
                     f"{_factor_effect(r.growth_factor)} (×{r.growth_factor})")
    if r.runway_per_head is not None:
        lines.append(f"- Runway ~{_money(r.runway_per_head)}/employee → "
                     f"{_factor_effect(r.runway_factor)} (×{r.runway_factor})")
    if r.hires_3mo_pct is not None:
        lines.append(f"- Hiring {r.hires_3mo_pct:.0f}% in 3mo → "
                     f"{_factor_effect(r.hiring_factor)} (×{r.hiring_factor})")
    if r.cadence_months != r.base_cadence_months:
        lines.append(f"- Adjusted cadence: {r.cadence_months} months "
                     f"({_factor_effect(r.combined_factor)} overall)")
    lines.append(f"- Projected next round: ~{_blank_date(r.projected_next_round)}")
    lines.append(f"- Reach out {r.lead_months} months before that → **{_blank_date(r.date)}**")
    return lines


def _reach_out_markdown(r, heading: str = "Reach out suggestion") -> str:
    """Render a ReachOutSuggestion (date + the math behind it) as Markdown."""
    if not r.date:
        return (f"### {heading}\n\n---\n\n"
                "No suggestion — this company has no recorded funding date to project from.")
    return (f"### {heading}\n\n**{_blank_date(r.date)}**\n\n"
            f"**How this was calculated:**\n\n" + "\n".join(_reachout_calc_lines(r)))


def _proceed_markdown(rec, reach, today: str) -> str:
    """Render a ProceedRecommendation combined with reach-out timing as Markdown.
    - bad fit → just state it's a bad fit and suggest passing (no timing)
    - otherwise → show the reach-out date + the math, flagging a date already in the past."""
    score_txt = "N/A" if rec.score is None else str(rec.score)
    badge = {"good": "🟢", "marginal": "🟡", "pass": "🔴"}.get(rec.band, "⚪")
    lines = [f"### {badge} {rec.headline}", "", f"**Fit score: {score_txt}**", ""]
    if rec.reasons:
        lines.append("**Why:**")
        lines.append("")
        lines.extend(f"- {reason}" for reason in rec.reasons)
        lines.append("")

    # Bad fit → stop here; no point suggesting when to reach out.
    if rec.band == "pass":
        lines.append("**This looks like a bad fit — suggest passing.**")
        return "\n".join(lines)

    # Good / marginal / unknown → fold in the reach-out timing.
    lines.append("---")
    lines.append("")
    if not reach.date:
        lines.append("**When to reach out:** ---  (no funding date on file to project from)")
        return "\n".join(lines)

    if reach.date < today:
        lines.append(f"**When to reach out: {_blank_date(reach.date)}** "
                     "— ⚠️ this date has already passed; consider reaching out now.")
    else:
        lines.append(f"**When to reach out: {_blank_date(reach.date)}**")
    lines.append("")
    lines.extend(_reachout_calc_lines(reach))
    return "\n".join(lines)


def _money(amount: float | None) -> str:
    """Format a USD amount compactly (e.g. $6.3M), or '---' if missing."""
    if amount is None:
        return "---"
    if amount >= 1_000_000:
        return f"${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"${amount / 1_000:.0f}K"
    return f"${amount:,.0f}"


class NoCompanyColumnError(Exception):
    """Raised when an imported file has no company-name / company-domain column header."""


def _int_or_none(s: str | None) -> int | None:
    """Parse a stored setting into an int, or None if empty/invalid."""
    try:
        return int(s) if s else None
    except (TypeError, ValueError):
        return None


def _friendly(err: Exception) -> str:
    """Turn a raw exception into a short, plain-language message."""
    if isinstance(err, AffinityQuotaExhausted):
        return str(err)          # already a clear "quota exhausted — resets in N days" message
    text = str(err).lower()
    if "timed out" in text or "cancel" in text:
        return "Microsoft sign-in was cancelled or timed out — try again."
    if any(s in text for s in ("connect", "network", "getaddrinfo", "name or service", "ssl")):
        return "Couldn't reach the server — check your connection."
    if "401" in text or "unauthorized" in text:
        return "Not authorized — your login or API key may need refreshing."
    if "403" in text or "forbidden" in text:
        return "Access denied for that request."
    return f"Something went wrong: {err}"


def _titled(title: str, *widgets: QWidget) -> QWidget:
    """Wrap a bold title + widgets into one box (for use as a splitter pane)."""
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.addWidget(QLabel(f"<b>{title}</b>"))
    for w in widgets:
        v.addWidget(w)
    return box


class _PopoutWindow(QWidget):
    """Top-level window that hosts the reminders pane while it's popped out.
    Closing it (via the window ✕) docks the pane back rather than losing it."""

    def __init__(self, on_close, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlag(Qt.WindowType.Window, True)   # own top-level window
        self._on_close = on_close

    def closeEvent(self, event) -> None:
        self._on_close()
        super().closeEvent(event)


class AffinityView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._companies: list[Company] = []      # full loaded set (active statuses)
        self._passed: list[Company] = []          # the owner's Passed deals (separate "Passed" filter)
        # Owner scoping for custom lists: the connected user (from whoami, persisted) and the owner
        # whose lists are currently shown (follows the loaded owner; persisted for startup).
        self._connected_owner_id = _int_or_none(get_setting("pref.connected_owner_id"))
        self._current_owner_id = (_int_or_none(get_setting("pref.current_list_owner_id"))
                                  or self._connected_owner_id)
        self._current: Company | None = None
        self._web_source: str | None = None      # last web panel source (for auto-refresh)
        self._notes: list[Note] = []
        self._timeline: list[tuple[str, Interaction]] = []   # (label, interaction)
        self._events: list[dict] = []             # calendar events {company, date, subject}
        self._reminders: list[Company | None] = []   # rows of the reminders/list pane (None = not loaded)
        self._reminder_ids: list[int] = []            # company id per reminders row (for list removal)
        self._reminders_window: QWidget | None = None  # the popped-out reminders window, if any
        self._categorized: dict[str, list[Company]] = {}   # category key -> companies
        self._event_company_ids: set[int] = set()          # companies with an upcoming meeting
        self._overrides: dict[int, int] = get_all_overrides()   # company_id -> manual fit score
        self._logo_cache: dict[str, QIcon] = {}   # domain -> icon; value is the blank icon on a miss
        self._domain_rows: dict[str, list[tuple[QListWidget, int]]] = {}   # for patching icons in-place
        self._blank_icon = _blank_icon()
        self._logo_files: set[str] | None = None   # cached listing of non-empty logo filenames

        self.refresh_btn = QPushButton("Refresh from Affinity")
        self.refresh_btn.setToolTip("Re-fetch everything from Affinity (slow — minutes). "
                                    "The app shows cached data instantly on launch.")

        # Owner switcher: pick whose Deals-list companies the next Refresh loads. Populated
        # from cached owners discovered by "Find owners" (a full-list scan, skipping Passed).
        self.owner_selector = QComboBox()
        self.owner_selector.setToolTip(
            "Whose deals to load. Pick a colleague, then click “Refresh from Affinity”. "
            "Use “Find owners” to (re)build this list from the Deals list.")
        self.discover_owners_btn = QPushButton("Find owners")
        self.discover_owners_btn.setToolTip(
            "Scan active deals (New / Reached Out / Tracking) to collect everyone who owns one, "
            "and cache them for this dropdown. Runs once, then cached.")
        self._populate_owner_selector(owners=[])   # seed with the default item; cache fills it later
        self.generate_csv_btn = QPushButton("Generate CSV")
        self.generate_csv_btn.setToolTip(
            "Export the companies in the current view to a CSV file (name, domain, "
            "Affinity ID, last email/meeting, and funding fields).")
        self.update_status_btn = QPushButton("Update Status")
        self.update_status_btn.setToolTip(
            "Set every company in the selected list to a chosen Affinity status "
            "(Passed is one of the options).")
        self.status = QLabel("Loading cached companies…")

        # --- events widgets (shown in the "View Upcoming Events" popup, EventsDialog —
        # not part of this view's own layout; MainWindow reparents them into the dialog) ---
        self.events_status = QLabel("")
        self.events_list = QListWidget()

        # --- reminders pane (top) ---
        self.reminders_order = QComboBox()
        self.reminders_order.addItems(REMINDERS_ORDER_OPTIONS)
        self.reminders_order.setCurrentText(get_setting("pref.reminders_order", "Newest Added"))

        # "Founding Date" filter controls — shown only when that list is selected.
        self.founding_dir = QComboBox()
        self.founding_dir.addItems(["After", "Before"])
        self.founding_years = QSpinBox()
        self.founding_years.setRange(0, 200)
        self.founding_years.setValue(5)
        self.founding_controls = QWidget()
        _fc = QHBoxLayout(self.founding_controls)
        _fc.setContentsMargins(0, 0, 0, 0)
        _fc.addWidget(QLabel("Founded"))
        _fc.addWidget(self.founding_dir)
        _fc.addWidget(self.founding_years)
        _fc.addWidget(QLabel("years ago"))
        _fc.addStretch(1)
        self.founding_controls.setVisible(False)

        self.reminders_status = QLabel("")
        self.reminders_list = QListWidget()
        # larger, more spaced-out reminder rows (per request: ~1.3x font, ~1.5x spacing)
        _rem_font = self.reminders_list.font()
        _rem_font.setPointSizeF(_rem_font.pointSizeF() * 1.3)
        self.reminders_list.setFont(_rem_font)
        self.reminders_list.setSpacing(6)
        self.reminders_list.setIconSize(QSize(20, 20))         # downsized company logo
        # reminders keep the logo + styling but NO gradient backdrop
        self.reminders_list.setItemDelegate(
            CompanyRowDelegate(self.reminders_list, paint_gradient=False, fit_width=False))

        # list selector: built-in "Noted, not contacted" + custom watchlists, with a
        # "+" to create a new list
        self.list_selector = QComboBox()
        self.list_selector.setToolTip(
            "Your lists and live buckets. Right-click or double-click a custom list to rename it.")
        self.add_list_btn = QPushButton("+")
        self.add_list_btn.setFixedWidth(28)
        self.add_list_btn.setToolTip("Create a new list")
        self.del_list_btn = QPushButton("🗑")
        self.del_list_btn.setFixedWidth(28)
        self.del_list_btn.setToolTip("Delete the selected list")
        self.import_list_btn = QPushButton("Import")
        self.import_list_btn.setToolTip(
            "Create a list from a CSV/Excel (.xlsx) file or a pasted list of company names/domains")
        self.popout_btn = QPushButton("Pop out")
        self.popout_btn.setToolTip("Open the reminders pane in its own window")
        self._refresh_list_selector()
        reminders_header = QWidget()
        rh = QHBoxLayout(reminders_header)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(self.list_selector, 1)
        rh.addWidget(self.add_list_btn)
        rh.addWidget(self.del_list_btn)
        rh.addWidget(self.import_list_btn)
        rh.addWidget(self.popout_btn)

        # --- left column: search box + category dropdown + one company list ---
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search companies…")
        self.search_box.setClearButtonEnabled(True)

        # dropdown picks which category is shown in the single list below.
        self.category_selector = QComboBox()
        self.category_selector.setToolTip(
            "All  ·  Ongoing (a meeting is logged)  ·  Follow up (emailed, not met)  ·  "
            "Missed (no email or meeting)")
        for key, label in CATEGORY_DEFS:
            self.category_selector.addItem(label, key)

        # dropdown picks the sort order of the list below (mirrors the reminders pane).
        self.company_order = QComboBox()
        self.company_order.setToolTip("Sort the company list")
        self.company_order.addItems(REMINDERS_ORDER_OPTIONS)
        self.company_order.setCurrentText(get_setting("pref.company_order", "Newest Added"))

        self.company_list = QListWidget()
        self.company_list.setIconSize(LOGO_ICON_SIZE)
        _cfont = self.company_list.font()
        _cfont.setPointSize(_cfont.pointSize() + 2)
        self.company_list.setFont(_cfont)
        self.company_list.setSpacing(3)
        # no horizontal scrollbar: rows fit the column width (gradient rescales, text elides)
        self.company_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.company_list.setItemDelegate(CompanyRowDelegate(self.company_list))
        self._visible_companies: list[Company] = []   # companies currently shown in company_list

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.search_box)
        ll.addWidget(self.category_selector)
        ll.addWidget(self.company_order)
        ll.addWidget(self.company_list)

        # --- detail pane ---
        self.detail_name = QLabel("Select a company")
        self.detail_meta = QLabel("")
        self.pitchbook_btn = QPushButton("Open in PitchBook")
        self.raylu_btn = QPushButton("Open in Raylu")
        self.website_btn = QPushButton("Open website")
        self.linkedin_btn = QPushButton("Open LinkedIn")
        self.affinity_btn = QPushButton("Open in Affinity")
        self.activity_btn = QPushButton("Load Activity")
        self.suggestions_btn = QPushButton("Suggestions")
        self.add_to_list_btn = QPushButton("Add to list")
        self.add_to_list_menu = QMenu(self)
        self.add_to_list_btn.setMenu(self.add_to_list_menu)   # dropdown of lists
        self.add_to_list_menu.aboutToShow.connect(self._populate_add_to_list_menu)
        self.add_note_btn = QPushButton("Add note")
        for b in (self.pitchbook_btn, self.raylu_btn, self.website_btn, self.linkedin_btn,
                  self.affinity_btn, self.activity_btn, self.suggestions_btn,
                  self.add_to_list_btn, self.add_note_btn):
            b.setEnabled(False)
            b.setStyleSheet(_DETAIL_BTN_QSS)   # transparent so the panel gradient shows through
        self.detail_panel = GradientPanel()   # faint brand-color backdrop
        dl = QVBoxLayout(self.detail_panel)
        dl.addWidget(self.detail_name)
        dl.addWidget(self.detail_meta)
        dl.addWidget(self.pitchbook_btn)
        dl.addWidget(self.raylu_btn)
        dl.addWidget(self.website_btn)
        dl.addWidget(self.linkedin_btn)
        dl.addWidget(self.affinity_btn)
        dl.addWidget(self.activity_btn)
        dl.addWidget(self.suggestions_btn)
        dl.addWidget(self.add_to_list_btn)
        dl.addWidget(self.add_note_btn)
        dl.addStretch(1)

        # --- side view: summary + timeline + notes + reader ---
        self.summary_label = QLabel("")
        self.summary_label.setTextFormat(Qt.TextFormat.RichText)
        self.summary_label.setWordWrap(True)
        self.timeline_status = QLabel("")
        self.timeline_list = QListWidget()
        self.notes_status = QLabel("")
        self.notes_list = QListWidget()
        self.reader = QTextBrowser()
        self.reader.setOpenExternalLinks(True)
        self.reader.setStyleSheet("background: transparent;")   # let panel gradient show through
        # Make the timeline/notes lists transparent too — via the PALETTE (Base→transparent
        # + no viewport auto-fill), NOT a stylesheet, so item rendering stays palette-driven
        # (a QSS rule on a QListWidget is what caused the earlier row-inversion).
        for _lst in (self.timeline_list, self.notes_list):
            _p = _lst.palette()
            _p.setColor(QPalette.ColorRole.Base, QColor(Qt.GlobalColor.transparent))
            _lst.setPalette(_p)
            _lst.viewport().setAutoFillBackground(False)

        side_split = QSplitter(Qt.Orientation.Vertical)
        side_split.addWidget(_titled("Relationship (Affinity)", self.summary_label))
        side_split.addWidget(_titled("Timeline", self.timeline_status, self.timeline_list))
        side_split.addWidget(_titled("Notes", self.notes_status, self.notes_list))
        side_split.addWidget(_titled("Note details", self.reader))
        side_split.setStretchFactor(3, 2)

        self.side = GradientPanel()            # faint brand-color backdrop
        sv = QVBoxLayout(self.side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(side_split)
        self.side.setVisible(False)

        # --- Raylu Suggestions panel: raw MCP output + an "Enrich from Raylu" button ---
        self._raylu_session_tokens = 0             # running token total across enrich calls
        self.suggest_status = QLabel("")
        self.raylu_output = QTextBrowser()
        self.raylu_output.setStyleSheet("background: transparent;")   # panel gradient shows through
        _mono = QFont("Consolas")
        _mono.setStyleHint(QFont.StyleHint.Monospace)
        self.raylu_output.setFont(_mono)
        self.raylu_output.setLineWrapMode(QTextBrowser.LineWrapMode.NoWrap)   # raw JSON scrolls

        self.raylu_tokens = QLabel("")             # token usage, shown left of the button
        self.enrich_btn = QPushButton("Enrich from Raylu")
        self.enrich_btn.setStyleSheet(_DETAIL_BTN_QSS)
        raylu_bar = QHBoxLayout()
        raylu_bar.addStretch(1)                    # push the label + button to the bottom-right
        raylu_bar.addWidget(self.raylu_tokens)
        raylu_bar.addWidget(self.enrich_btn)

        self.suggestions_panel = GradientPanel()   # faint brand-color backdrop
        sgv = QVBoxLayout(self.suggestions_panel)
        sgv.setContentsMargins(0, 0, 0, 0)
        sgv.addWidget(self.suggest_status)
        sgv.addWidget(self.raylu_output, 1)
        sgv.addLayout(raylu_bar)
        self.suggestions_panel.setVisible(False)

        # --- selected company: detail | side (activity) | suggestions ---
        selected = QSplitter(Qt.Orientation.Horizontal)
        selected.addWidget(self.detail_panel)
        selected.addWidget(self.side)
        selected.addWidget(self.suggestions_panel)
        selected.setStretchFactor(1, 2)
        selected.setStretchFactor(2, 2)
        # the three panels share ONE continuous gradient spanning this splitter
        for _p in (self.detail_panel, self.side, self.suggestions_panel):
            _p.set_group(selected)
        selected.splitterMoved.connect(lambda *_: self._refresh_panel_gradients())

        # --- right column: reminders (top) | selected company (bottom), aligned vertically ---
        self.reminders_box = _titled("Reminders",
                                     reminders_header, self.reminders_order, self.founding_controls,
                                     self.reminders_status, self.reminders_list,
                                     self.update_status_btn)
        right_col = QSplitter(Qt.Orientation.Vertical)
        right_col.addWidget(self.reminders_box)
        right_col.addWidget(selected)
        right_col.setStretchFactor(1, 3)        # selected-company area gets most of the height
        self._reminders_dock = right_col        # splitter to dock the reminders box back into (index 0)

        # outer horizontal splitter: company list (its own full-height column) | right column
        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.addWidget(left)
        main_split.addWidget(right_col)
        main_split.setStretchFactor(1, 2)       # right column gets more width than the company list

        # everything built so far is the "content" (compresses left when the web panel opens)
        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        owner_row = QHBoxLayout()
        owner_row.setContentsMargins(0, 0, 0, 0)
        owner_row.addWidget(QLabel("Owner:"))
        owner_row.addWidget(self.owner_selector, 1)
        owner_row.addWidget(self.discover_owners_btn)
        cv.addLayout(owner_row)
        cv.addWidget(self.refresh_btn)
        cv.addWidget(self.generate_csv_btn)
        cv.addWidget(self.status)
        # stretch=1 so the splitter absorbs all surplus vertical space. Without it, a
        # horizontal QSplitter's vertical policy is only "Preferred", so leftover height
        # would instead inflate the status label above it (leaving a big gap).
        cv.addWidget(main_split, 1)

        # --- shared web panel (right side, hidden until requested) ---
        # One persistent profile holds logins for PitchBook, Raylu, Affinity web.
        self._web_profile = QWebEngineProfile("highlandx-web", self)   # named → logins persist
        self._web_profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.web_view = QWebEngineView()
        self.web_view.setPage(QWebEnginePage(self._web_profile, self.web_view))
        web_close = QPushButton("✕ Close")
        web_close.clicked.connect(self.hide_web_panel)
        self.web_panel = QWidget()
        pp = QVBoxLayout(self.web_panel)
        pp.setContentsMargins(0, 0, 0, 0)
        pp.addWidget(web_close)
        pp.addWidget(self.web_view)
        self.web_panel.setVisible(False)

        outer = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(content)
        outer.addWidget(self.web_panel)
        outer.setStretchFactor(0, 3)
        outer.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.addWidget(outer)

        self.refresh_btn.clicked.connect(self.load)
        self.discover_owners_btn.clicked.connect(self._discover_owners)
        self.update_status_btn.clicked.connect(self._update_status)
        self.generate_csv_btn.clicked.connect(self._generate_csv)
        self.search_box.textChanged.connect(self.apply_filter)
        self.reminders_order.currentIndexChanged.connect(lambda _i: self._on_order_changed())
        self.list_selector.currentIndexChanged.connect(lambda _i: self._build_reminders())
        self.founding_dir.currentIndexChanged.connect(lambda _i: self._build_reminders())
        self.founding_years.valueChanged.connect(lambda _v: self._build_reminders())
        self.list_selector.currentIndexChanged.connect(
            lambda _i: self.del_list_btn.setEnabled(isinstance(self.list_selector.currentData(), int)))
        self.add_list_btn.clicked.connect(self._create_list)
        self.del_list_btn.clicked.connect(self._delete_current_list)
        self.import_list_btn.clicked.connect(self._import_list)
        self.popout_btn.clicked.connect(self._toggle_popout)
        self.events_list.currentRowChanged.connect(self.select_from_event)
        self.reminders_list.currentRowChanged.connect(self.select_from_reminder)
        self.reminders_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.reminders_list.customContextMenuRequested.connect(self._reminders_context_menu)
        # rename a custom list: right-click the dropdown, or double-click an item in its popup
        self.list_selector.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_selector.customContextMenuRequested.connect(self._list_selector_context_menu)
        self.list_selector.view().viewport().installEventFilter(self)
        self.detail_name.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.detail_name.customContextMenuRequested.connect(self._detail_name_context_menu)
        self.company_list.currentRowChanged.connect(self._company_selected)
        self.category_selector.currentIndexChanged.connect(lambda _i: self._display_companies())
        self.company_order.currentIndexChanged.connect(lambda _i: self._on_company_order_changed())
        self.pitchbook_btn.clicked.connect(self.open_pitchbook)
        self.raylu_btn.clicked.connect(self.open_raylu)
        self.website_btn.clicked.connect(self.open_website)
        self.linkedin_btn.clicked.connect(self.open_linkedin)
        self.affinity_btn.clicked.connect(self.open_affinity)
        self.activity_btn.clicked.connect(self.load_activity)
        self.add_note_btn.clicked.connect(self.add_note)
        self.suggestions_btn.clicked.connect(self.show_suggestions)
        self.enrich_btn.clicked.connect(self.enrich_from_raylu)
        # itemClicked (not currentRowChanged) so re-clicking an already-selected row still
        # re-renders it — currentRowChanged only fires when the row index actually changes,
        # which breaks re-clicking the same timeline row after selecting something in notes
        # (each list tracks its own currentRow independently).
        self.timeline_list.itemClicked.connect(lambda item: self.show_timeline_entry(self.timeline_list.row(item)))
        self.notes_list.itemClicked.connect(lambda item: self.show_note(self.notes_list.row(item)))
        self.notes_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.notes_list.customContextMenuRequested.connect(self._notes_context_menu)

        # --- keyboard shortcuts (kept as attributes so they aren't garbage-collected) ---
        self._sc_find = QShortcut(QKeySequence.StandardKey.Find, self)        # Ctrl+F
        self._sc_find.activated.connect(self.search_box.setFocus)
        self._sc_reload = QShortcut(QKeySequence.StandardKey.Refresh, self)   # F5
        self._sc_reload.activated.connect(
            lambda: self.load() if self.refresh_btn.isEnabled() else None)    # ignore while loading
        self._sc_esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)       # Esc
        self._sc_esc.activated.connect(self.hide_web_panel)

        # Render instantly from the last cached fetch (no network, no MS login).
        self.load_from_cache()

        # Remember the user's panel proportions across launches (saved on quit).
        self._splitters = {
            "selected": selected, "side": side_split,
            "right_col": right_col, "main": main_split,
        }
        self._restore_layout()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._save_layout)

    @asyncSlot()
    async def load(self) -> None:
        self.refresh_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        owner_name = self.owner_selector.currentText()
        self.status.setText(f"Refreshing {owner_name}'s deals from Affinity…")
        try:
            # The connected user (whoami — cheap, quota-exempt). Used to resolve "My deals" and to
            # own any pre-existing unassigned lists (a one-time migration).
            connected = await my_owner_id()
            if connected != self._connected_owner_id:
                self._connected_owner_id = connected
                set_setting("pref.connected_owner_id", str(connected))
                assign_unowned_to(connected)     # hand legacy lists to the connected user
            # Selected owner's person id, or the connected user for "My deals".
            pid = self.owner_selector.currentData() or connected
            self._companies = await list_my_companies(pid)
            self._loaded_pid = pid          # for the background Passed fetch
            self._passed = []               # clear the previous owner's Passed set until reloaded
            # Custom lists follow the loaded owner (persist for startup).
            self._current_owner_id = pid
            set_setting("pref.current_list_owner_id", str(pid))
        except Exception as err:
            self.status.setText(_friendly(err))
            QApplication.restoreOverrideCursor()
            self.refresh_btn.setEnabled(True)
            return

        # Companies are in hand: persist + render them NOW, without waiting on the slow
        # notes-index and Outlook events. Those finish in the background and update their
        # own panes when ready, so the company list is usable in seconds.
        updated = write_cache(CACHE_COMPANIES, companies_to_json(self._companies))
        self._refresh_list_selector()           # show the loaded owner's custom lists
        self.apply_filter()                     # partitions + builds reminders + sets status
        self.status.setText(
            self.status.text() + f"  ·  updated {self._ago(updated)} — loading events…")
        QApplication.restoreOverrideCursor()
        asyncio.ensure_future(self._finish_load_background())

    def _populate_owner_selector(self, owners: list[Owner]) -> None:
        """Rebuild the owner dropdown: a default "My deals" item (userData=None) followed by
        each discovered owner (userData = their person id). Preserves the current selection
        by person id when possible."""
        prev = self.owner_selector.currentData()
        self.owner_selector.blockSignals(True)
        self.owner_selector.clear()
        self.owner_selector.addItem(MY_DEALS_LABEL, None)
        for o in owners:
            self.owner_selector.addItem(o.name, o.id)
        # Restore the prior pick if it survived the rebuild; else fall back to the default.
        idx = self.owner_selector.findData(prev) if prev is not None else 0
        self.owner_selector.setCurrentIndex(idx if idx >= 0 else 0)
        self.owner_selector.blockSignals(False)

    def _load_owners_from_cache(self) -> None:
        """Fill the owner dropdown from the locally cached owner list (instant, no network)."""
        text, _ = read_cache(CACHE_OWNERS)
        if not text:
            return
        try:
            self._populate_owner_selector(owners_from_json(text))
        except Exception:
            pass       # a bad/old cache shouldn't break startup — just leave the default item

    @asyncSlot()
    async def _discover_owners(self) -> None:
        """Scan active deals for distinct owners, cache them, and fill the dropdown."""
        self.discover_owners_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.status.setText("Finding deal owners (scanning active deals)…")

        def _progress(scanned: int, found: int) -> None:
            self.status.setText(
                f"Finding deal owners… scanned {scanned:,} active deals, {found} owners so far.")

        try:
            owners = await discover_owners(progress=_progress)
        except Exception as err:
            self.status.setText(_friendly(err))
            return
        finally:
            QApplication.restoreOverrideCursor()
            self.discover_owners_btn.setEnabled(True)
        write_cache(CACHE_OWNERS, owners_to_json(owners))
        self._populate_owner_selector(owners)
        self.status.setText(f"Found {len(owners)} deal owners — pick one and Refresh.")

    async def _finish_load_background(self) -> None:
        """The slow, non-critical parts of a refresh — run after companies are on screen."""
        try:
            # The owner's Passed deals — a second filtered fetch, kept off the critical path
            # since it's a separate call and can be large. Populates the "Passed" filter.
            await self._load_passed()
            # Refresh makes only the companies call (v2). The Outlook calendar is Microsoft
            # Graph, not Affinity, so it doesn't touch the Affinity quota. (The old per-company
            # notes-index that used to run here was removed when "Noted, not contacted" was
            # merged into "Missed".)
            await self._load_events()              # Outlook calendar (Graph, not Affinity quota)
        finally:
            self.refresh_btn.setEnabled(True)
            self.status.setText(self.status.text().replace(" — loading events…", ""))

    async def _load_passed(self) -> None:
        """Fetch the loaded owner's Passed deals into the separate "Passed" filter, then cache."""
        pid = getattr(self, "_loaded_pid", None)
        if pid is None:
            return
        try:
            self._passed = await list_passed_companies(pid)
        except Exception:
            return                                 # leave the Passed filter empty on failure
        write_cache(CACHE_PASSED, companies_to_json(self._passed))
        self.apply_filter()                        # re-bucket so the "Passed" filter fills in

    def load_from_cache(self) -> None:
        """Populate the UI from the local SQLite cache — instant, no network, no MS login."""
        self._load_owners_from_cache()          # fill the owner dropdown from cache
        text, updated = read_cache(CACHE_COMPANIES)
        if not text:
            self.status.setText("No cached data yet — click “Refresh from Affinity”.")
            return
        try:
            self._companies = companies_from_json(text)
            passed_text, _ = read_cache(CACHE_PASSED)
            self._passed = companies_from_json(passed_text) if passed_text else []
        except Exception as err:
            self.status.setText(_friendly(err))
            return
        self.apply_filter()                     # partitions + builds reminders + sets status
        self.status.setText(
            self.status.text() + f"  ·  cached {self._ago(updated)} (Refresh to update)")

    @staticmethod
    def _ago(iso: str | None) -> str:
        """Human-readable 'time since' for an ISO-8601 timestamp."""
        if not iso:
            return "unknown"
        try:
            then = datetime.fromisoformat(iso)
            secs = int((datetime.now(timezone.utc) - then).total_seconds())
        except Exception:
            return "unknown"
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{secs // 60} min ago"
        if secs < 86400:
            return f"{secs // 3600} h ago"
        return f"{secs // 86400} d ago"

    def apply_filter(self) -> None:
        """Filter by search term, bucket into mutually-exclusive categories, show selection."""
        term = self.search_box.text().strip().lower()
        visible = [c for c in self._companies if term in c.name.lower()]
        self._event_company_ids = {e["company"].id for e in self._events}
        buckets: dict[str, list[Company]] = {k: [] for k in _EXCLUSIVE_KEYS}
        for c in visible:
            buckets[self._categorize(c)].append(c)
        # "Upcoming meetings" overlaps the exclusive buckets (a company can be in both).
        buckets["upcoming"] = [c for c in visible if c.id in self._event_company_ids]
        # "Missing financial data" also overlaps: any company missing an Affinity funding field.
        buckets["missingfin"] = [c for c in visible if _missing_financials(c)]
        # "Passed" is a SEPARATE set (from list_passed_companies), not part of self._companies, so
        # it never appears in "All" or any exclusive bucket. Still honors the search term.
        buckets["passed"] = [c for c in self._passed if term in c.name.lower()]
        self._categorized = buckets
        self._update_cat_counts()
        self._display_companies()               # fills the single list + starts logo fetch
        self._build_reminders()
        self.status.setText(f"{len(self._companies)} companies")

    def _categorize(self, c: Company) -> str:
        """The company's single EXCLUSIVE bucket (upcoming meetings is handled separately)."""
        if c.met:
            return "ongoing"
        if c.emailed:
            return "followup" if self._has_response(c) else "noresponse"
        return "missed"

    def _has_response(self, c: Company) -> bool:
        """True if the company has emailed us back — a known email is from their domain."""
        dom = (c.domain or "").lower()
        if not dom:
            return False
        for e in (c.first_email, c.last_email):
            if e and e.from_address and dom in e.from_address.lower():
                return True
        return False

    def _companies_for_category(self, key: str) -> list[Company]:
        """The company list for a dropdown category key."""
        if key == "all":
            out: list[Company] = []
            for k in _EXCLUSIVE_KEYS:            # exclude "upcoming" so companies aren't duplicated
                out += self._categorized.get(k, [])
            return out
        return self._categorized.get(key, [])

    def _display_companies(self) -> None:
        """Show the currently-selected category in the single company list."""
        key = self.category_selector.currentData() or "all"
        self._visible_companies = sort_companies(
            self._companies_for_category(key),
            self.company_order.currentText(), self._overrides)
        self._fill(self.company_list, self._visible_companies, self._overrides)
        self._start_logo_fetch()

    def _company_selected(self, row: int) -> None:
        if 0 <= row < len(self._visible_companies):
            self.select_company(self._visible_companies[row])

    def _generate_csv(self) -> None:
        """Export the companies in the current view to a CSV file the user picks."""
        companies = self._visible_companies
        if not companies:
            QMessageBox.information(self, "Generate CSV", "No companies in the current view.")
            return

        category = dict(CATEGORY_DEFS).get(
            self.category_selector.currentData() or "all", "companies")
        slug = re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-") or "companies"
        path, _ = QFileDialog.getSaveFileName(
            self, "Generate CSV", f"highlandx-{slug}.csv", "CSV files (*.csv)")
        if not path:
            return                              # user cancelled

        def _d(iso: str | None) -> str:         # date part of an ISO timestamp, or blank
            return iso[:10] if iso else ""

        def _amt(v: float | None) -> str:
            return "" if v is None else str(v)

        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["Company name", "Company domain", "Company Affinity ID",
                            "Last email", "Last meeting", "Last raised",
                            "Amount last raised", "Total amount raised"])
                for c in companies:
                    w.writerow([
                        c.name,
                        c.domain or "",
                        c.id,
                        _d(c.last_email.date if c.last_email else None),
                        _d(c.last_event.date if c.last_event else None),
                        _d(c.last_funding_date),
                        _amt(c.last_funding_amount),
                        _amt(c.total_funding_amount),
                    ])
        except OSError as e:
            QMessageBox.warning(self, "Generate CSV", f"Couldn't write the file:\n{e}")
            return

        self.status.setText(f"Wrote {len(companies)} companies to {path}")

    @asyncSlot()
    async def _update_status(self) -> None:
        """Bulk-set every company in the selected reminders list to a chosen Affinity status.
        Shared-data write — gated behind status selection + an explicit confirmation. Setting
        the status to "Passed" runs the extra pass failsafes and writes a pass reason."""
        companies = [c for c in self._reminders if c and c.list_entry_id]
        # self._reminders can hold None (a list member not in the current load) — those have no
        # loaded list entry, so they're excluded from the count below.
        skipped = len(self._reminders) - len(companies)
        if not companies:
            rows = len(self._reminders)
            no_entry = sum(1 for c in self._reminders if c and not c.list_entry_id)
            QMessageBox.information(
                self, "Update Status",
                f"No companies to update. This list has {rows} row(s): "
                f"{no_entry} loaded but without an Affinity list-entry id, and "
                f"{rows - no_entry - len(companies)} not loaded in the current owner's data.\n\n"
                "Refresh from Affinity for the owner these companies belong to, then try again.")
            return

        list_name = self.list_selector.currentText() or "this list"
        picked = await self._pick_status(len(companies), list_name)
        if picked is None:
            return                                 # cancelled / unavailable at the status step
        status_id, status_text = picked
        is_pass = status_id == PASSED_OPTION_ID

        # Skip companies already at the target status — no write needed, saves one API call each.
        target = status_text.strip().lower()
        already = [c for c in companies if (c.status or "").strip().lower() == target]
        companies = [c for c in companies if (c.status or "").strip().lower() != target]
        if not companies:
            QMessageBox.information(
                self, "Update Status",
                f"All {len(already)} companies are already set to “{status_text}”. "
                "Nothing to update.")
            return

        # Shared failsafes for ANY status change: never touch Portfolio companies, and flag
        # recently-contacted / recently-met ones for review (protected by default).
        result = self._safeguard_review(companies, skipped, list_name, status_text, is_pass)
        if result is None:
            return                                 # cancelled or nothing selected
        to_change, kept = result

        reason = None
        if is_pass:                                # Passed also writes a pass reason
            reason = await self._pick_pass_reason()
            if reason is None:
                return                             # cancelled at the reason step
        await self._apply_status_updates(to_change, status_id, status_text, reason,
                                         kept=kept, already_same=len(already))

    async def _pick_status(self, count: int, list_name: str) -> "tuple[int, str] | None":
        """Load the Affinity Status options and let the user pick a target status for the batch.
        Returns (option_id, text), or None if cancelled / unavailable."""
        statuses = None
        try:
            statuses = await get_statuses()
            if statuses:
                write_cache(CACHE_STATUSES, json.dumps(statuses))
        except Exception:
            statuses = None
        if not statuses:                            # live fetch failed → fall back to cache
            cached, _ = read_cache(CACHE_STATUSES)
            if cached:
                try:
                    statuses = json.loads(cached)
                except Exception:
                    statuses = None
        if not statuses:
            QMessageBox.warning(self, "Update Status",
                                "Couldn't load the Affinity statuses. Try again in a moment.")
            return None

        texts = [s["text"] for s in statuses]
        choice, ok = QInputDialog.getItem(
            self, "Update Status",
            f"Set the {count} companies in “{list_name}” to which status?", texts, 0, False)
        if not ok:
            return None
        s = next(x for x in statuses if x["text"] == choice)
        return s["id"], s["text"]

    def _safeguard_review(self, companies, skipped, list_name, status_text, is_pass):
        """Shared bulk-change failsafes (applied for ANY target status): exclude Portfolio
        companies entirely, and flag recently-contacted / recently-met ones for review
        (protected by default). Returns (to_change, kept) or None if cancelled / nothing left."""
        months = int(get_setting("pref.pass_recent_contact_months", "3"))
        portfolio = [c for c in companies if (c.status or "").strip().lower() == "portfolio"]
        candidates = [c for c in companies if c not in portfolio]
        recent: list[tuple[Company, str]] = []   # (company, last-contact date)
        safe: list[Company] = []
        for c in candidates:
            iso = _last_contact_iso(c)
            m = _months_since_date(iso)
            if m is not None and m < months:      # recently emailed or met → protect + review
                recent.append((c, iso))
            else:
                safe.append(c)

        if recent:
            chosen = self._review_recent_contacts(
                list_name, safe, recent, portfolio, months, status_text)
            if chosen is None:                    # cancelled
                return None
            to_change = safe + chosen
        else:
            note = ""
            if portfolio:
                note += f"\n\n({len(portfolio)} portfolio companies will NOT be changed.)"
            if skipped:
                note += f"\n({skipped} with no loaded entry will be skipped.)"
            undo = " and can't be easily undone" if is_pass else ""
            reply = QMessageBox.warning(
                self, "Update Status",
                f"Set {len(safe)} companies in “{list_name}” to “{status_text}” in Affinity?\n\n"
                f"This changes shared CRM data for your whole team{undo}." + note,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            if reply != QMessageBox.StandardButton.Yes:
                return None
            to_change = safe

        if not to_change:
            QMessageBox.information(self, "Update Status", "No companies selected.")
            return None
        kept = len(portfolio) + (len(recent) - (len(to_change) - len(safe)))
        return to_change, kept

    async def _apply_status_updates(self, companies, status_id, status_text, reason,
                                    kept=0, already_same=0) -> None:
        """Write the chosen status (and optional pass reason) to each company, concurrently.

        Progress is shown by updating the status LABEL — deliberately NOT a modal QProgressDialog:
        a modal dialog's setValue() calls QApplication.processEvents(), which re-enters the qasync
        event loop while these tasks are running and raises "Cannot enter into task…". A plain
        label update just schedules a repaint (handled between awaits), so it never re-enters.
        """
        total = len(companies)
        self.update_status_btn.setEnabled(False)
        self.status.setText(f"Updating {total} companies to “{status_text}” in Affinity… (0/{total})")

        sem = asyncio.Semaphore(8)
        done = succeeded = failed = 0
        first_error: Exception | None = None

        async def _one(c: Company) -> None:
            nonlocal done, succeeded, failed, first_error
            async with sem:
                try:
                    await set_status(c.list_entry_id, status_id, reason=reason or None)
                    succeeded += 1
                except Exception as e:
                    failed += 1
                    if first_error is None:
                        first_error = e
                finally:
                    done += 1
                    self.status.setText(          # safe label-only update (no processEvents)
                        f"Updating to “{status_text}”… {done}/{total} "
                        f"({succeeded} ok" + (f", {failed} failed" if failed else "") + ")")

        await asyncio.gather(*(_one(c) for c in companies))

        reason_note = f" · reason: {reason['text']}" if reason and reason.get("text") else ""
        self.status.setText(
            f"Set {succeeded} companies to “{status_text}”"
            + (f" · {failed} failed" if failed else "")
            + (f" · {kept} kept" if kept else "")
            + (f" · {already_same} already set" if already_same else "") + reason_note + ".")
        self.update_status_btn.setEnabled(True)

        # Final summary (shown after all tasks finish — no concurrent tasks, so a modal box is fine).
        summary = f"{succeeded} of {total} companies updated to “{status_text}”."
        if already_same:
            summary += f"\n{already_same} were already set to “{status_text}” — skipped (no API call)."
        if failed and first_error is not None:
            summary += f"\n\n{failed} failed. First error: {_friendly(first_error)}"
        QMessageBox.information(self, "Update Status", summary)

        if succeeded:
            self.load()          # refresh — status changes can drop companies from the active view

    async def _pick_pass_reason(self) -> "dict | None":
        """Load the Pass Reason options and let the user pick one for the whole batch.

        Returns a reason dict {field_id, value_type, option_id, text} to write, an empty dict
        {} to pass with NO reason, or None to cancel the Pass All entirely."""
        reasons = None
        try:
            reasons = await get_pass_reasons()
            if reasons and reasons.get("options"):
                write_cache(CACHE_PASS_REASONS, json.dumps(reasons))   # cache for offline reuse
        except Exception:
            reasons = None
        if not reasons or not reasons.get("options"):     # live fetch failed → try cache
            cached, _ = read_cache(CACHE_PASS_REASONS)
            if cached:
                try:
                    reasons = json.loads(cached)
                except Exception:
                    reasons = None

        if not reasons or not reasons.get("options"):
            reply = QMessageBox.question(
                self, "Pass reason",
                "Couldn't load pass reasons from Affinity. Pass without writing a reason?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel)
            return {} if reply == QMessageBox.StandardButton.Yes else None

        no_reason = "(no reason)"
        texts = [no_reason] + [o["text"] for o in reasons["options"]]
        choice, ok = QInputDialog.getItem(
            self, "Pass reason", "Reason to write for the passed companies:", texts, 0, False)
        if not ok:
            return None
        if choice == no_reason:
            return {}
        opt = next(o for o in reasons["options"] if o["text"] == choice)
        return {"field_id": reasons["field_id"], "value_type": reasons.get("value_type"),
                "option_id": opt["id"], "text": opt["text"]}

    def _review_recent_contacts(self, list_name, safe, recent, portfolio, months, status_text):
        """Warn about recently-contacted companies before a bulk status change. Returns the list
        of recently-contacted companies the user chose to include, or None if cancelled."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Update Status — review recent contacts")
        dlg.resize(460, 460)
        lay = QVBoxLayout(dlg)
        summary = (f"Setting companies in “{list_name}” to “{status_text}”.\n\n"
                   f"{len(safe)} will be updated automatically.\n"
                   f"{len(recent)} were contacted within {months} month(s) (recently emailed or "
                   "met) — check any you still want to change (unchecked stays untouched).")
        if portfolio:
            summary += f"\n{len(portfolio)} portfolio companies will NOT be changed."
        summary_lbl = QLabel(summary)
        summary_lbl.setWordWrap(True)
        lay.addWidget(summary_lbl)

        review = QListWidget()
        for c, iso in recent:
            item = QListWidgetItem(f"{c.name}  ·  last contact {iso}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)     # protect recent contacts by default
            item.setData(Qt.ItemDataRole.UserRole, c)
            review.addItem(item)
        lay.addWidget(review, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dlg.reject)
        proceed_btn = QPushButton("Proceed")
        proceed_btn.clicked.connect(dlg.accept)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(proceed_btn)
        lay.addLayout(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        chosen = []
        for i in range(review.count()):
            it = review.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                chosen.append(it.data(Qt.ItemDataRole.UserRole))
        return chosen

    def _fill(self, widget: QListWidget, companies: list[Company],
             overrides: dict[int, int]) -> None:
        widget.blockSignals(True)
        widget.clear()
        for c in companies:
            item = QListWidgetItem(
                f"{c.name}  ({_score_label(c, overrides)})  ·  {_last_raised_label(c)}")
            item.setIcon(self._row_icon(c.domain))            # lazy disk icon (or blank if none)
            item.setData(Qt.ItemDataRole.UserRole, c.domain)   # delegate reads this for brand colors
            widget.addItem(item)
        widget.setCurrentRow(-1)
        widget.blockSignals(False)

    def _start_logo_fetch(self) -> None:
        """Kick off (fire-and-forget) fetching logos not already in this session's cache,
        for every company currently shown in the company list."""
        self._domain_rows = {}
        for row, c in enumerate(self._visible_companies):
            if c.domain:
                self._domain_rows.setdefault(c.domain, []).append((self.company_list, row))
        missing = [d for d in self._domain_rows if d not in self._logo_cache]
        if missing:
            asyncio.ensure_future(self._fetch_logos(missing))

    async def _fetch_logos(self, domains: list[str]) -> None:
        sem = asyncio.Semaphore(20)

        async def fetch_one(domain: str) -> None:
            async with sem:
                try:
                    data = await get_logo_bytes(domain)
                except Exception:
                    data = None
                icon = self._blank_icon
                if data:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(data):
                        icon = QIcon(pixmap)
                self._logo_cache[domain] = icon
                if icon is not self._blank_icon:
                    self._apply_icon(domain, icon)

        await asyncio.gather(*(fetch_one(d) for d in domains))

    def _apply_icon(self, domain: str, icon: QIcon) -> None:
        """Patch the icon into any already-rendered rows for this domain, in place."""
        for widget, row in self._domain_rows.get(domain, []):
            item = widget.item(row)
            if item is not None:
                item.setIcon(icon)

    def _update_cat_counts(self) -> None:
        """Show live counts in the category dropdown labels (without firing its signal)."""
        total = sum(len(self._categorized.get(k, [])) for k in _EXCLUSIVE_KEYS)
        labels = dict(CATEGORY_DEFS)
        self.category_selector.blockSignals(True)
        for i in range(self.category_selector.count()):
            key = self.category_selector.itemData(i)
            n = total if key == "all" else len(self._categorized.get(key, []))
            self.category_selector.setItemText(i, f"{labels[key]} ({n})")
        self.category_selector.blockSignals(False)

    def _refresh_list_selector(self) -> None:
        """Populate the list dropdown: the live 'last raised' buckets first, then the
        user's custom watchlists below a separator."""
        current = self.list_selector.currentData()   # remember selection (bucket key | list_id)
        self.list_selector.blockSignals(True)
        self.list_selector.clear()
        self.list_selector.addItem("Upcoming meetings", MEETINGS_KEY)
        for key, label, _lo, _hi in RAISED_BUCKETS:
            self.list_selector.addItem(label, key)
        self.list_selector.addItem("Founding Date", FOUNDING_KEY)
        lists = get_lists(self._current_owner_id)     # only the current owner's custom lists
        if lists:
            self.list_selector.insertSeparator(self.list_selector.count())
            for lid, name in lists:
                self.list_selector.addItem(name, lid)
        idx = self.list_selector.findData(current)
        self.list_selector.setCurrentIndex(idx if idx >= 0 else 0)   # default: first bucket
        self.list_selector.blockSignals(False)
        self.del_list_btn.setEnabled(isinstance(self.list_selector.currentData(), int))

    def _create_list(self) -> None:
        name, ok = QInputDialog.getText(self, "New list", "List name:")
        if not ok or not name.strip():
            return
        lid = create_list(name.strip(), self._current_owner_id)   # belongs to the current owner
        self._refresh_list_selector()
        idx = self.list_selector.findData(lid)
        if idx >= 0:
            self.list_selector.setCurrentIndex(idx)   # fires _build_reminders via the signal

    def _import_list(self) -> None:
        """Create a watchlist from a CSV file or a pasted list of company names/domains,
        matched against the loaded deals list."""
        if not self._companies and not self._passed:
            QMessageBox.information(self, "Import list",
                                    "No companies loaded yet — refresh from Affinity first.")
            return

        # pick a source
        box = QMessageBox(self)
        box.setWindowTitle("Import list")
        box.setText("Where are the company names / domains coming from?")
        csv_choice = box.addButton("From file (CSV / Excel)", QMessageBox.ButtonRole.AcceptRole)
        paste_choice = box.addButton("Paste a list", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()

        tokens: list[str] = []
        if clicked is csv_choice:
            path, _ = QFileDialog.getOpenFileName(
                self, "Import list", "",
                "Spreadsheet / CSV / text (*.xlsx *.csv *.txt);;All files (*)")
            if not path:
                return
            try:
                tokens = self._read_tokens_from_file(path)
            except NoCompanyColumnError:
                QMessageBox.warning(
                    self, "Import list",
                    "There is no company name or company domain column. "
                    "Please check the columns of the uploaded file again.")
                return
            except Exception as e:      # OSError, or openpyxl parse/format errors
                QMessageBox.warning(self, "Import list", f"Couldn't read the file:\n{e}")
                return
        elif clicked is paste_choice:
            text, ok = QInputDialog.getMultiLineText(
                self, "Import list",
                "Company names or domains (one per line or comma-separated):", "")
            if not ok or not text.strip():
                return
            tokens = re.split(r"[\n,]", text)
        else:
            return                                 # cancelled

        matched, unmatched = self._match_tokens(tokens)
        if not matched:
            # Nothing matched, but still let the user copy the unidentified entries.
            if unmatched:
                self._show_unmatched_dialog(
                    unmatched, "None of those matched a company in your deals list.")
            else:
                QMessageBox.information(self, "Import list", "Nothing to import.")
            return

        op = self._choose_import_operation()
        if op is None:
            return                                     # cancelled at the operation/destination step
        operation, lid, list_name, is_new = op

        # Exceptions: let the user uncheck matched companies to exclude from this operation.
        verb = "adding to" if operation == "add" else "removing from"
        selected = self._review_import_selection(matched, verb, list_name)
        if selected is None:
            return                                     # cancelled at the review step
        if not selected:
            QMessageBox.information(self, "Import list", "No companies selected.")
            return

        if operation == "add":
            already_ids = {cid for cid, _ in get_members(lid)}
            newly = [c for c in selected if c.id not in already_ids]
            for c in selected:
                add_company(lid, c.id, c.name)         # dedupes: no-op if already a member
            if is_new:
                summary = f"Created “{list_name}” with {len(selected)} companies."
            else:
                summary = f"Added {len(newly)} companies to “{list_name}”."
                already = len(selected) - len(newly)
                if already:
                    summary += f" ({already} already in the list.)"
        else:  # remove
            member_ids = {cid for cid, _ in get_members(lid)}
            removed = [c for c in selected if c.id in member_ids]
            for c in selected:
                remove_company(lid, c.id)              # no-op if it isn't a member
            summary = f"Removed {len(removed)} companies from “{list_name}”."
            not_member = len(selected) - len(removed)
            if not_member:
                summary += f" ({not_member} weren’t in the list.)"

        self._refresh_list_selector()
        idx = self.list_selector.findData(lid)
        if idx >= 0:
            self.list_selector.setCurrentIndex(idx)   # select it → fires _build_reminders
        self._build_reminders()                       # reflect removals when the list is already shown

        self.status.setText(summary)
        if unmatched:
            self._show_unmatched_dialog(unmatched, summary)
        else:
            QMessageBox.information(self, "Import list", summary)

    def _choose_import_operation(self) -> "tuple[str, int, str, bool] | None":
        """Ask whether to ADD the matched companies to a list or REMOVE them from one, then pick
        the target list. Returns (operation, list_id, list_name, is_new) — operation is "add" or
        "remove" — or None if cancelled. Remove is only offered when a list already exists."""
        existing = get_lists(self._current_owner_id)
        if existing:
            box = QMessageBox(self)
            box.setWindowTitle("Import list")
            box.setText("Add the matched companies to a list, or remove them from one?")
            add_btn = box.addButton("Add to a list", QMessageBox.ButtonRole.AcceptRole)
            remove_btn = box.addButton("Remove from a list", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is remove_btn:
                picked = self._pick_existing_list("Remove the companies from which list?")
                return None if picked is None else ("remove", picked[0], picked[1], False)
            if clicked is not add_btn:
                return None                            # cancelled

        dest = self._choose_import_destination()       # New vs Existing (add path)
        if dest is None:
            return None
        lid, name, is_new = dest
        return "add", lid, name, is_new

    def _pick_existing_list(self, prompt: str) -> "tuple[int, str] | None":
        """Pick one of the user's existing lists by name. Returns (list_id, name) or None."""
        existing = get_lists(self._current_owner_id)
        if not existing:
            QMessageBox.information(self, "Import list", "You have no lists yet.")
            return None
        names = [nm for _, nm in existing]
        name, ok = QInputDialog.getItem(self, "Import list", prompt, names, 0, False)
        if not ok or not name:
            return None
        return next(i for i, nm in existing if nm == name), name

    def _review_import_selection(self, matched: list[Company], verb: str,
                                 list_name: str) -> "list[Company] | None":
        """Show the matched companies with checkboxes (all checked) so the user can exclude some
        ('exceptions'). Returns the checked companies, or None if cancelled."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Import list — choose companies")
        dlg.resize(440, 420)
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"{len(matched)} companies matched. Uncheck any to exclude from "
            f"{verb} “{list_name}”:"))
        lw = QListWidget()
        for c in matched:
            item = QListWidgetItem(f"{c.name}" + (f"  ·  {c.domain}" if c.domain else ""))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, c)
            lw.addItem(item)
        v.addWidget(lw)

        def _set_all(state) -> None:
            for i in range(lw.count()):
                lw.item(i).setCheckState(state)

        row = QHBoxLayout()
        all_btn = QPushButton("Select all")
        all_btn.clicked.connect(lambda: _set_all(Qt.CheckState.Checked))
        none_btn = QPushButton("Deselect all")
        none_btn.clicked.connect(lambda: _set_all(Qt.CheckState.Unchecked))
        ok_btn = QPushButton("OK")
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(dlg.reject)
        row.addWidget(all_btn)
        row.addWidget(none_btn)
        row.addStretch(1)
        row.addWidget(cancel_btn)
        row.addWidget(ok_btn)
        v.addLayout(row)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None
        return [lw.item(i).data(Qt.ItemDataRole.UserRole) for i in range(lw.count())
                if lw.item(i).checkState() == Qt.CheckState.Checked]

    def _choose_import_destination(self) -> "tuple[int, str, bool] | None":
        """Ask whether to import into a NEW list or an EXISTING one. Returns
        (list_id, list_name, is_new), or None if the user cancels."""
        existing = get_lists(self._current_owner_id)                         # [(id, name)], ordered by name
        make_new = True
        if existing:
            box = QMessageBox(self)
            box.setWindowTitle("Import list")
            box.setText("Add the matched companies to a new list or an existing one?")
            new_btn = box.addButton("New list", QMessageBox.ButtonRole.AcceptRole)
            existing_btn = box.addButton("Existing list", QMessageBox.ButtonRole.AcceptRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.exec()
            clicked = box.clickedButton()
            if clicked is existing_btn:
                make_new = False
            elif clicked is not new_btn:
                return None                            # cancelled

        if make_new:
            name, ok = QInputDialog.getText(self, "Import list", "Name for the new list:")
            if not ok or not name.strip():
                return None
            return create_list(name.strip(), self._current_owner_id), name.strip(), True

        names = [nm for _, nm in existing]
        name, ok = QInputDialog.getItem(
            self, "Import list", "Add to which list?", names, 0, False)   # editable=False
        if not ok or not name:
            return None
        lid = next(i for i, nm in existing if nm == name)
        return lid, name, False

    def _read_tokens_from_file(self, path: str) -> list[str]:
        """Read candidate tokens from a file's company-name / company-domain column(s) only.

        Supports .xlsx (each sheet checked independently) and CSV/TXT. In each, the first
        non-empty row is treated as the header; only columns whose header names a company
        name or domain are scanned. Raises NoCompanyColumnError if no such column exists,
        or other errors (OSError / openpyxl) for the caller to surface."""
        if path.lower().endswith(".xlsx"):
            from openpyxl import load_workbook          # optional dep; imported lazily
            wb = load_workbook(path, read_only=True, data_only=True)   # values, not formulas
            tokens: list[str] = []
            found = False
            try:
                for ws in wb.worksheets:
                    got = self._tokens_from_rows(list(ws.iter_rows(values_only=True)))
                    if got is not None:                 # this sheet had a name/domain column
                        found = True
                        tokens += got
            finally:
                wb.close()
            if not found:
                raise NoCompanyColumnError()
            return tokens
        # CSV / plain text
        with open(path, newline="", encoding="utf-8-sig") as f:
            got = self._tokens_from_rows(list(csv.reader(f)))
        if got is None:
            raise NoCompanyColumnError()
        return got

    @staticmethod
    def _tokens_from_rows(rows: list) -> list[str] | None:
        """Find the header row (first non-empty) and the company-name / company-domain columns,
        then return the values from those columns (header excluded). Returns None if the file
        has no such column, so the caller can distinguish "no matching header" from "no data"."""
        header_idx = next(
            (i for i, row in enumerate(rows)
             if any(c is not None and str(c).strip() for c in row)), None)
        if header_idx is None:
            return None                                 # empty file → treated as "no column"
        headers = [(str(c).strip().lower() if c is not None else "") for c in rows[header_idx]]

        cols: set[int] = set()
        for j, h in enumerate(headers):
            if "name" in h or h == "company":                       # company-name column
                cols.add(j)
            if any(k in h for k in ("domain", "website", "url")):   # company-domain column
                cols.add(j)
        if not cols:
            return None

        tokens: list[str] = []
        for row in rows[header_idx + 1:]:
            for j in cols:
                if j < len(row) and row[j] is not None:
                    val = str(row[j]).strip()
                    if val:
                        tokens.append(val)
        return tokens

    def _show_unmatched_dialog(self, unmatched: list[str], header: str) -> None:
        """Show the entries that couldn't be identified (as domains where they look like one),
        in a read-only, selectable list with a one-click Copy-to-clipboard button."""
        entries = [self._as_domain_or_raw(t) for t in unmatched]
        text = "\n".join(entries)
        n = len(entries)

        dlg = QDialog(self)
        dlg.setWindowTitle("Import list — unidentified companies")
        v = QVBoxLayout(dlg)
        v.addWidget(QLabel(
            f"{header}\n\n{n} entr{'y' if n == 1 else 'ies'} could not be identified "
            f"(no matching company in your deals list):"))
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        v.addWidget(view)

        row = QHBoxLayout()
        copy_btn = QPushButton("Copy to clipboard")

        def _copy() -> None:
            QApplication.clipboard().setText(text)
            copy_btn.setText("Copied ✓")

        copy_btn.clicked.connect(_copy)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(dlg.accept)
        row.addWidget(copy_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        v.addLayout(row)
        dlg.resize(440, 380)
        dlg.exec()

    @staticmethod
    def _as_domain_or_raw(token: str) -> str:
        """Normalize a domain-like token (has a dot, no spaces) to a clean domain; otherwise
        return it unchanged (e.g. a plain company name)."""
        t = (token or "").strip()
        if "." in t and not any(ch.isspace() for ch in t):
            return AffinityView._norm_domain(t)
        return t

    @staticmethod
    def _norm_domain(s: str) -> str:
        """Normalize a domain/URL for matching: drop scheme, path, and a www. prefix."""
        s = s.strip().lower()
        s = re.sub(r"^https?://", "", s)
        s = s.split("/", 1)[0]
        return re.sub(r"^www\.", "", s)

    def _match_tokens(self, tokens: list[str]) -> tuple[list[Company], list[str]]:
        """Match name/domain tokens to loaded companies — the active set AND the owner's Passed
        deals, so passed-on companies are recognized too. Numeric-only cells (IDs, amounts,
        dates) are ignored. Returns (matched companies deduped, unmatched tokens deduped)."""
        pool = self._companies + self._passed         # active + Passed (disjoint status sets)
        by_domain = {self._norm_domain(c.domain): c for c in pool if c.domain}
        by_name = {c.name.strip().lower(): c for c in pool if c.name}
        matched: dict[int, Company] = {}
        unmatched: list[str] = []
        seen_unmatched: set[str] = set()
        for tok in tokens:
            t = (tok or "").strip()
            if not t or not any(ch.isalpha() for ch in t):   # skip blanks + numeric cells
                continue
            c = by_domain.get(self._norm_domain(t)) or by_name.get(t.lower())
            if c:
                matched[c.id] = c
            elif t.lower() not in seen_unmatched:
                seen_unmatched.add(t.lower())
                unmatched.append(t)
        return list(matched.values()), unmatched

    def _populate_add_to_list_menu(self) -> None:
        """Rebuild the 'Add to list' menu from current lists (just before it opens)."""
        self.add_to_list_menu.clear()
        for lid, name in get_lists(self._current_owner_id):
            act = self.add_to_list_menu.addAction(name)
            act.triggered.connect(
                lambda checked=False, lid=lid, name=name: self._add_current_to_list(lid, name))
        self.add_to_list_menu.addSeparator()
        self.add_to_list_menu.addAction("New list…").triggered.connect(self._new_list_and_add)

    def _add_current_to_list(self, list_id: int, list_name: str) -> None:
        if not self._current:
            return
        add_company(list_id, self._current.id, self._current.name)
        self.status.setText(f"Added {self._current.name} to “{list_name}”.")
        if self.list_selector.currentData() == list_id:   # that list is showing → refresh it
            self._build_reminders()

    def _new_list_and_add(self) -> None:
        name, ok = QInputDialog.getText(self, "New list", "List name:")
        if not ok or not name.strip():
            return
        lid = create_list(name.strip(), self._current_owner_id)
        self._refresh_list_selector()          # so the reminders dropdown shows it
        self._add_current_to_list(lid, name.strip())

    def _delete_current_list(self) -> None:
        list_id = self.list_selector.currentData()
        if not isinstance(list_id, int):       # live buckets aren't real lists — can't delete
            return
        name = self.list_selector.currentText()
        reply = QMessageBox.question(
            self, "Delete list", f"Delete the list “{name}”? This can't be undone.")
        if reply != QMessageBox.StandardButton.Yes:
            return
        delete_list(list_id)
        self._refresh_list_selector()          # drops it; selection falls back to the built-in
        self._build_reminders()

    def _list_selector_context_menu(self, pos) -> None:
        """Right-click the list dropdown to rename or reassign the selected custom list."""
        list_id = self.list_selector.currentData()
        if not isinstance(list_id, int):       # live buckets aren't real lists — can't edit
            return
        menu = QMenu(self)
        menu.addAction("Rename list…").triggered.connect(lambda: self._rename_list(list_id))
        menu.addAction("Assign to owner…").triggered.connect(lambda: self._reassign_list(list_id))
        menu.exec(self.list_selector.mapToGlobal(pos))

    def eventFilter(self, obj, event):
        """Double-click an item in the list dropdown's popup to rename that custom list."""
        if (obj is self.list_selector.view().viewport()
                and event.type() == QEvent.Type.MouseButtonDblClick):
            idx = self.list_selector.view().indexAt(event.position().toPoint())
            if idx.isValid():
                data = self.list_selector.itemData(idx.row())
                if isinstance(data, int):      # a real custom list (not a bucket/separator)
                    self.list_selector.hidePopup()
                    self._rename_list(data)
                    return True                # consume the double-click
        return super().eventFilter(obj, event)

    def _rename_list(self, list_id: int) -> None:
        """Prompt for and apply a new name for a custom list."""
        info = get_list(list_id)
        old = info[2] if info else ""
        new, ok = QInputDialog.getText(self, "Rename list", "New name:", text=old)
        if not ok:
            return
        new = new.strip()
        if not new or new == old:
            return
        if not rename_list(list_id, new):
            QMessageBox.warning(self, "Rename list",
                                f"Couldn't rename — this owner already has a list named “{new}”.")
            return
        self._refresh_list_selector()          # relabels; keeps this list selected
        idx = self.list_selector.findData(list_id)
        if idx >= 0:
            self.list_selector.setCurrentIndex(idx)
        self._build_reminders()

    def _owner_options(self) -> list[tuple[int, str]]:
        """(owner_id, name) pairs to assign lists to — the discovered owners in the owner
        dropdown, plus the connected user (from the 'My deals' entry). Deduped by id."""
        options: list[tuple[int, str]] = []
        seen: set[int] = set()
        for i in range(self.owner_selector.count()):
            oid = self.owner_selector.itemData(i)
            name = self.owner_selector.itemText(i)
            if oid is None:                    # "My deals (default)" → the connected user
                oid = self._connected_owner_id
                name = f"Me ({name})"
            if oid is not None and oid not in seen:
                seen.add(oid)
                options.append((oid, name))
        return options

    def _pick_owner(self, default_id: "int | None", prompt: str) -> "int | None":
        """Let the user pick an owner to assign a list to. Returns the owner id, or None if
        cancelled / no owners are available."""
        options = self._owner_options()
        if not options:
            QMessageBox.information(
                self, "Assign to owner",
                "No owners available yet — run “Find owners” to load them.")
            return None
        names = [nm for _, nm in options]
        default_row = next((i for i, (oid, _) in enumerate(options) if oid == default_id), 0)
        choice, ok = QInputDialog.getItem(self, "Assign to owner", prompt, names, default_row, False)
        if not ok:
            return None
        return next(oid for oid, nm in options if nm == choice)

    def _reassign_list(self, list_id: int) -> None:
        """Move a custom list to a different Affinity owner, prompting for a rename if that owner
        already has a list with the same name (per the requested behaviour)."""
        info = get_list(list_id)
        if info is None:
            return
        _id, cur_owner, name = info
        new_owner = self._pick_owner(cur_owner, f"Assign “{name}” to which owner?")
        if new_owner is None or new_owner == cur_owner:
            return

        new_name = None
        while not reassign_list(list_id, new_owner, new_name):
            # Name clash in the target owner — require a different name (or cancel).
            new_name, ok = QInputDialog.getText(
                self, "Assign to owner",
                f"That owner already has a list named “{new_name or name}”.\nNew name for it:",
                text=(new_name or name))
            if not ok or not new_name.strip():
                return                         # cancelled the reassignment
            new_name = new_name.strip()

        self._refresh_list_selector()          # the list leaves this owner's view
        self._build_reminders()
        QMessageBox.information(self, "Assign to owner",
                                f"“{new_name or name}” moved to the selected owner.")

    # --- pop the reminders pane out into its own window --------------------

    def _toggle_popout(self) -> None:
        if self._reminders_window is None:
            self._popout_reminders()
        else:
            self._dock_reminders()

    def _popout_reminders(self) -> None:
        win = _PopoutWindow(self._on_popout_closed, self)
        win.setWindowTitle("HighlandX — Reminders")
        lay = QVBoxLayout(win)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.addWidget(self.reminders_box)      # reparents the pane out of the splitter
        win.resize(360, 520)
        self._reminders_window = win
        self.popout_btn.setText("Dock")
        win.show()

    def _dock_reminders(self) -> None:
        """Return the pane to the splitter (from the Dock button)."""
        if self._reminders_window is None:
            return
        self._reminders_dock.insertWidget(0, self.reminders_box)   # back to its original spot
        self.popout_btn.setText("Pop out")
        win, self._reminders_window = self._reminders_window, None
        win.close()                            # now empty; closeEvent no-ops (window is None)

    def _on_popout_closed(self) -> None:
        """The pop-out window was closed via its ✕ — dock the pane back so it isn't lost."""
        if self._reminders_window is None:
            return                             # already docking via the button
        self._reminders_dock.insertWidget(0, self.reminders_box)
        self.popout_btn.setText("Pop out")
        self._reminders_window = None

    def _build_reminders(self) -> None:
        """Populate the reminders pane for the selected list: upcoming meetings, a live
        'last raised' bucket (str key), or a custom watchlist (int id)."""
        sel = self.list_selector.currentData()   # None | str key | int list_id
        mode = self.reminders_order.currentText()
        self.founding_controls.setVisible(sel == FOUNDING_KEY)   # filter fields only for that list

        # Each branch yields entries [(id, Company|None, name)] + a status line. Most rows
        # are annotated with the last-raised date; meetings show the meeting date and keep
        # their own soonest-first order (so the reminders sort dropdown doesn't apply).
        annotate = _last_raised_label
        presorted = False

        if sel == MEETINGS_KEY:                   # companies with an upcoming Outlook meeting
            soonest: dict[int, str] = {}
            companies: dict[int, Company] = {}
            for e in self._events:
                c, when = e["company"], e["date"] or ""
                if c.id not in soonest or when < soonest[c.id]:
                    soonest[c.id] = when
                companies.setdefault(c.id, c)
            ordered = sorted(companies.values(), key=lambda c: soonest[c.id])   # soonest first
            entries: list[tuple[int, Company | None, str]] = [(c.id, c, c.name) for c in ordered]
            annotate = lambda c: f"Meeting {_date(soonest[c.id])}"
            presorted = True
            status = (f"{len(entries)} with upcoming meetings" if entries
                      else "No upcoming meetings matched (refresh to load Outlook).")
        elif sel == FOUNDING_KEY:                 # founded before/after N years ago
            years = self.founding_years.value()
            after = self.founding_dir.currentText() == "After"
            threshold = date.today().year - years   # the year N years ago
            entries = [(c.id, c, c.name) for c in self._companies
                       if c.year_founded is not None
                       and (c.year_founded >= threshold if after else c.year_founded < threshold)]
            annotate = lambda c: f"Founded {c.year_founded}"
            status = (f"{len(entries)} founded {'after' if after else 'before'} "
                      f"{years} year(s) ago" if entries else "No companies match.")
        elif sel is None:
            entries = []
            status = "No list selected."
        elif isinstance(sel, str):                # live 'last raised' bucket
            lo, hi = _RAISED_RANGE[sel]
            entries = [(c.id, c, c.name) for c in self._companies
                       if _in_raised_bucket(c, lo, hi)]
            status = f"{len(entries)} companies" if entries else "No companies in this range."
        else:                                     # custom watchlist (int id)
            # Include Passed deals so passed companies in a list resolve to real Company objects
            # (with a list_entry_id) — this lets Update Status act on them too.
            by_id = {c.id: c for c in self._companies + self._passed}
            entries = []
            for cid, name in get_members(sel):
                c = by_id.get(cid)
                entries.append((cid, c, c.name if c else name))
            status = f"{len(entries)} in list" if entries else "Empty — use “Add to list”."

        if not presorted:
            entries = sort_companies(entries, mode, self._overrides, key=lambda e: e[1])
        self._reminders = [e[1] for e in entries]
        self._reminder_ids = [e[0] for e in entries]
        rows = []
        for _cid, c, name in entries:
            if not c:
                rows.append(f"{name}  (N/A)  (not in current view)")
                continue
            rows.append(f"{name}  ({_score_label(c, self._overrides)})  ·  {annotate(c)}")

        self.reminders_list.blockSignals(True)
        self.reminders_list.clear()
        for text, comp in zip(rows, self._reminders):
            item = QListWidgetItem(text)
            domain = comp.domain if comp else None
            item.setIcon(self._row_icon(domain))
            item.setData(Qt.ItemDataRole.UserRole, domain)   # delegate reads this for brand colors
            self.reminders_list.addItem(item)
        self.reminders_list.setCurrentRow(-1)
        self.reminders_list.blockSignals(False)
        self.reminders_status.setText(status)

    def set_gradient_enabled(self, on: bool) -> None:
        """Toggle the brand-color gradient backdrop and repaint everything affected."""
        set_gradient_enabled(on)
        self.company_list.viewport().update()
        self._refresh_panel_gradients()

    def _refresh_panel_gradients(self) -> None:
        """Repaint all three panels so their shared continuous gradient stays aligned
        (offsets/total change whenever one shows, hides, or is resized)."""
        for panel in (self.detail_panel, self.side, self.suggestions_panel):
            panel.refresh()

    def _available_logos(self) -> set[str]:
        """Filenames of non-empty logo files on disk, scanned once (one dir walk, not a
        stat() per company)."""
        if self._logo_files is None:
            try:
                self._logo_files = {e.name for e in os.scandir(LOGOS_DIR)
                                    if e.is_file() and e.stat().st_size > 0}
            except OSError:
                self._logo_files = set()
        return self._logo_files

    def _row_icon(self, domain: str | None):
        """The logo QIcon for a domain — from the session cache, else lazily from the warm
        disk cache. QIcon(path) is lazy: Qt only decodes the image when a row is actually
        painted (≈the handful of visible rows), instead of decoding all ~4k up front."""
        if not domain:
            return self._blank_icon
        if domain in self._logo_cache:
            return self._logo_cache[domain]
        path = _cache_path(domain)
        if path.name in self._available_logos():
            icon = QIcon(str(path))          # lazy — decoded on paint, not now
            self._logo_cache[domain] = icon
            return icon
        return self._blank_icon              # no disk file → _start_logo_fetch will network-fetch

    def select_company(self, c: Company) -> None:
        self._current = c
        self.detail_name.setText(f"<h2>{c.name} ({_score_label(c, self._overrides)})</h2>")
        self.detail_meta.setText(f"Status: {c.status or '—'}   ·   Domain: {c.domain or '—'}")
        for panel in (self.detail_panel, self.side, self.suggestions_panel):
            panel.set_domain(c.domain)         # faint brand-color backdrop for this company
        for b in (self.pitchbook_btn, self.raylu_btn, self.linkedin_btn, self.affinity_btn,
                  self.activity_btn, self.suggestions_btn, self.add_to_list_btn,
                  self.add_note_btn):
            b.setEnabled(True)
        self.website_btn.setEnabled(bool(c.domain))

        # if a panel is already open, refresh it for the new company instead of
        # making the user re-click; otherwise just reset the (hidden) activity view
        if self.side.isVisible():
            self.load_activity()
        else:
            self._clear_activity()
        if self.suggestions_panel.isVisible():
            self.show_suggestions()
        else:
            self._clear_suggestions()
        if self.web_panel.isVisible():
            self._reload_web()

    def _detail_name_context_menu(self, pos) -> None:
        if not self._current:
            return
        menu = QMenu(self)
        menu.addAction("Set fit score override…").triggered.connect(self._set_fit_override)
        clear_act = menu.addAction("Clear fit score override")
        clear_act.setEnabled(self._current.id in self._overrides)
        clear_act.triggered.connect(self._clear_fit_override)
        menu.exec(self.detail_name.mapToGlobal(pos))

    def _set_fit_override(self) -> None:
        c = self._current
        if not c:
            return
        current = self._overrides.get(c.id)
        if current is None:
            current = fit_score(c).score or 50
        score, ok = QInputDialog.getInt(
            self, "Set fit score override", f"Fit score for {c.name} (0-100):",
            current, 0, 100)
        if not ok:
            return
        set_override(c.id, score)
        self._overrides[c.id] = score
        self.detail_name.setText(f"<h2>{c.name} ({_score_label(c, self._overrides)})</h2>")
        self.apply_filter()

    def _clear_fit_override(self) -> None:
        c = self._current
        if not c or c.id not in self._overrides:
            return
        clear_override(c.id)
        del self._overrides[c.id]
        self.detail_name.setText(f"<h2>{c.name} ({_score_label(c, self._overrides)})</h2>")
        self.apply_filter()

    def _clear_activity(self) -> None:
        self.side.setVisible(False)
        self._refresh_panel_gradients()
        self._notes = []
        self._timeline = []
        self.summary_label.clear()
        self.timeline_list.clear()
        self.timeline_status.clear()
        self.notes_list.clear()
        self.notes_status.clear()
        self.reader.clear()

    def show_suggestions(self) -> None:
        """Open the Raylu Suggestions panel for the selected company."""
        c = self._current
        if not c:
            return
        self.suggestions_panel.setVisible(True)
        self._refresh_panel_gradients()
        self.suggest_status.setText(f"Raylu Suggestions — {c.name}")
        self.raylu_output.setPlainText(
            f"Press “Enrich from Raylu” to fetch this company's data and its "
            f"“{raylu_service.SCORING_DEFINITION}”.")
        self.raylu_tokens.clear()

    @asyncSlot()
    async def enrich_from_raylu(self) -> None:
        """Fetch get_company + the deal score from Raylu's MCP (via Haiku 4.5) and show
        the raw output. Runs off the Qt event loop so the UI stays responsive."""
        c = self._current
        if not c:
            return
        self.enrich_btn.setEnabled(False)
        self.suggest_status.setText(f"Raylu Suggestions — {c.name}  ·  enriching…")
        try:
            result = await raylu_service.enrich(c.name, c.domain)
        except Exception as e:                     # surface any failure in the panel itself
            self.raylu_output.setPlainText(f"Enrich failed:\n\n{e}")
            self.raylu_tokens.clear()
        else:
            self.raylu_output.setPlainText(result.text)
            u = result.usage
            self._raylu_session_tokens += u.total
            self.raylu_tokens.setText(
                f"{u.input:,} in · {u.output:,} out · {u.total:,} tok"
                f"   (session {self._raylu_session_tokens:,})")
        finally:
            self.enrich_btn.setEnabled(True)
            self.suggest_status.setText(f"Raylu Suggestions — {c.name}")

    def _clear_suggestions(self) -> None:
        self.suggestions_panel.setVisible(False)
        self._refresh_panel_gradients()
        self.suggest_status.clear()
        self.raylu_output.clear()
        self.raylu_tokens.clear()

    def _select_and_highlight(self, company: Company, prefer: str | None = None) -> None:
        """Clear the search, switch the dropdown to the company's category, select it.
        prefer picks a specific category to show it in (e.g. 'upcoming' from the Events pane)."""
        self.search_box.blockSignals(True)
        self.search_box.clear()
        self.search_box.blockSignals(False)
        self.apply_filter()                      # rebucket with no search term

        cat = prefer if prefer and any(c.id == company.id
                                       for c in self._categorized.get(prefer, [])) \
            else self._categorize(company)
        idx = self.category_selector.findData(cat)
        if idx >= 0:
            self.category_selector.blockSignals(True)
            self.category_selector.setCurrentIndex(idx)
            self.category_selector.blockSignals(False)
            self._display_companies()

        row = next((i for i, c in enumerate(self._visible_companies) if c.id == company.id), -1)
        if row >= 0:
            self.company_list.setCurrentRow(row)   # fires _company_selected → select_company
        else:
            self.select_company(company)           # not in current view (e.g. filtered out)

    # --- events (Outlook calendar) -----------------------------------------

    def _match_company(self, ev, domain_map: dict) -> Company | None:
        """Identify the company an event is about, or None if not confident."""
        for addr in ev.attendees:
            domain = addr.rsplit("@", 1)[-1] if "@" in addr else ""
            if domain and domain in domain_map:
                return domain_map[domain]

        title = ev.subject.lower()
        hits: list[Company] = []
        seen: set = set()
        for c in self._companies:
            name = (c.name or "").lower().strip()
            if len(name) < 4 or name not in title:
                continue
            if re.search(r"\b" + re.escape(name) + r"\b", title) and c.id not in seen:
                seen.add(c.id)
                hits.append(c)
                if len(hits) > 1:
                    return None
        return hits[0] if len(hits) == 1 else None

    async def _load_events(self) -> None:
        """Match upcoming calendar events to companies (attendee domain, then title)."""
        self.events_list.clear()
        self.events_status.setText("Loading events…")
        try:
            events = await get_calendar_events()
        except Exception as err:
            self.events_status.setText(_friendly(err))
            return

        domain_map = {c.domain.lower(): c for c in self._companies if c.domain}
        self._events = []
        for ev in events:
            company = self._match_company(ev, domain_map)
            if company is None:
                continue
            self._events.append({"company": company, "date": ev.start, "subject": ev.subject})

        self._events.sort(key=lambda r: r["date"] or "")
        for r in self._events:
            self.events_list.addItem(
                f"{_date(r['date'])}  ·  {r['company'].name} ({_score_label(r['company'], self._overrides)})"
                f"  —  {r['subject']}")
        self.events_status.setText(
            f"{len(self._events)} events" if self._events else "No events matched to companies"
        )
        # events just loaded → rebucket so the "Upcoming meetings" category reflects them
        self.apply_filter()

    def select_from_event(self, row: int) -> None:
        if 0 <= row < len(self._events):
            self._select_and_highlight(self._events[row]["company"], prefer="upcoming")

    def select_from_reminder(self, row: int) -> None:
        if 0 <= row < len(self._reminders):
            company = self._reminders[row]
            if company is not None:            # None = a list member not in the current load
                self._select_and_highlight(company)

    def _reminders_context_menu(self, pos) -> None:
        """Right-click a row in a custom list to remove that company from it."""
        list_id = self.list_selector.currentData()
        if not isinstance(list_id, int):       # live buckets are computed, not editable
            return
        item = self.reminders_list.itemAt(pos)
        if item is None:
            return
        row = self.reminders_list.row(item)
        if not (0 <= row < len(self._reminder_ids)):
            return
        company_id = self._reminder_ids[row]
        menu = QMenu(self)
        menu.addAction("Remove from list").triggered.connect(
            lambda: self._remove_from_current_list(list_id, company_id))
        menu.exec(self.reminders_list.mapToGlobal(pos))

    def _remove_from_current_list(self, list_id: int, company_id: int) -> None:
        remove_company(list_id, company_id)
        self._build_reminders()

    def _notes_context_menu(self, pos) -> None:
        """Right-click a locally-stored note to delete it (Affinity notes aren't touched)."""
        item = self.notes_list.itemAt(pos)
        if item is None:
            return
        row = self.notes_list.row(item)
        if not (0 <= row < len(self._notes)):
            return
        note = self._notes[row]
        if not note.local:                     # only local notes are deletable from here
            return
        menu = QMenu(self)
        menu.addAction("Delete local note").triggered.connect(
            lambda: self._delete_local_note(note.id))
        menu.exec(self.notes_list.mapToGlobal(pos))

    def _delete_local_note(self, note_id: int) -> None:
        delete_local_note(note_id)
        self.load_activity()                    # refresh the notes pane

    # --- web panel (PitchBook / Raylu / website / Affinity) ----------------

    def open_pitchbook(self) -> None:
        """Reveal the web panel and search PitchBook for the current company (B; fallback A)."""
        if not self._current:
            return
        self._web_source = "pitchbook"
        self.web_panel.setVisible(True)
        self.web_view.setUrl(QUrl(PITCHBOOK_SEARCH_URL.format(q=quote_plus(self._current.name))))

    def open_raylu(self) -> None:
        """Reveal the web panel and open Raylu (no name-based search → user navigates)."""
        self._web_source = "raylu"
        self.web_panel.setVisible(True)
        self.web_view.setUrl(QUrl(RAYLU_HOME_URL))

    def open_website(self) -> None:
        if self._current and self._current.domain:
            self._web_source = "website"
            self.web_panel.setVisible(True)
            self.web_view.setUrl(QUrl(f"https://{self._current.domain}"))

    def open_linkedin(self) -> None:
        """Open the company's LinkedIn page: the enriched URL from Affinity when present,
        else a LinkedIn company search by name."""
        if not self._current:
            return
        self._web_source = "linkedin"
        self.web_panel.setVisible(True)
        url = (self._current.linkedin_url
               or LINKEDIN_SEARCH_URL.format(q=quote_plus(self._current.name)))
        self.web_view.setUrl(QUrl(url))

    def open_affinity(self) -> None:
        if self._current:
            self._web_source = "affinity"
            self.web_panel.setVisible(True)
            self.web_view.setUrl(QUrl(company_url(self._current.id)))

    def _reload_web(self) -> None:
        """Re-point the open web panel at the current company (for company switches)."""
        if self._web_source == "pitchbook":
            self.open_pitchbook()
        elif self._web_source == "website":
            self.open_website()
        elif self._web_source == "linkedin":
            self.open_linkedin()
        elif self._web_source == "affinity":
            self.open_affinity()
        # raylu is not company-specific (URLs aren't name-based) → leave as-is

    def hide_web_panel(self) -> None:
        self.web_panel.setVisible(False)

    # --- activity (timeline + notes) ---------------------------------------

    @asyncSlot()
    async def load_activity(self) -> None:
        c = self._current
        if not c:
            return
        self.side.setVisible(True)
        self._refresh_panel_gradients()
        self.activity_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        # --- relationship summary (firm-wide interaction dates) ---
        self.summary_label.setText("Loading…")
        try:
            summary = await get_company_summary(c.id)
            self.summary_label.setText(
                f"Last contact: {_date(summary.last_contact)}<br>"
                f"Last email: {_date(summary.last_email)}<br>"
                f"Next meeting: {_date(summary.next_event)}<br>"
                f"Last meeting: {_date(summary.last_event)}<br>"
                f"Last raised: {_blank_date(c.last_funding_date)}<br>"
                f"Amount last raised: {_money(c.last_funding_amount)}<br>"
                f"Total amount raised: {_money(c.total_funding_amount)}"
            )
        except Exception as err:
            self.summary_label.setText(_friendly(err))

        # --- timeline: rich interactions (emails + meetings) from Affinity ---
        self.timeline_list.clear()
        self._timeline = []
        seen: set = set()
        for label, it in [("Next meeting", c.next_event), ("Last meeting", c.last_event),
                          ("Last email", c.last_email), ("First email", c.first_email)]:
            if not it:
                continue
            # a single email is returned as BOTH first_email and last_email (same for a
            # lone event) — collapse those so the same interaction isn't listed twice
            key = (it.kind, it.date, it.subject, it.from_address)
            if key in seen:
                continue
            seen.add(key)
            self._timeline.append((label, it))
        self._timeline.sort(key=lambda t: t[1].date or "", reverse=True)   # newest first

        # pinned to the top, always shown (even with no data) per the reach-out heuristic
        self._reach_out = reach_out_suggestion(c)
        suggestion = Interaction(kind="suggestion", date=self._reach_out.date or "",
                                 subject="Reach out suggestion", who="")
        self._timeline.insert(0, ("Reach out suggestion", suggestion))

        for label, it in self._timeline:
            if it.kind == "suggestion":
                self.timeline_list.addItem(f"Reach out suggestion: {_blank_date(it.date)}")
                continue
            icon = "✉" if it.kind == "email" else "📅"
            self.timeline_list.addItem(f"{_date(it.date)}  ·  {icon} {it.subject}  —  {it.who}")
        real_count = len(self._timeline) - 1   # excludes the pinned suggestion row
        self.timeline_status.setText(
            f"{real_count} interactions — click to view" if real_count else "No interactions"
        )

        # --- notes: Affinity (shared) + local (this machine only), merged newest-first ---
        self.notes_list.clear()
        self.reader.clear()
        self.notes_status.setText("Loading notes…")
        local = get_local_notes(c.id)            # cheap, no network — always available
        notes_key = f"{CACHE_AFFINITY_NOTES_PREFIX}{c.id}"
        affinity, err, from_cache = [], None, False
        try:
            affinity = await get_company_notes(c.id)
            write_cache(notes_key, notes_to_json(affinity))   # cache the fresh copy for later
        except Exception as e:
            err = e
            # Rate-limited / offline: fall back to the last cached Affinity notes so they still
            # show (local notes always show regardless of the API being down).
            cached, _ = read_cache(notes_key)
            if cached:
                try:
                    affinity = notes_from_json(cached)
                    from_cache = bool(affinity)
                except Exception:
                    affinity = []
        self._notes = sorted(affinity + local, key=lambda n: n.created_at or "", reverse=True)
        for n in self._notes:
            label = "Local note" if n.local else ("Meeting note" if n.is_meeting else "Note")
            self.notes_list.addItem(f"{_date(n.created_at)}  ·  {label}  —  {_plain(n.content)[:70]}")
        if self._notes:
            if err and from_cache:
                suffix = "  ·  (Affinity unavailable — showing cached notes)"
            elif err:
                suffix = "  ·  (Affinity unavailable)"
            else:
                suffix = ""
            self.notes_status.setText(f"{len(self._notes)} notes — click one to read it" + suffix)
        elif err:
            self.notes_status.setText(_friendly(err))
        else:
            self.notes_status.setText("No notes")
        QApplication.restoreOverrideCursor()
        self.activity_btn.setEnabled(True)

    @asyncSlot()
    async def add_note(self) -> None:
        """Write a note for the current company — shared to Affinity or stored locally."""
        c = self._current
        if not c:
            return
        text, ok = QInputDialog.getMultiLineText(self, "Add note", f"Note for {c.name}:", "")
        if not ok or not text.strip():
            return

        # choose where the note goes
        box = QMessageBox(self)
        box.setWindowTitle("Save note")
        box.setText(f"Where should this note for {c.name} go?")
        box.setInformativeText("“Share to Affinity” is visible to your whole team.\n"
                               "“Store locally” stays on this machine only.")
        affinity_choice = box.addButton("Share to Affinity", QMessageBox.ButtonRole.AcceptRole)
        local_choice = box.addButton("Store locally", QMessageBox.ButtonRole.AcceptRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (affinity_choice, local_choice):
            return                                # cancelled

        self.add_note_btn.setEnabled(False)
        try:
            if clicked is affinity_choice:
                self.status.setText(f"Sharing note to Affinity for {c.name}…")
                await create_note(c.id, text.strip())
                self.status.setText(f"Note shared to Affinity for {c.name}.")
            else:
                add_local_note(c.id, text.strip())   # local SQLite; no network
                self.status.setText(f"Note stored locally for {c.name}.")
        except Exception as err:
            self.status.setText(_friendly(err))
            QMessageBox.warning(self, "Add note", f"Couldn't save the note:\n{_friendly(err)}")
        else:
            self.load_activity()                  # reveal + refresh the activity pane so it shows
        finally:
            self.add_note_btn.setEnabled(True)

    @asyncSlot()
    async def show_timeline_entry(self, row: int) -> None:
        """Render a timeline interaction; for emails, try to pull the body from Outlook."""
        if not (0 <= row < len(self._timeline)):
            return
        label, it = self._timeline[row]
        if it.kind == "suggestion":
            self.reader.setMarkdown(_reach_out_markdown(self._reach_out))
            return
        if it.kind == "meeting":
            self.reader.setMarkdown(
                f"### {it.subject}\n\n**{label}** · {_date(it.date)}\n\nAttendees: {it.who or '—'}"
            )
            return

        meta = f"### {it.subject}\n\nFrom **{it.who}** · {_date(it.date)}\n\n"
        self.reader.setMarkdown(meta + "_Looking up the message in your mailbox…_")
        try:
            msg = await get_message_by_interaction(it.from_address, it.date, it.subject)
        except Exception as err:
            self.reader.setMarkdown(meta + f"_{_friendly(err)}_")
            return
        if msg:
            header = (f"<h3 style='margin:0'>{msg.subject}</h3>"
                      f"<p style='color:#666;margin:4px 0'><b>{msg.sender_name}</b> "
                      f"&lt;{msg.sender_address}&gt;<br>{msg.received}</p><hr>")
            if msg.body_is_html:
                self.reader.setHtml(header + msg.body_content)
            else:
                self.reader.setHtml(header + f"<pre style='white-space:pre-wrap'>{msg.body_content}</pre>")
        else:
            self.reader.setMarkdown(
                meta + "_Couldn't find this message in your mailbox._"
            )

    def show_note(self, row: int) -> None:           # sync — renders selected note
        if 0 <= row < len(self._notes):
            self.reader.setMarkdown(self._notes[row].content or "")

    def _on_order_changed(self) -> None:
        set_setting("pref.reminders_order", self.reminders_order.currentText())
        self._build_reminders()

    def _on_company_order_changed(self) -> None:
        set_setting("pref.company_order", self.company_order.currentText())
        self._display_companies()

    def reload_prefs(self) -> None:
        """Re-read the reminders order from settings (called after the Settings dialog closes)."""
        self.reminders_order.setCurrentText(get_setting("pref.reminders_order", "Newest Added"))

    def _save_layout(self) -> None:
        """Persist each splitter's proportions so the layout survives across launches."""
        for key, sp in self._splitters.items():
            state = sp.saveState().toBase64().data().decode("ascii")
            set_setting(f"layout.{key}", state)

    def _restore_layout(self) -> None:
        """Restore saved splitter proportions, if any were stored on a previous run.
        QSplitter.restoreState() also restores ORIENTATION — so a state saved when the
        layout was arranged differently would silently flip the splitter's direction and
        override what the code builds. Re-assert the code-defined orientation afterward so
        only the sizes are honored."""
        for key, sp in self._splitters.items():
            text = get_setting(f"layout.{key}")
            if text:
                orientation = sp.orientation()
                sp.restoreState(QByteArray.fromBase64(text.encode("ascii")))
                sp.setOrientation(orientation)