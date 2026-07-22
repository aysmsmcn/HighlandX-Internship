from PySide6.QtWidgets import (QDialog, QVBoxLayout, QGroupBox, QFormLayout,
                               QLineEdit, QPushButton, QComboBox, QLabel,
                               QSpinBox, QDialogButtonBox)

from qasync import asyncSlot

from services.settings_service import get_setting, set_setting
from auth.secrets import (set_secret, get_secret, AFFINITY_API_KEY,
                          ANTHROPIC_API_KEY, RAYLU_MCP_URL, RAYLU_OAUTH_TOKENS)
from auth import ms_auth
from services import affinity_service, raylu_service
from ui.views.affinity_view import REMINDERS_ORDER_OPTIONS


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

        # --- Anthropic API key (drives Raylu enrichment via Haiku 4.5) ---
        self.anthropic_field = QLineEdit()
        self.anthropic_field.setEchoMode(QLineEdit.EchoMode.Password)
        self.anthropic_field.setPlaceholderText(
            "•••• already set" if get_secret(ANTHROPIC_API_KEY) else "paste Anthropic API key")
        anthropic_btn = QPushButton("Save")
        anthropic_btn.clicked.connect(self._save_anthropic)
        self.anthropic_status = QLabel("")
        form.addRow("Anthropic API key:", self.anthropic_field)
        form.addRow("", anthropic_btn)
        form.addRow("", self.anthropic_status)

        # --- Raylu MCP endpoint + one-time OAuth authorize ---
        self.raylu_url_field = QLineEdit(get_secret(RAYLU_MCP_URL) or "")
        self.raylu_url_field.setPlaceholderText("https://…/mcp")
        save_url_btn = QPushButton("Save URL")
        save_url_btn.clicked.connect(self._save_raylu_url)
        self.authorize_btn = QPushButton(
            "Re-authorize Raylu" if get_secret(RAYLU_OAUTH_TOKENS) else "Authorize Raylu")
        self.authorize_btn.clicked.connect(self._authorize_raylu)
        self.raylu_status = QLabel("")
        form.addRow("Raylu MCP URL:", self.raylu_url_field)
        form.addRow("", save_url_btn)
        form.addRow("Raylu access:", self.authorize_btn)
        form.addRow("", self.raylu_status)

        # --- Microsoft sign-out ---
        signout_btn = QPushButton("Sign out of Microsoft")
        signout_btn.clicked.connect(self._sign_out)
        self.signout_status = QLabel("")
        form.addRow("Microsoft:", signout_btn)
        form.addRow("", self.signout_status)

        return box

    def _save_anthropic(self) -> None:
        text = self.anthropic_field.text().strip()
        if not text:
            self.anthropic_status.setText("Enter a key first.")
            return
        set_secret(ANTHROPIC_API_KEY, text)
        self.anthropic_field.clear()
        self.anthropic_status.setText("Saved ✓")

    def _save_raylu_url(self) -> None:
        url = self.raylu_url_field.text().strip()
        if not url:
            self.raylu_status.setText("Enter the MCP URL first.")
            return
        set_secret(RAYLU_MCP_URL, url)
        self.raylu_status.setText("URL saved ✓")

    @asyncSlot()
    async def _authorize_raylu(self) -> None:
        url = self.raylu_url_field.text().strip() or get_secret(RAYLU_MCP_URL)
        if not url:
            self.raylu_status.setText("Save the MCP URL first.")
            return
        set_secret(RAYLU_MCP_URL, url)             # persist any freshly typed URL
        self.authorize_btn.setEnabled(False)
        self.raylu_status.setText("Opening your browser — approve access, then come back…")
        try:
            await raylu_service.authorize()
        except Exception as e:
            self.raylu_status.setText(f"Authorization failed: {e}")
        else:
            self.raylu_status.setText("Authorized ✓")
            self.authorize_btn.setText("Re-authorize Raylu")
        finally:
            self.authorize_btn.setEnabled(True)

    def _save_key(self) -> None:
        text = self.key_field.text().strip()
        if not text:
            self.key_status.setText("Enter a key first.")
            return
        set_secret(AFFINITY_API_KEY, text)
        self.key_field.clear()
        self.key_status.setText("Saved ✓")

    def _sign_out(self) -> None:
        ms_auth.sign_out()
        self.signout_status.setText("Signed out — you'll be asked to sign in next time.")

    def _build_preferences(self) -> QGroupBox:
        box = QGroupBox("Preferences")
        form = QFormLayout(box)

        self.order_combo = QComboBox()
        self.order_combo.addItems(REMINDERS_ORDER_OPTIONS)
        # load the saved value BEFORE connecting, so this initial set doesn't trigger a save
        self.order_combo.setCurrentText(get_setting("pref.reminders_order", "Newest first"))
        self.order_combo.currentTextChanged.connect(self._save_order)
        form.addRow("Reminders order:", self.order_combo)

        # --- fit-score bands for the "How to proceed" suggestion ---
        self.good_spin = QSpinBox()
        self.good_spin.setRange(0, 100)
        self.good_spin.setValue(int(get_setting(
            "pref.good_fit_threshold", str(affinity_service.DEFAULT_GOOD_FIT_THRESHOLD))))
        self.good_spin.setToolTip("Fit score at or above this counts as a good fit.")
        self.good_spin.valueChanged.connect(self._save_thresholds)
        form.addRow("Good-fit score ≥:", self.good_spin)

        self.pass_spin = QSpinBox()
        self.pass_spin.setRange(0, 100)
        self.pass_spin.setValue(int(get_setting(
            "pref.pass_threshold", str(affinity_service.DEFAULT_PASS_THRESHOLD))))
        self.pass_spin.setToolTip("Fit score below this is flagged as a likely pass.")
        self.pass_spin.valueChanged.connect(self._save_thresholds)
        form.addRow("Pass score <:", self.pass_spin)

        self.threshold_status = QLabel("")
        form.addRow("", self.threshold_status)

        # --- Pass All safeguard: flag companies contacted within this many months ---
        self.recent_months_spin = QSpinBox()
        self.recent_months_spin.setRange(0, 60)
        self.recent_months_spin.setValue(
            int(get_setting("pref.pass_recent_contact_months", "3")))
        self.recent_months_spin.setToolTip(
            "“Pass All” flags companies contacted within this many months for review "
            "before passing them.")
        self.recent_months_spin.valueChanged.connect(self._save_recent_months)
        form.addRow("Pass All — recent contact (months):", self.recent_months_spin)
        return box

    def _save_recent_months(self, value: int) -> None:
        set_setting("pref.pass_recent_contact_months", str(value))

    def _save_order(self, text: str) -> None:
        set_setting("pref.reminders_order", text)

    def _save_thresholds(self) -> None:
        good, pass_ = self.good_spin.value(), self.pass_spin.value()
        if pass_ >= good:
            self.threshold_status.setText("Pass score should be below the good-fit score.")
            return
        self.threshold_status.setText("")
        set_setting("pref.good_fit_threshold", str(good))
        set_setting("pref.pass_threshold", str(pass_))

    def _build_about(self) -> QGroupBox:
        box = QGroupBox("Configuration (read-only)")
        form = QFormLayout(box)
        form.addRow("Deals list ID:", QLabel(str(affinity_service.DEALS_LIST_ID)))
        form.addRow("Allowed statuses:",
                    QLabel(", ".join(sorted(affinity_service.ALLOWED_STATUSES))))
        return box