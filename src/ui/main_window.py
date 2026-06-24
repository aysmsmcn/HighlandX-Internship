from PySide6.QtWidgets import QMainWindow

from config import APP_NAME
from ui.views.affinity_view import AffinityView


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 720)          # wider for the 3-column layout

        self.setCentralWidget(AffinityView())
