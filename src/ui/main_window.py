from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QMainWindow

from config import APP_NAME
from ui.views.affinity_view import AffinityView
from ui.dialogs.settings_dialog import SettingsDialog
from ui.dialogs.events_dialog import EventsDialog


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 720)

        self.view = AffinityView()        # store a reference (was inline before)
        self.setCentralWidget(self.view)
        self._events_dialog: EventsDialog | None = None   # lazily created, reused

        self._build_menu()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        settings_action = file_menu.addAction("Settings…")
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self.open_settings)

        events_action = self.menuBar().addAction("View Upcoming Events")
        events_action.triggered.connect(self.open_events)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self)
        dlg.exec()
        self.view.reload_prefs()

    def open_events(self) -> None:
        # non-modal (unlike Settings) so clicking an event can select the company live
        # in the main window without needing to close the popup first
        if self._events_dialog is None:
            self._events_dialog = EventsDialog(self.view, self)
        self._events_dialog.show()
        self._events_dialog.raise_()
        self._events_dialog.activateWindow()