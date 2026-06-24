"""Main application window.

UI layer only. This module must never import msgraph or playwright directly —
it talks to the service layer instead (see ROADMAP.md "Guiding rule").
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow

from config import APP_NAME


from ui.views.outlook_view import OutlookView


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(960, 640)
        self.setCentralWidget(OutlookView())   # was the placeholder QLabel