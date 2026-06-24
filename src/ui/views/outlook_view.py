"""Outlook view. UI layer — imports services ONLY, never msgraph."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QListWidget, QLabel,
)
from qasync import asyncSlot

from services.outlook_service import get_recent_messages


class OutlookView(QWidget):
    def __init__(self) -> None:
        super().__init__()

        self.refresh_btn = QPushButton("Refresh inbox")
        self.status = QLabel("Click refresh to load your mail.")
        self.list = QListWidget()

        layout = QVBoxLayout(self)
        layout.addWidget(self.refresh_btn)
        layout.addWidget(self.status)
        layout.addWidget(self.list)

        # Clicking the button runs the async loader on the qasync loop.
        self.refresh_btn.clicked.connect(self.load)

    @asyncSlot()
    async def load(self) -> None:
        self.refresh_btn.setEnabled(False)     # prevent double-clicks mid-fetch
        self.status.setText("Loading…")
        self.list.clear()
        try:
            messages = await get_recent_messages(15)
            for e in messages:
                self.list.addItem(f"{e.received}  |  {e.sender}  —  {e.subject}")
            self.status.setText(f"Loaded {len(messages)} messages.")
        except Exception as err:               # surface failures instead of silent freeze
            self.status.setText(f"Failed to load: {err}")
        finally:
            self.refresh_btn.setEnabled(True)