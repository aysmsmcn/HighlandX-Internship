import re

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QPushButton, QListWidget,
                               QLabel, QSplitter, QTextBrowser, QLineEdit)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from qasync import asyncSlot

from services.affinity_service import (my_owner_id, list_my_companies, Company,
                                       company_url, get_company_notes, Note,
                                       get_company_summary)
from services.outlook_service import get_calendar_events


def _plain(text: str) -> str:
    """Strip HTML tags so content shows as a readable one-line row."""
    return re.sub(r"<[^>]+>", "", text or "").strip()


def _date(d: str | None) -> str:
    """Show just the date part of an ISO timestamp, or an em-dash if missing."""
    return d[:10] if d else "—"


def _titled(title: str, *widgets: QWidget) -> QWidget:
    """Wrap a bold title + widgets into one box (for use as a splitter pane)."""
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.addWidget(QLabel(f"<b>{title}</b>"))
    for w in widgets:
        v.addWidget(w)
    return box


class AffinityView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._companies: list[Company] = []      # full loaded set
        self._current: Company | None = None
        self._notes: list[Note] = []
        self._reminders: list[dict] = []          # {company, date, subject}
        self._ongoing: list[Company] = []         # emailed + met
        self._followup: list[Company] = []        # emailed, not met
        self._missed: list[Company] = []          # untouched

        self.refresh_btn = QPushButton("Load my companies")
        self.status = QLabel("Click to load your Affinity companies.")

        # --- events pane (top area) ---
        self.reminders_status = QLabel("")
        self.reminders_list = QListWidget()

        # --- left column: search box + 3 category lists ---
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search companies…")
        self.search_box.setClearButtonEnabled(True)
        self.ongoing_list = QListWidget()
        self.followup_list = QListWidget()
        self.missed_list = QListWidget()
        cat_split = QSplitter(Qt.Orientation.Horizontal)
        cat_split.addWidget(_titled("Ongoing", self.ongoing_list))
        cat_split.addWidget(_titled("Follow up", self.followup_list))
        cat_split.addWidget(_titled("Missed", self.missed_list))
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.search_box)
        ll.addWidget(cat_split)

        # --- detail pane ---
        self.detail_name = QLabel("Select a company")
        self.detail_meta = QLabel("")
        self.website_btn = QPushButton("Open website")
        self.affinity_btn = QPushButton("Open in Affinity")
        self.activity_btn = QPushButton("Load Activity")
        for b in (self.website_btn, self.affinity_btn, self.activity_btn):
            b.setEnabled(False)
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.addWidget(self.detail_name)
        dl.addWidget(self.detail_meta)
        dl.addWidget(self.website_btn)
        dl.addWidget(self.affinity_btn)
        dl.addWidget(self.activity_btn)
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

        side_split = QSplitter(Qt.Orientation.Vertical)
        side_split.addWidget(_titled("Relationship (Affinity)", self.summary_label))
        side_split.addWidget(_titled("Timeline", self.timeline_status, self.timeline_list))
        side_split.addWidget(_titled("Notes", self.notes_status, self.notes_list))
        side_split.addWidget(_titled("Note details", self.reader))
        side_split.setStretchFactor(3, 2)

        self.side = QWidget()
        sv = QVBoxLayout(self.side)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.addWidget(side_split)
        self.side.setVisible(False)

        # --- columns: categories | detail | side ---
        columns = QSplitter(Qt.Orientation.Horizontal)
        columns.addWidget(left)
        columns.addWidget(detail)
        columns.addWidget(self.side)
        columns.setStretchFactor(0, 2)
        columns.setStretchFactor(2, 2)

        # --- events on top, columns below ---
        main_split = QSplitter(Qt.Orientation.Vertical)
        main_split.addWidget(_titled("Events (upcoming)",
                                     self.reminders_status, self.reminders_list))
        main_split.addWidget(columns)
        main_split.setStretchFactor(1, 3)

        layout = QVBoxLayout(self)
        layout.addWidget(self.refresh_btn)
        layout.addWidget(self.status)
        layout.addWidget(main_split)

        self.refresh_btn.clicked.connect(self.load)
        self.search_box.textChanged.connect(self.apply_filter)
        self.reminders_list.currentRowChanged.connect(self.select_from_reminder)
        self.ongoing_list.currentRowChanged.connect(lambda r: self._cat_selected("ongoing", r))
        self.followup_list.currentRowChanged.connect(lambda r: self._cat_selected("followup", r))
        self.missed_list.currentRowChanged.connect(lambda r: self._cat_selected("missed", r))
        self.website_btn.clicked.connect(self.open_website)
        self.affinity_btn.clicked.connect(self.open_affinity)
        self.activity_btn.clicked.connect(self.load_activity)
        self.notes_list.currentRowChanged.connect(self.show_note)

    @asyncSlot()
    async def load(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading…")
        try:
            pid = await my_owner_id()
            self._companies = await list_my_companies(pid)
            self.apply_filter()                 # partitions into the 3 columns + sets status
        except Exception as err:
            self.status.setText(f"Failed: {err}")
            self.refresh_btn.setEnabled(True)
            return

        await self._load_reminders()            # Outlook calendar — non-fatal
        self.refresh_btn.setEnabled(True)

    def apply_filter(self) -> None:
        """Filter by search term, then partition into Ongoing / Follow up / Missed."""
        term = self.search_box.text().strip().lower()
        visible = [c for c in self._companies if term in c.name.lower()]
        self._ongoing, self._followup, self._missed = [], [], []
        for c in visible:
            if c.met:                           # met (and usually emailed) → ongoing
                self._ongoing.append(c)
            elif c.emailed:                     # emailed but not met → follow up
                self._followup.append(c)
            else:                               # untouched → missed
                self._missed.append(c)
        self._fill(self.ongoing_list, self._ongoing)
        self._fill(self.followup_list, self._followup)
        self._fill(self.missed_list, self._missed)
        self.status.setText(
            f"{len(self._companies)} companies — {len(self._ongoing)} ongoing, "
            f"{len(self._followup)} follow up, {len(self._missed)} missed"
        )

    @staticmethod
    def _fill(widget: QListWidget, companies: list[Company]) -> None:
        widget.blockSignals(True)
        widget.clear()
        for c in companies:
            widget.addItem(f"[{c.status}]  {c.name}")
        widget.setCurrentRow(-1)
        widget.blockSignals(False)

    def _cat_selected(self, cat: str, row: int) -> None:
        lst, companies = {
            "ongoing": (self.ongoing_list, self._ongoing),
            "followup": (self.followup_list, self._followup),
            "missed": (self.missed_list, self._missed),
        }[cat]
        if not (0 <= row < len(companies)):
            return
        for other in (self.ongoing_list, self.followup_list, self.missed_list):
            if other is not lst:
                other.blockSignals(True)
                other.setCurrentRow(-1)
                other.blockSignals(False)
        self.select_company(companies[row])

    def select_company(self, c: Company) -> None:
        self._current = c
        self.detail_name.setText(f"<h2>{c.name}</h2>")
        self.detail_meta.setText(f"Status: {c.status or '—'}   ·   Domain: {c.domain or '—'}")
        self.website_btn.setEnabled(bool(c.domain))
        self.affinity_btn.setEnabled(True)
        self.activity_btn.setEnabled(True)

        # reset + hide the side view until they ask for this company
        self.side.setVisible(False)
        self._notes = []
        self.summary_label.clear()
        self.timeline_list.clear()
        self.timeline_status.clear()
        self.notes_list.clear()
        self.notes_status.clear()
        self.reader.clear()

    # --- events (Outlook calendar) -----------------------------------------

    async def _load_reminders(self) -> None:
        """Match upcoming calendar events to companies by name in title."""
        self.reminders_list.clear()
        self.reminders_status.setText("Loading events…")
        try:
            events = await get_calendar_events()
        except Exception as err:
            self.reminders_status.setText(f"Calendar load failed: {err}")
            return

        self._reminders = []
        for ev in events:
            subj = ev.subject.lower()
            for c in self._companies:
                if c.name and c.name.lower() in subj:
                    self._reminders.append({"company": c, "date": ev.start, "subject": ev.subject})
                    break

        self._reminders.sort(key=lambda r: r["date"] or "")     # soonest first
        for r in self._reminders:
            self.reminders_list.addItem(f"{_date(r['date'])}  ·  {r['company'].name}  —  {r['subject']}")
        self.reminders_status.setText(
            f"{len(self._reminders)} events" if self._reminders else "No events matched to companies"
        )

    def select_from_reminder(self, row: int) -> None:
        """Clicking an event selects that company as if searched."""
        if not (0 <= row < len(self._reminders)):
            return
        company = self._reminders[row]["company"]
        self.search_box.clear()                 # clears filter → repartitions all companies
        self.select_company(company)
        for lst, arr in [(self.ongoing_list, self._ongoing),
                         (self.followup_list, self._followup),
                         (self.missed_list, self._missed)]:
            lst.blockSignals(True)
            lst.setCurrentRow(arr.index(company) if company in arr else -1)
            lst.blockSignals(False)

    # --- activity (timeline + notes) ---------------------------------------

    @asyncSlot()
    async def load_activity(self) -> None:
        c = self._current
        if not c:
            return
        self.side.setVisible(True)
        self.activity_btn.setEnabled(False)

        # --- relationship summary (firm-wide interaction dates) ---
        self.summary_label.setText("Loading…")
        summary = None
        try:
            summary = await get_company_summary(c.id)
            self.summary_label.setText(
                f"Last contact: {_date(summary.last_contact)}<br>"
                f"Last email: {_date(summary.last_email)}<br>"
                f"Next meeting: {_date(summary.next_event)}<br>"
                f"Last meeting: {_date(summary.last_event)}"
            )
        except Exception as err:
            self.summary_label.setText(f"Summary failed: {err}")

        # --- timeline: event anchors only, newest first ---
        self.timeline_list.clear()
        anchors = []
        if summary:
            anchors = [
                (summary.next_event, "Next meeting"),
                (summary.last_event, "Last meeting"),
                (summary.first_event, "First meeting"),
                (summary.last_email, "Last email"),
                (summary.first_email, "First email"),
            ]
            anchors = [(d, lbl) for d, lbl in anchors if d]
            anchors.sort(key=lambda a: a[0], reverse=True)
        for date, label in anchors:
            self.timeline_list.addItem(f"{_date(date)}  ·  {label}")
        self.timeline_status.setText(f"{len(anchors)} events" if anchors else "No timeline data")

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
            self.notes_status.setText(f"Notes load failed: {err}")
        finally:
            self.activity_btn.setEnabled(True)

    def show_note(self, row: int) -> None:           # sync — renders selected note
        if 0 <= row < len(self._notes):
            self.reader.setMarkdown(self._notes[row].content or "")

    def open_website(self) -> None:
        if self._current and self._current.domain:
            QDesktopServices.openUrl(QUrl(f"https://{self._current.domain}"))

    def open_affinity(self) -> None:
        if self._current:
            QDesktopServices.openUrl(QUrl(company_url(self._current.id)))
