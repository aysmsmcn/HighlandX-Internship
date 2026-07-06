import asyncio
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote_plus

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QListWidget,
                               QLabel, QSplitter, QTextBrowser, QLineEdit, QComboBox,
                               QApplication, QInputDialog, QMenu, QMessageBox)
from PySide6.QtCore import Qt, QUrl, QByteArray
from PySide6.QtGui import QShortcut, QKeySequence
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage
from qasync import asyncSlot

from services.affinity_service import (my_owner_id, list_my_companies, Company,
                                       company_url, get_company_notes, Note,
                                       get_company_summary, Interaction, company_has_notes,
                                       companies_to_json, companies_from_json)
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


def _titled_w(title_label: QLabel, *widgets: QWidget) -> QWidget:
    """Like _titled, but uses a caller-owned title label so its text can change later."""
    box = QWidget()
    v = QVBoxLayout(box)
    v.setContentsMargins(0, 0, 0, 0)
    v.addWidget(title_label)
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
        self._reminders: list[Company | None] = []   # rows of the reminders/list pane (None = not loaded)
        self._reminder_ids: list[int] = []            # company id per reminders row (for list removal)
        self._noted_ids: set[int] = set()         # org ids that have at least one note
        self._ongoing: list[Company] = []         # emailed + met
        self._followup: list[Company] = []        # emailed, not met
        self._missed: list[Company] = []          # untouched

        self.refresh_btn = QPushButton("Refresh from Affinity")
        self.refresh_btn.setToolTip("Re-fetch everything from Affinity (slow — minutes). "
                                    "The app shows cached data instantly on launch.")
        self.status = QLabel("Loading cached companies…")

        # --- events pane (top-left) ---
        self.events_status = QLabel("")
        self.events_list = QListWidget()

        # --- reminders pane (top-right) ---
        self.reminders_order = QComboBox()
        self.reminders_order.addItems(["Newest first", "Oldest first"])
        self.reminders_order.setCurrentText(get_setting("pref.reminders_order", "Newest first"))
        self.reminders_status = QLabel("")
        self.reminders_list = QListWidget()

        # list selector: built-in "Noted, not contacted" + custom watchlists, with a
        # "+" to create a new list
        self.list_selector = QComboBox()
        self.add_list_btn = QPushButton("+")
        self.add_list_btn.setFixedWidth(28)
        self.add_list_btn.setToolTip("Create a new list")
        self.del_list_btn = QPushButton("🗑")
        self.del_list_btn.setFixedWidth(28)
        self.del_list_btn.setToolTip("Delete the selected list")
        self._refresh_list_selector()
        reminders_header = QWidget()
        rh = QHBoxLayout(reminders_header)
        rh.setContentsMargins(0, 0, 0, 0)
        rh.addWidget(self.list_selector, 1)
        rh.addWidget(self.add_list_btn)
        rh.addWidget(self.del_list_btn)

        # --- left column: search box + 3 category lists ---
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search companies…")
        self.search_box.setClearButtonEnabled(True)
        self.ongoing_list = QListWidget()
        self.followup_list = QListWidget()
        self.missed_list = QListWidget()

        self.ongoing_title = QLabel("<b>Ongoing</b>")
        self.ongoing_title.setToolTip("You've met with these (a meeting is logged).")
        self.followup_title = QLabel("<b>Follow up</b>")
        self.followup_title.setToolTip("Emailed, but no meeting logged yet.")
        self.missed_title = QLabel("<b>Missed</b>")
        self.missed_title.setToolTip("No email or meeting logged yet.")

        cat_split = QSplitter(Qt.Orientation.Horizontal)
        cat_split.addWidget(_titled_w(self.ongoing_title, self.ongoing_list))
        cat_split.addWidget(_titled_w(self.followup_title, self.followup_list))
        cat_split.addWidget(_titled_w(self.missed_title, self.missed_list))
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
        self.add_to_list_btn = QPushButton("Add to list")
        self.add_to_list_menu = QMenu(self)
        self.add_to_list_btn.setMenu(self.add_to_list_menu)   # dropdown of lists
        self.add_to_list_menu.aboutToShow.connect(self._populate_add_to_list_menu)
        for b in (self.pitchbook_btn, self.raylu_btn, self.website_btn,
                  self.affinity_btn, self.activity_btn, self.add_to_list_btn):
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

        # --- bottom row: company list (left) | selected company [detail | side] (right) ---
        selected = QSplitter(Qt.Orientation.Horizontal)
        selected.addWidget(detail)
        selected.addWidget(self.side)
        selected.setStretchFactor(1, 2)
        bottom = QSplitter(Qt.Orientation.Horizontal)
        bottom.addWidget(left)
        bottom.addWidget(selected)

        # --- top row: events (left) | reminders (right) ---
        top = QSplitter(Qt.Orientation.Horizontal)
        top.addWidget(_titled("Events (upcoming)", self.events_status, self.events_list))
        top.addWidget(_titled("Reminders",
                              reminders_header, self.reminders_order,
                              self.reminders_status, self.reminders_list))

        # outer vertical splitter → one continuous horizontal divider (top / bottom)
        main_split = QSplitter(Qt.Orientation.Vertical)
        main_split.addWidget(top)
        main_split.addWidget(bottom)
        main_split.setStretchFactor(1, 3)

        # align the two vertical dividers (top's Events|Reminders with bottom's list|company)
        top.setSizes([400, 600])
        bottom.setSizes([400, 600])
        top.splitterMoved.connect(lambda *_: bottom.setSizes(top.sizes()))
        bottom.splitterMoved.connect(lambda *_: top.setSizes(bottom.sizes()))

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
        self.reminders_order.currentIndexChanged.connect(lambda _i: self._on_order_changed())
        self.list_selector.currentIndexChanged.connect(lambda _i: self._build_reminders())
        self.list_selector.currentIndexChanged.connect(
            lambda _i: self.del_list_btn.setEnabled(self.list_selector.currentData() is not None))
        self.add_list_btn.clicked.connect(self._create_list)
        self.del_list_btn.clicked.connect(self._delete_current_list)
        self.events_list.currentRowChanged.connect(self.select_from_event)
        self.reminders_list.currentRowChanged.connect(self.select_from_reminder)
        self.reminders_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.reminders_list.customContextMenuRequested.connect(self._reminders_context_menu)
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
            "top": top, "bottom": bottom, "cat": cat_split,
            "selected": selected, "side": side_split, "main": main_split,
        }
        self._restore_layout()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._save_layout)

    @asyncSlot()
    async def load(self) -> None:
        self.refresh_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.status.setText("Refreshing from Affinity… (this can take a few minutes)")
        try:
            pid = await my_owner_id()
            self._companies = await list_my_companies(pid)
            self.status.setText("Checking notes on untouched companies…")
            await self._index_noted_missed()
            # Persist to the local cache so the next launch loads instantly.
            write_cache(CACHE_COMPANIES, companies_to_json(self._companies))
            updated = write_cache(CACHE_NOTED_IDS, json.dumps(sorted(self._noted_ids)))
            self.apply_filter()                 # partitions + builds reminders + sets status
            self.status.setText(self.status.text() + f"  ·  updated {self._ago(updated)}")
            await self._load_events()           # Outlook calendar — non-fatal
        except Exception as err:
            self.status.setText(_friendly(err))
        finally:
            QApplication.restoreOverrideCursor()
            self.refresh_btn.setEnabled(True)

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
        self._update_cat_titles()
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

    def _update_cat_titles(self) -> None:
        """Show live counts in the three category column titles."""
        self.ongoing_title.setText(f"<b>Ongoing ({len(self._ongoing)})</b>")
        self.followup_title.setText(f"<b>Follow up ({len(self._followup)})</b>")
        self.missed_title.setText(f"<b>Missed ({len(self._missed)})</b>")

    def _refresh_list_selector(self) -> None:
        """Populate the list dropdown: built-in 'Noted, not contacted' (data=None) + custom lists."""
        current = self.list_selector.currentData()   # remember selection (list_id or None)
        self.list_selector.blockSignals(True)
        self.list_selector.clear()
        self.list_selector.addItem("Noted, not contacted", None)   # built-in
        for lid, name in get_lists():
            self.list_selector.addItem(name, lid)
        idx = self.list_selector.findData(current)
        self.list_selector.setCurrentIndex(idx if idx >= 0 else 0)
        self.list_selector.blockSignals(False)
        self.del_list_btn.setEnabled(self.list_selector.currentData() is not None)

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
        if list_id is None:                    # built-in list can't be deleted
            return
        name = self.list_selector.currentText()
        reply = QMessageBox.question(
            self, "Delete list", f"Delete the list “{name}”? This can't be undone.")
        if reply != QMessageBox.StandardButton.Yes:
            return
        delete_list(list_id)
        self._refresh_list_selector()          # drops it; selection falls back to the built-in
        self._build_reminders()

    def _build_reminders(self) -> None:
        """Populate the reminders pane for whichever list is selected in the dropdown."""
        list_id = self.list_selector.currentData()   # None = built-in "Noted, not contacted"
        newest_first = self.reminders_order.currentIndex() == 0

        if list_id is None:
            companies = [c for c in self._missed if c.id in self._noted_ids]
            companies.sort(key=lambda c: c.added or "", reverse=newest_first)
            self._reminders = list(companies)
            self._reminder_ids = [c.id for c in companies]
            rows = [f"{_date(c.added)}  ·  {c.name}" for c in companies]
            status = f"{len(companies)} noted, not contacted" if companies else "None"
        else:
            by_id = {c.id: c for c in self._companies}
            # (company_id, Company|None, display name, sort key) per member
            entries = []
            for cid, name in get_members(list_id):
                c = by_id.get(cid)
                entries.append((cid, c, c.name if c else name, (c.added or "") if c else ""))
            entries.sort(key=lambda e: e[3], reverse=newest_first)
            self._reminders = [e[1] for e in entries]
            self._reminder_ids = [e[0] for e in entries]
            rows = [f"{(_date(c.added) if c else '—')}  ·  {name}"
                    f"{'' if c else '  (not in current view)'}"
                    for _cid, c, name, _key in entries]
            status = f"{len(entries)} in list" if entries else "Empty — use “Add to list”."

        self.reminders_list.blockSignals(True)
        self.reminders_list.clear()
        for r in rows:
            self.reminders_list.addItem(r)
        self.reminders_list.setCurrentRow(-1)
        self.reminders_list.blockSignals(False)
        self.reminders_status.setText(status)

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
        for b in (self.pitchbook_btn, self.raylu_btn, self.affinity_btn,
                  self.activity_btn, self.add_to_list_btn):
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
            self.events_list.addItem(f"{_date(r['date'])}  ·  {r['company'].name}  —  {r['subject']}")
        self.events_status.setText(
            f"{len(self._events)} events" if self._events else "No events matched to companies"
        )

    def select_from_event(self, row: int) -> None:
        if 0 <= row < len(self._events):
            self._select_and_highlight(self._events[row]["company"])

    def select_from_reminder(self, row: int) -> None:
        if 0 <= row < len(self._reminders):
            company = self._reminders[row]
            if company is not None:            # None = a list member not in the current load
                self._select_and_highlight(company)

    def _reminders_context_menu(self, pos) -> None:
        """Right-click a row in a custom list to remove that company from it."""
        list_id = self.list_selector.currentData()
        if list_id is None:                    # built-in list is computed, not editable
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
                f"Last meeting: {_date(summary.last_event)}"
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

    def reload_prefs(self) -> None:
        """Re-read the reminders order from settings (called after the Settings dialog closes)."""
        self.reminders_order.setCurrentText(get_setting("pref.reminders_order", "Newest first"))

    def _save_layout(self) -> None:
        """Persist each splitter's proportions so the layout survives across launches."""
        for key, sp in self._splitters.items():
            state = sp.saveState().toBase64().data().decode("ascii")
            set_setting(f"layout.{key}", state)

    def _restore_layout(self) -> None:
        """Restore saved splitter proportions, if any were stored on a previous run."""
        for key, sp in self._splitters.items():
            text = get_setting(f"layout.{key}")
            if text:
                sp.restoreState(QByteArray.fromBase64(text.encode("ascii")))