"""Outlook view. UI layer — imports services ONLY, never msgraph."""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QListWidget, QLabel,
)
from qasync import asyncSlot

from services.outlook_service import get_recent_messages


from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QPushButton, QListWidget, QListWidgetItem,
    QLabel, QSplitter, QTextBrowser,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from qasync import asyncSlot

from services.outlook_service import get_recent_messages, EmailSummary


class OutlookView(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._messages: list[EmailSummary] = []

        self.refresh_btn = QPushButton("Refresh inbox")
        self.status = QLabel("Click refresh to load your mail.")

        self.list = QListWidget()
        self.reader = QTextBrowser()
        self.reader.setOpenExternalLinks(True)   # links open in the system browser

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.list)
        split.addWidget(self.reader)
        split.setStretchFactor(1, 2)             # reading pane wider

        layout = QVBoxLayout(self)
        layout.addWidget(self.refresh_btn)
        layout.addWidget(self.status)
        layout.addWidget(split)

        self.refresh_btn.clicked.connect(self.load)
        self.list.currentRowChanged.connect(self.show_message)   # selecting a row opens it

    @asyncSlot()
    async def load(self) -> None:
        self.refresh_btn.setEnabled(False)
        self.status.setText("Loading…")
        self.list.clear()
        self.reader.clear()
        try:
            self._messages = await get_recent_messages(25)
            for e in self._messages:
                item = QListWidgetItem(f"{e.sender_name}\n{e.subject}")
                if not e.is_read:                       # unread → bold, like Outlook
                    f = item.font(); f.setBold(True); item.setFont(f)
                self.list.addItem(item)
            self.status.setText(f"Loaded {len(self._messages)} messages.")
        except Exception as err:
            self.status.setText(f"Failed to load: {err}")
        finally:
            self.refresh_btn.setEnabled(True)

    def show_message(self, row: int) -> None:        # plain method — no network, no await
        if row < 0 or row >= len(self._messages):
            return
        e = self._messages[row]
        header = (
            f"<h2 style='margin:0'>{e.subject}</h2>"
            f"<p style='color:#666;margin:4px 0'>"
            f"<b>{e.sender_name}</b> &lt;{e.sender_address}&gt;<br>{e.received}</p><hr>"
        )
        if e.body_is_html:
            self.reader.setHtml(header + e.body_content)
        else:
            self.reader.setHtml(header + f"<pre style='white-space:pre-wrap'>{e.body_content}</pre>")