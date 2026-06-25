import asyncio
import re
from urllib.parse import quote_plus

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QPushButton, QListWidget,
                               QLabel, QSplitter, QTextBrowser, QLineEdit, QComboBox)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from qasync import asyncSlot

from services.affinity_service import (my_owner_id, list_my_companies, Company,
                                       company_url, get_company_notes, Note,
                                       get_company_summary, Interaction, company_has_notes)
from services.outlook_service import get_calendar_events, get_message_by_subject

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
        self._web_source: str | None = None      # last web panel source (for auto-refresh)
        self._notes: list[Note] = []
        self._timeline: list[tuple[str, Interaction]] = []   # (label, interaction)
        self._events: list[dict] = []             # calendar events {company, date, subject}
        self._reminders: list[Company] = []       # noted, not-contacted (Missed + has notes)
        self._noted_ids: set[int] = set()         # org ids that have at least one note
        self._ongoing: list[Company] = []         # emailed + met
        self._followup: list[Company] = []        # emailed, not met
        self._missed: list[Company] = []          # untouched

        self.refresh_btn = QPushButton("Load my companies")
        self.status = QLabel("Click to load your Affinity companies.")

        # --- events pane (top-left) ---
        self.events_status = QLabel("")
        self.events_list = QListWidget()

        # --- reminders pane (top-right): recently added, not contacted ---
        self.reminders_order = QComboBox()
        self.reminders_order.addItems(["Newest first", "Oldest first"])
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
        self.pitchbook_btn = QPushButton("Open in PitchBook")
        self.raylu_btn = QPushButton("Open in Raylu")
        self.website_btn = QPushButton("Open website")
        self.affinity_btn = QPushButton("Open in Affinity")
        self.activity_btn = QPushButton("Load Activity")
        for b in (self.pitchbook_btn, self.raylu_btn, self.website_btn,
                  self.affinity_btn, self.activity_btn):
            b.setEnabled(False)
        detail = QWidget()
        dl = QVBoxLayout(detail)
        dl.addWidget(self.detail_name)
        dl.addWidget(self.detail_meta)
        dl.addWidget(self.pitchbook_btn)
        dl.addWidget(self.raylu_btn)
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

        # --- top row: events (left) + reminders (right) ---
        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(_titled("Events (upcoming)", self.events_status, self.events_list))
        top.addWidget(_titled("Reminders — noted, not contacted",
                              self.reminders_order, self.reminders_status, self.reminders_list))

        main_split = QSplitter(Qt.Orientation.Vertical)
        main_split.addWidget(top)
        main_split.addWidget(columns)
        main_split.setStretchFactor(1, 3)

        # everything built so far is the "content" (compresses left when the web panel opens)
        content = QWidget()
        cv = QVBoxLayout(content)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(self.refresh_btn)
        cv.addWidget(self.status)
        cv.addWidget(main_split)

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
        self.events_list.currentRowChanged.connect(self.select_from_event)
        self.reminders_list.currentRowChanged.connect(self.select_from_reminder)
        self.reminders_order.currentIndexChanged.connect(lambda _i: self._build_reminders())
        self.ongoing_list.currentRowChanged.connect(lambda r: self._cat_selected("ongoing", r))
        self.followup_list.currentRowChanged.connect(lambda r: self._cat_selected("followup", r))
        self.missed_list.currentRowChanged.connect(lambda r: self._cat_selected("missed", r))
        self.pitchbook_btn.clicked.connect(self.open_pitchbook)
        self.raylu_btn.clicked.connect(self.open_raylu)
        self.website_btn.clicked.connect(self.open_website)
        self.affinity_btn.clicked.connect(self.open_affinity)
        self.activity_btn.clicked.connect(self.load_activity)
        self.timeline_list.currentRowChanged.connect(self.show_timeline_entry)
        self.notes_list.currentRowChanged.connect(self.show_note)

    @asyncSlot()
    async def load(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading…")
        try:
            pid = await my_owner_id()
            self._companies = await list_my_companies(pid)
            self.status.setText("Checking notes on untouched companies…")
            await self._index_noted_missed()
            self.apply_filter()                 # partitions + builds reminders + sets status
        except Exception as err:
            self.status.setText(f"Failed: {err}")
            self.refresh_btn.setEnabled(True)
            return

        await self._load_events()               # Outlook calendar — non-fatal
        self.refresh_btn.setEnabled(True)

    def apply_filter(self) -> None:
        """Filter by search term, partition into the 3 columns, rebuild reminders."""
        term = self.search_box.text().strip().lower()
        visible = [c for c in self._companies if term in c.name.lower()]
        self._ongoing, self._followup, self._missed = [], [], []
        for c in visible:
            if c.met:
                self._ongoing.append(c)
            elif c.emailed:
                self._followup.append(c)
            else:
                self._missed.append(c)
        self._fill(self.ongoing_list, self._ongoing)
        self._fill(self.followup_list, self._followup)
        self._fill(self.missed_list, self._missed)
        self._build_reminders()
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

    def _build_reminders(self) -> None:
        """Noted-but-not-contacted companies (Missed set that have notes), sorted by date added."""
        newest_first = self.reminders_order.currentIndex() == 0
        noted = [c for c in self._missed if c.id in self._noted_ids]
        self._reminders = sorted(noted, key=lambda c: c.added or "", reverse=newest_first)
        self.reminders_list.blockSignals(True)
        self.reminders_list.clear()
        for c in self._reminders:
            self.reminders_list.addItem(f"{_date(c.added)}  ·  {c.name}")
        self.reminders_list.setCurrentRow(-1)
        self.reminders_list.blockSignals(False)
        self.reminders_status.setText(
            f"{len(self._reminders)} noted, not contacted" if self._reminders else "None"
        )

    async def _index_noted_missed(self) -> None:
        """Check only the untouched (Missed) companies for notes → _noted_ids."""
        untouched = [c for c in self._companies if not c.emailed and not c.met]
        sem = asyncio.Semaphore(10)            # bound concurrency to be kind to the API

        async def check(c: Company) -> int | None:
            async with sem:
                try:
                    return c.id if await company_has_notes(c.id) else None
                except Exception:
                    return None

        results = await asyncio.gather(*(check(c) for c in untouched))
        self._noted_ids = {cid for cid in results if cid is not None}

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
        for b in (self.pitchbook_btn, self.raylu_btn, self.affinity_btn, self.activity_btn):
            b.setEnabled(True)
        self.website_btn.setEnabled(bool(c.domain))

        # if a panel is already open, refresh it for the new company instead of
        # making the user re-click; otherwise just reset the (hidden) activity view
        if self.side.isVisible():
            self.load_activity()
        else:
            self._clear_activity()
        if self.web_panel.isVisible():
            self._reload_web()

    def _clear_activity(self) -> None:
        self.side.setVisible(False)
        self._notes = []
        self._timeline = []
        self.summary_label.clear()
        self.timeline_list.clear()
        self.timeline_status.clear()
        self.notes_list.clear()
        self.notes_status.clear()
        self.reader.clear()

    def _select_and_highlight(self, company: Company) -> None:
        """Select a company (as if searched) and highlight it in its category column."""
        self.search_box.clear()                 # clears filter → repartitions all companies
        self.select_company(company)
        for lst, arr in [(self.ongoing_list, self._ongoing),
                         (self.followup_list, self._followup),
                         (self.missed_list, self._missed)]:
            lst.blockSignals(True)
            lst.setCurrentRow(arr.index(company) if company in arr else -1)
            lst.blockSignals(False)

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
            self.events_status.setText(f"Calendar load failed: {err}")
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
            self.events_list.addItem(f"{_date(r['date'])}  ·  {r['company'].name}  —  {r['subject']}")
        self.events_status.setText(
            f"{len(self._events)} events" if self._events else "No events matched to companies"
        )

    def select_from_event(self, row: int) -> None:
        if 0 <= row < len(self._events):
            self._select_and_highlight(self._events[row]["company"])

    def select_from_reminder(self, row: int) -> None:
        if 0 <= row < len(self._reminders):
            self._select_and_highlight(self._reminders[row])

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
        self.activity_btn.setEnabled(False)

        # --- relationship summary (firm-wide interaction dates) ---
        self.summary_label.setText("Loading…")
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

        # --- timeline: rich interactions (emails + meetings) from Affinity ---
        self.timeline_list.clear()
        self._timeline = []
        for label, it in [("Next meeting", c.next_event), ("Last meeting", c.last_event),
                          ("Last email", c.last_email), ("First email", c.first_email)]:
            if it:
                self._timeline.append((label, it))
        self._timeline.sort(key=lambda t: t[1].date or "", reverse=True)   # newest first
        for label, it in self._timeline:
            icon = "✉" if it.kind == "email" else "📅"
            self.timeline_list.addItem(f"{_date(it.date)}  ·  {icon} {it.subject}  —  {it.who}")
        self.timeline_status.setText(
            f"{len(self._timeline)} interactions — click to view"
            if self._timeline else "No interactions"
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
            self.notes_status.setText(f"Notes load failed: {err}")
        finally:
            self.activity_btn.setEnabled(True)

    @asyncSlot()
    async def show_timeline_entry(self, row: int) -> None:
        """Render a timeline interaction; for emails, try to pull the body from Outlook."""
        if not (0 <= row < len(self._timeline)):
            return
        label, it = self._timeline[row]
        if it.kind == "meeting":
            self.reader.setMarkdown(
                f"### {it.subject}\n\n**{label}** · {_date(it.date)}\n\nAttendees: {it.who or '—'}"
            )
            return

        meta = f"### {it.subject}\n\nFrom **{it.who}** · {_date(it.date)}\n\n"
        self.reader.setMarkdown(meta + "_Looking up the message in your mailbox…_")
        try:
            msg = await get_message_by_subject(it.subject)
        except Exception as err:
            self.reader.setMarkdown(meta + f"_Couldn't fetch body: {err}_")
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
                meta + "_Not in your mailbox — body unavailable (you weren't a participant)._"
            )

    def show_note(self, row: int) -> None:           # sync — renders selected note
        if 0 <= row < len(self._notes):
            self.reader.setMarkdown(self._notes[row].content or "")
