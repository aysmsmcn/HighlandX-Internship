from PySide6.QtWidgets import (QDialog, QVBoxLayout, QGroupBox, QFormLayout,
                               QLineEdit, QPushButton, QComboBox, QLabel,
                               QDialogButtonBox)

from services.settings_service import get_setting, set_setting
from auth.secrets import set_secret, get_secret, AFFINITY_API_KEY, ANTHROPIC_API_KEY
from auth import ms_auth
from services import affinity_service


class SettingsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)

        # --- sections get added here (Layer 2 & 3) ---
        layout.addWidget(self._build_credentials())
        layout.addWidget(self._build_preferences())   # add
        layout.addWidget(self._build_about())          # add
        # Close button at the bottom
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)   # Close fires "rejected" → shuts the dialog
        layout.addWidget(buttons)

    def _build_credentials(self) -> QGroupBox:
        box = QGroupBox("Credentials")
        form = QFormLayout(box)

        # --- Affinity API key ---
        self.key_field = QLineEdit()
        self.key_field.setEchoMode(QLineEdit.EchoMode.Password)
        already_set = get_secret(AFFINITY_API_KEY) is not None
        self.key_field.setPlaceholderText("•••• already set" if already_set else "paste API key")

        save_key_btn = QPushButton("Save")
        save_key_btn.clicked.connect(self._save_key)

        self.key_status = QLabel("")
        form.addRow("Affinity API key:", self.key_field)
        form.addRow("", save_key_btn)
        form.addRow("", self.key_status)

        # --- Anthropic API key (for AI features) ---
        self.anthropic_field = QLineEdit()
        self.anthropic_field.setEchoMode(QLineEdit.EchoMode.Password)
        anthropic_set = get_secret(ANTHROPIC_API_KEY) is not None
        self.anthropic_field.setPlaceholderText(
            "•••• already set" if anthropic_set else "paste API key")

        save_anthropic_btn = QPushButton("Save")
        save_anthropic_btn.clicked.connect(self._save_anthropic_key)

        self.anthropic_status = QLabel("")
        form.addRow("Anthropic API key:", self.anthropic_field)
        form.addRow("", save_anthropic_btn)
        form.addRow("", self.anthropic_status)

        # --- Microsoft sign-out ---
        signout_btn = QPushButton("Sign out of Microsoft")
        signout_btn.clicked.connect(self._sign_out)
        self.signout_status = QLabel("")
        form.addRow("Microsoft:", signout_btn)
        form.addRow("", self.signout_status)

        return box

    def _save_key(self) -> None:
        text = self.key_field.text().strip()
        if not text:
            self.key_status.setText("Enter a key first.")
            return
        set_secret(AFFINITY_API_KEY, text)
        self.key_field.clear()
        self.key_status.setText("Saved ✓")

    def _save_anthropic_key(self) -> None:
        text = self.anthropic_field.text().strip()
        if not text:
            self.anthropic_status.setText("Enter a key first.")
            return
        set_secret(ANTHROPIC_API_KEY, text)
        self.anthropic_field.clear()
        self.anthropic_status.setText("Saved ✓")

    def _sign_out(self) -> None:
        ms_auth.sign_out()
        self.signout_status.setText("Signed out — you'll be asked to sign in next time.")

    def _build_preferences(self) -> QGroupBox:
        box = QGroupBox("Preferences")
        form = QFormLayout(box)

        self.order_combo = QComboBox()
        self.order_combo.addItems(["Newest first", "Oldest first", "Reach-out date (soonest)"])
        # load the saved value BEFORE connecting, so this initial set doesn't trigger a save
        self.order_combo.setCurrentText(get_setting("pref.reminders_order", "Newest first"))
        self.order_combo.currentTextChanged.connect(self._save_order)

        form.addRow("Reminders order:", self.order_combo)
        return box

    def _save_order(self, text: str) -> None:
        set_setting("pref.reminders_order", text)

    def _build_about(self) -> QGroupBox:
        box = QGroupBox("Configuration (read-only)")
        form = QFormLayout(box)
        form.addRow("Deals list ID:", QLabel(str(affinity_service.DEALS_LIST_ID)))
        form.addRow("Allowed statuses:",
                    QLabel(", ".join(sorted(affinity_service.ALLOWED_STATUSES))))
        return box