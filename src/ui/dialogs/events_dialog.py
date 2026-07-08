"""Upcoming-events popup, opened from the "View Upcoming Events" menu-bar item.

Reparents AffinityView's existing events_status/events_list widgets in rather
than duplicating the fetch/match logic that already lives there.
"""

from PySide6.QtWidgets import QDialog, QVBoxLayout, QLabel


class EventsDialog(QDialog):
    def __init__(self, view, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Upcoming Events")
        self.resize(480, 420)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Events (upcoming)</b>"))
        layout.addWidget(view.events_status)
        layout.addWidget(view.events_list)
