from PySide6.QtGui import QKeySequence, QActionGroup
from PySide6.QtWidgets import QMainWindow, QApplication

from config import APP_NAME
from ui.views.affinity_view import AffinityView
from ui.dialogs.settings_dialog import SettingsDialog
from ui.dialogs.events_dialog import EventsDialog
from ui import theme
from services.settings_service import get_setting, set_setting


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 720)

        # Apply the saved theme (default dark) before building the rest of the UI.
        self._theme = get_setting("pref.theme", theme.DARK)
        theme.apply_theme(QApplication.instance(), self._theme)

        self.view = AffinityView()        # store a reference (was inline before)
        self.setCentralWidget(self.view)
        self._events_dialog: EventsDialog | None = None   # lazily created, reused

        self._build_menu()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        settings_action = file_menu.addAction("Settings…")
        settings_action.setShortcut("Ctrl+,")
        settings_action.triggered.connect(self.open_settings)

        view_menu = self.menuBar().addMenu("&View")
        self._theme_group = QActionGroup(self)   # exclusive → radio-style checkmarks
        self._theme_group.setExclusive(True)
        for label, mode in (("Dark mode", theme.DARK), ("Light mode", theme.LIGHT)):
            act = view_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._theme == mode)
            act.triggered.connect(lambda _checked, m=mode: self._set_theme(m))
            self._theme_group.addAction(act)

        view_menu.addSeparator()
        gradient_on = get_setting("pref.gradient", "on") == "on"
        grad_act = view_menu.addAction("Company color gradient")
        grad_act.setCheckable(True)
        grad_act.setChecked(gradient_on)
        grad_act.toggled.connect(self._set_gradient)
        self.view.set_gradient_enabled(gradient_on)   # apply saved state on startup

        events_action = self.menuBar().addAction("Upcoming Events")
        events_action.triggered.connect(self.open_events)

    def _set_theme(self, mode: str) -> None:
        self._theme = mode
        theme.apply_theme(QApplication.instance(), mode)
        set_setting("pref.theme", mode)

    def _set_gradient(self, on: bool) -> None:
        self.view.set_gradient_enabled(on)
        set_setting("pref.gradient", "on" if on else "off")

    def open_settings(self) -> None:
        # non-modal (like Events) so the async "Authorize Raylu" OAuth flow isn't
        # starved by a modal exec() loop; reload prefs when the dialog closes.
        self._settings_dialog = SettingsDialog(self)
        self._settings_dialog.finished.connect(lambda _r: self.view.reload_prefs())
        self._settings_dialog.show()
        self._settings_dialog.raise_()
        self._settings_dialog.activateWindow()

    def open_events(self) -> None:
        # non-modal (unlike Settings) so clicking an event can select the company live
        # in the main window without needing to close the popup first
        if self._events_dialog is None:
            self._events_dialog = EventsDialog(self.view, self)
        self._events_dialog.show()
        self._events_dialog.raise_()
        self._events_dialog.activateWindow()