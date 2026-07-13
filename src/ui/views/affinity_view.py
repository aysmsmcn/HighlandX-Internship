import asyncio
import json
import os
import re
from datetime import datetime, timezone, date
from urllib.parse import quote_plus

from config import LOGOS_DIR

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
                               QListWidgetItem, QLabel, QSplitter, QTextBrowser, QLineEdit,
                               QComboBox, QApplication, QInputDialog, QMenu, QMessageBox)
from PySide6.QtCore import Qt, QUrl, QByteArray, QSize
from PySide6.QtGui import QShortcut, QKeySequence, QIcon, QPixmap, QColor, QPalette
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from qasync import asyncSlot

from services.affinity_service import (my_owner_id, list_my_companies, Company,
                                       company_url, get_company_notes, Note,
                                       get_company_summary, Interaction, company_has_notes,
                                       companies_to_json, companies_from_json, reach_out_suggestion,
                                       fit_score, proceed_recommendation,
                                       DEFAULT_GOOD_FIT_THRESHOLD, DEFAULT_PASS_THRESHOLD)
from services.fit_service import get_all_overrides, set_override, clear_override
from services.logo_service import get_logo_bytes, _cache_path
from ui.company_row_delegate import CompanyRowDelegate, GradientPanel, set_gradient_enabled
from services.outlook_service import get_calendar_events, get_message_by_interaction
from services.settings_service import get_setting, set_setting
from services.cache_service import read_cache, write_cache
from services.lists_service import (get_lists, create_list, get_members, add_company,
                                    remove_company, delete_list)

# Cache keys for the local SQLite store.
CACHE_COMPANIES = "affinity.companies"
CACHE_NOTED_IDS = "affinity.noted_ids"

# Best-guess PitchBook search URL. If it doesn't land on a search, do a search in
# the panel, copy the address-bar URL, and replace this template ({q} = query).
PITCHBOOK_SEARCH_URL = "https://my.pitchbook.com/search-results/s/all?query={q}"
PITCHBOOK_HOME_URL = "https://my.pitchbook.com"
# Raylu company URLs are internally generated (not name-based) → no pre-search;
# just open the app and let the user navigate/search inside the panel.
RAYLU_HOME_URL = "https://app.raylu.ai/"



def _plain(text: str) -> str:
    """Strip HTML tags so content shows as a readable one-line row."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _date(d: str | None) -> str:
    """Show just the date part of an ISO timestamp, or an em-dash if missing."""
    return d[:10] if d else "—"


def _blank_date(d: str | None) -> str:
    """Show just the date part of an ISO timestamp, or '---' if missing."""
    return d[:10] if d else "---"


REMINDERS_ORDER_OPTIONS = ["Newest First", "Oldest First",
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

LOGO_ICON_SIZE = QSize(28, 28)

# Company-list categories. The five EXCLUSIVE categories partition every company into
# exactly one bucket (met > emailed > untouched). "Upcoming meetings" is an OVERLAPPING
# view: any company with a matched upcoming calendar event, regardless of its bucket — so a
# company can show under both "Ongoing" and "Upcoming meetings". "all" is the combined view.
CATEGORY_DEFS = [
    ("all", "All"),
    ("ongoing", "Ongoing"),                    # a meeting is logged
    ("upcoming", "Upcoming meetings"),         # OVERLAPS: matched upcoming calendar event
    ("followup", "Follow up"),                 # emailed and they replied
    ("noresponse", "Contacted, no response"),  # only we have emailed them
    ("noted", "Noted, not contacted"),         # untouched but has a note
    ("missed", "Missed"),                      # untouched, no note
]
_EXCLUSIVE_KEYS = ("ongoing", "followup", "noresponse", "noted", "missed")

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


def _in_raised_bucket(company: Company | None, lo: int, hi: int | None) -> bool:
    """True if months-since-last-raised falls in [lo, hi) (hi None = open-ended)."""
    m = _months_since_raised(company)
    if m is None:
        return False
    return m >= lo and (hi is None or m < hi)


def sort_companies(items, mode: str, overrides: dict[int, int], key=lambda c: c):
    """Order items by a REMINDERS_ORDER_OPTIONS mode (shared by the reminders pane
    and the main company list so both stay consistent). `key` maps each item to its
    Company — identity for a bare company list; the reminders pane passes tuples and
    supplies a key that pulls out the (possibly None) company."""
    by_score = mode in ("Highest Fit Score", "Lowest Fit Score")
    by_reachout = mode in ("Most urgent first", "Least urgent first")
    by_raised = mode in ("Longest Since Raised", "Most Recently Raised")
    most_urgent = mode == "Most urgent first"
    longest_first = mode == "Longest Since Raised"
    reverse = mode in ("Newest First", "Highest Fit Score")
    items = list(items)
    if by_score:
        items.sort(key=lambda x: _score_sort_value(key(x), overrides), reverse=reverse)
    elif by_reachout:
        items.sort(key=lambda x: _reachout_sort_key(key(x), most_urgent))
    elif by_raised:
        items.sort(key=lambda x: _last_raised_sort_key(key(x), longest_first))
    else:
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


def _friendly(err: Exception) -> str:
    """Turn a raw exception into a short, plain-language message."""
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
        self._companies: list[Company] = []      # full loaded set
        self._current: Company | None = None
        self._web_source: str | None = None      # last web panel source (for auto-refresh)
        self._notes: list[Note] = []
        self._timeline: list[tuple[str, Interaction]] = []   # (label, interaction)
        self._events: list[dict] = []             # calendar events {company, date, subject}
        self._reminders: list[Company | None] = []   # rows of the reminders/list pane (None = not loaded)
        self._reminder_ids: list[int] = []            # company id per reminders row (for list removal)
        self._reminders_window: QWidget | None = None  # the popped-out reminders window, if any
        self._noted_ids: set[int] = set()         # org ids that have at least one note
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
        self.status = QLabel("Loading cached companies…")

        # --- events widgets (shown in the "View Upcoming Events" popup, EventsDialog —
        # not part of this view's own layout; MainWindow reparents them into the dialog) ---
        self.events_status = QLabel("")
        self.events_list = QListWidget()

        # --- reminders pane (top) ---
        self.reminders_order = QComboBox()
        self.reminders_order.addItems(REMINDERS_ORDER_OPTIONS)
        self.reminders_order.setCurrentText(get_setting("pref.reminders_order", "Newest first"))
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
        self.add_list_btn = QPushButton("+")
        self.add_list_btn.setFixedWidth(28)
        self.add_list_btn.setToolTip("Create a new list")
        self.del_list_btn = QPushButton("🗑")
        self.del_list_btn.setFixedWidth(28)
        self.del_list_btn.setToolTip("Delete the selected list")
        self.popout_btn = QPushButton("Pop out")
        self.popout_btn.setToolTip("Open the reminders pane in its own window")
        self._refresh_list_selector()
        reminders_header = QWidget()
        rh = QHBoxLayout(reminders_header)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(self.list_selector, 1)
        rh.addWidget(self.add_list_btn)
        rh.addWidget(self.del_list_btn)
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
        self.company_order.setCurrentText(get_setting("pref.company_order", "Newest First"))

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
        self.affinity_btn = QPushButton("Open in Affinity")
        self.activity_btn = QPushButton("Load Activity")
        self.suggestions_btn = QPushButton("Suggestions")
        self.add_to_list_btn = QPushButton("Add to list")
        self.add_to_list_menu = QMenu(self)
        self.add_to_list_btn.setMenu(self.add_to_list_menu)   # dropdown of lists
        self.add_to_list_menu.aboutToShow.connect(self._populate_add_to_list_menu)
        for b in (self.pitchbook_btn, self.raylu_btn, self.website_btn,
                  self.affinity_btn, self.activity_btn, self.suggestions_btn,
                  self.add_to_list_btn):
            b.setEnabled(False)
            b.setStyleSheet(_DETAIL_BTN_QSS)   # transparent so the panel gradient shows through
        self.detail_panel = GradientPanel()   # faint brand-color backdrop
        dl = QVBoxLayout(self.detail_panel)
        dl.addWidget(self.detail_name)
        dl.addWidget(self.detail_meta)
        dl.addWidget(self.pitchbook_btn)
        dl.addWidget(self.raylu_btn)
        dl.addWidget(self.website_btn)
        dl.addWidget(self.affinity_btn)
        dl.addWidget(self.activity_btn)
        dl.addWidget(self.suggestions_btn)
        dl.addWidget(self.add_to_list_btn)
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

        # --- suggestions panel: how to proceed (incl. reach-out timing) + email template ---
        self.suggest_status = QLabel("")
        self.suggest_proceed = QTextBrowser()
        self.suggest_email = QTextBrowser()
        for tb in (self.suggest_proceed, self.suggest_email):
            tb.setStyleSheet("background: transparent;")   # let panel gradient show through

        suggest_split = QSplitter(Qt.Orientation.Vertical)
        suggest_split.addWidget(_titled("How to proceed", self.suggest_proceed))
        suggest_split.addWidget(_titled("Suggested email", self.suggest_email))

        self.suggestions_panel = GradientPanel()   # faint brand-color backdrop
        sgv = QVBoxLayout(self.suggestions_panel)
        sgv.setContentsMargins(0, 0, 0, 0)
        sgv.addWidget(self.suggest_status)
        sgv.addWidget(suggest_split)
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
                                     reminders_header, self.reminders_order,
                                     self.reminders_status, self.reminders_list)
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
        cv.addWidget(self.refresh_btn)
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
        self.search_box.textChanged.connect(self.apply_filter)
        self.reminders_order.currentIndexChanged.connect(lambda _i: self._on_order_changed())
        self.list_selector.currentIndexChanged.connect(lambda _i: self._build_reminders())
        self.list_selector.currentIndexChanged.connect(
            lambda _i: self.del_list_btn.setEnabled(isinstance(self.list_selector.currentData(), int)))
        self.add_list_btn.clicked.connect(self._create_list)
        self.del_list_btn.clicked.connect(self._delete_current_list)
        self.popout_btn.clicked.connect(self._toggle_popout)
        self.events_list.currentRowChanged.connect(self.select_from_event)
        self.reminders_list.currentRowChanged.connect(self.select_from_reminder)
        self.reminders_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.reminders_list.customContextMenuRequested.connect(self._reminders_context_menu)
        self.detail_name.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.detail_name.customContextMenuRequested.connect(self._detail_name_context_menu)
        self.company_list.currentRowChanged.connect(self._company_selected)
        self.category_selector.currentIndexChanged.connect(lambda _i: self._display_companies())
        self.company_order.currentIndexChanged.connect(lambda _i: self._on_company_order_changed())
        self.pitchbook_btn.clicked.connect(self.open_pitchbook)
        self.raylu_btn.clicked.connect(self.open_raylu)
        self.website_btn.clicked.connect(self.open_website)
        self.affinity_btn.clicked.connect(self.open_affinity)
        self.activity_btn.clicked.connect(self.load_activity)
        self.suggestions_btn.clicked.connect(self.show_suggestions)
        # itemClicked (not currentRowChanged) so re-clicking an already-selected row still
        # re-renders it — currentRowChanged only fires when the row index actually changes,
        # which breaks re-clicking the same timeline row after selecting something in notes
        # (each list tracks its own currentRow independently).
        self.timeline_list.itemClicked.connect(lambda item: self.show_timeline_entry(self.timeline_list.row(item)))
        self.notes_list.itemClicked.connect(lambda item: self.show_note(self.notes_list.row(item)))

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
        self.status.setText("Refreshing from Affinity…")
        try:
            pid = await my_owner_id()
            self._companies = await list_my_companies(pid)
        except Exception as err:
            self.status.setText(_friendly(err))
            QApplication.restoreOverrideCursor()
            self.refresh_btn.setEnabled(True)
            return

        # Companies are in hand: persist + render them NOW, without waiting on the slow
        # notes-index and Outlook events. Those finish in the background and update their
        # own panes when ready, so the company list is usable in seconds.
        updated = write_cache(CACHE_COMPANIES, companies_to_json(self._companies))
        self.apply_filter()                     # partitions + builds reminders + sets status
        self.status.setText(
            self.status.text() + f"  ·  updated {self._ago(updated)} — loading notes & events…")
        QApplication.restoreOverrideCursor()
        asyncio.ensure_future(self._finish_load_background())

    async def _finish_load_background(self) -> None:
        """The slow, non-critical parts of a refresh — run after companies are on screen."""
        try:
            await self._index_noted_background()   # notes-index → reminders pane
            await self._load_events()              # Outlook calendar (also triggers MS login)
        finally:
            self.refresh_btn.setEnabled(True)
            self.status.setText(self.status.text().replace(" — loading notes & events…", ""))

    async def _index_noted_background(self) -> None:
        """Rebuild the noted-companies index off the critical path, then re-bucket."""
        self.status.setText(self.status.text() + "  ·  checking notes…")
        try:
            await self._index_noted_missed()
            write_cache(CACHE_NOTED_IDS, json.dumps(sorted(self._noted_ids)))
        except Exception:
            return
        self.apply_filter()                      # re-bucket so "Noted, not contacted" reflects notes

    def load_from_cache(self) -> None:
        """Populate the UI from the local SQLite cache — instant, no network, no MS login."""
        text, updated = read_cache(CACHE_COMPANIES)
        if not text:
            self.status.setText("No cached data yet — click “Refresh from Affinity”.")
            return
        try:
            self._companies = companies_from_json(text)
            noted_text, _ = read_cache(CACHE_NOTED_IDS)
            self._noted_ids = set(json.loads(noted_text)) if noted_text else set()
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
        if c.id in self._noted_ids:
            return "noted"
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
        for key, label, _lo, _hi in RAISED_BUCKETS:
            self.list_selector.addItem(label, key)
        lists = get_lists()
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
        lid = create_list(name.strip())
        self._refresh_list_selector()
        idx = self.list_selector.findData(lid)
        if idx >= 0:
            self.list_selector.setCurrentIndex(idx)   # fires _build_reminders via the signal

    def _populate_add_to_list_menu(self) -> None:
        """Rebuild the 'Add to list' menu from current lists (just before it opens)."""
        self.add_to_list_menu.clear()
        for lid, name in get_lists():
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
        lid = create_list(name.strip())
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
        """Populate the reminders pane for the selected list: a live 'last raised' bucket
        (str key) or a custom watchlist (int id)."""
        sel = self.list_selector.currentData()   # None | str bucket key | int list_id
        mode = self.reminders_order.currentText()

        # entries: (company_id, Company|None, display name) per row
        if sel is None:
            entries: list[tuple[int, Company | None, str]] = []
            status = "No list selected."
        elif isinstance(sel, str):                # live 'last raised' bucket
            lo, hi = _RAISED_RANGE[sel]
            entries = [(c.id, c, c.name) for c in self._companies
                       if _in_raised_bucket(c, lo, hi)]
            status = f"{len(entries)} companies" if entries else "No companies in this range."
        else:                                     # custom watchlist (int id)
            by_id = {c.id: c for c in self._companies}
            entries = []
            for cid, name in get_members(sel):
                c = by_id.get(cid)
                entries.append((cid, c, c.name if c else name))
            status = f"{len(entries)} in list" if entries else "Empty — use “Add to list”."

        entries = sort_companies(entries, mode, self._overrides, key=lambda e: e[1])
        self._reminders = [e[1] for e in entries]
        self._reminder_ids = [e[0] for e in entries]
        rows = []
        for _cid, c, name in entries:
            if not c:
                rows.append(f"{name}  (N/A)  (not in current view)")
                continue
            rows.append(f"{name}  ({_score_label(c, self._overrides)})  ·  {_last_raised_label(c)}")

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

    async def _index_noted_missed(self) -> None:
        """Check untouched (Missed) companies for notes → _noted_ids.

        Incremental: companies already known to have notes are kept without re-checking;
        only untouched companies whose note-status we don't yet know are queried. New notes
        on not-yet-noted companies are still picked up on every refresh (they get re-checked);
        the only thing skipped is re-confirming companies already flagged as noted."""
        untouched_ids = {c.id for c in self._companies if not c.emailed and not c.met}
        known = self._noted_ids & untouched_ids          # already noted + still untouched → keep
        to_check = [c for c in self._companies
                    if c.id in untouched_ids and c.id not in known]
        sem = asyncio.Semaphore(10)            # bound concurrency to be kind to the API

        async def check(c: Company) -> int | None:
            async with sem:
                try:
                    return c.id if await company_has_notes(c.id) else None
                except Exception:
                    return None

        results = await asyncio.gather(*(check(c) for c in to_check))
        self._noted_ids = known | {cid for cid in results if cid is not None}

    def select_company(self, c: Company) -> None:
        self._current = c
        self.detail_name.setText(f"<h2>{c.name} ({_score_label(c, self._overrides)})</h2>")
        self.detail_meta.setText(f"Status: {c.status or '—'}   ·   Domain: {c.domain or '—'}")
        for panel in (self.detail_panel, self.side, self.suggestions_panel):
            panel.set_domain(c.domain)         # faint brand-color backdrop for this company
        for b in (self.pitchbook_btn, self.raylu_btn, self.affinity_btn,
                  self.activity_btn, self.suggestions_btn, self.add_to_list_btn):
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
        """Open the suggestions panel: how to proceed, when to reach out, email template.
        TODO: "email template" is a placeholder — logic to come later."""
        c = self._current
        if not c:
            return
        self.suggestions_panel.setVisible(True)
        self._refresh_panel_gradients()
        self.suggest_status.setText(f"Suggestions for {c.name}")

        good = int(get_setting("pref.good_fit_threshold", str(DEFAULT_GOOD_FIT_THRESHOLD)))
        pass_ = int(get_setting("pref.pass_threshold", str(DEFAULT_PASS_THRESHOLD)))
        rec = proceed_recommendation(c, good, pass_, override_score=self._overrides.get(c.id))
        today = datetime.now(timezone.utc).date().isoformat()
        self.suggest_proceed.setMarkdown(_proceed_markdown(rec, reach_out_suggestion(c), today))

        self.suggest_email.setMarkdown(
            "_Coming soon — a suggested email draft will appear here._"
        )

    def _clear_suggestions(self) -> None:
        self.suggestions_panel.setVisible(False)
        self._refresh_panel_gradients()
        self.suggest_status.clear()
        self.suggest_proceed.clear()
        self.suggest_email.clear()

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

        # --- notes (separate from the timeline) ---
        self.notes_list.clear()
        self.reader.clear()
        self.notes_status.setText("Loading notes…")
        try:
            self._notes = await get_company_notes(c.id)
            if self._notes:
                for n in self._notes:
                    label = "Meeting note" if n.is_meeting else "Note"
                    self.notes_list.addItem(f"{_date(n.created_at)}  ·  {label}  —  {_plain(n.content)[:70]}")
                self.notes_status.setText(f"{len(self._notes)} notes — click one to read it")
            else:
                self.notes_status.setText("No notes")
        except Exception as err:
            self.notes_status.setText(_friendly(err))
        finally:
            QApplication.restoreOverrideCursor()
            self.activity_btn.setEnabled(True)

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
        self.reminders_order.setCurrentText(get_setting("pref.reminders_order", "Newest First"))

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