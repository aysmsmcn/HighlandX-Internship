"""Modal dialog: an estimated fundraising timeline + landmark outreach dates.

UI layer — talks to services (affinity_service for notes, ai_service for the
estimate), never to the SDKs directly.
"""

from PySide6.QtWidgets import (QDialog, QVBoxLayout, QLabel, QTextBrowser,
                               QPushButton, QDialogButtonBox, QApplication)
from PySide6.QtCore import Qt
from qasync import asyncSlot

from services.ai_service import estimate_company
from services import pitchbook_service


def _friendly_ai_error(err: Exception) -> str:
    text = str(err).lower()
    if "api key" in text:
        return "No Anthropic API key — add one in Settings."
    if "rate_limit" in text or "429" in text:
        return ("Rate limited — your API tier's per-minute limit was hit. "
                "Wait a minute and try again, or raise your tier.")
    if any(s in text for s in ("connect", "network", "getaddrinfo", "ssl")):
        return "Couldn't reach the server — check your connection."
    return f"Something went wrong: {err}"


class FundraisingDialog(QDialog):
    def __init__(self, parent, company) -> None:
        super().__init__(parent)
        self._company = company
        self.setWindowTitle(f"Fundraising estimate — {company.name}")
        self.setMinimumSize(480, 540)

        layout = QVBoxLayout(self)
        self.caveat = QLabel("Estimate based on Claude's training knowledge + your Affinity notes "
                             "— no live web data. Treat as a rough guide, not verified fact.")
        self.caveat.setWordWrap(True)
        self.caveat.setStyleSheet("color: #666;")
        layout.addWidget(self.caveat)

        self.reader = QTextBrowser()
        self.reader.setOpenExternalLinks(True)
        layout.addWidget(self.reader, 1)

        self.reestimate_btn = QPushButton("Re-estimate")
        self.reestimate_btn.clicked.connect(self._run)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.addButton(self.reestimate_btn, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._run()   # auto-estimate on open

    @asyncSlot()
    async def _run(self) -> None:
        self.reestimate_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        self.reader.setMarkdown("_Estimating… (this calls the Anthropic API and can take a few seconds)_")
        try:
            est = await estimate_company(
                self._company.id, self._company.name, self._company.domain)
            self.caveat.setText(
                "Estimate based on PitchBook funding data + your Affinity notes."
                if pitchbook_service.AVAILABLE else
                "Estimate based on Claude's training knowledge + your Affinity notes "
                "— no live web data. Treat as a rough guide, not verified fact.")
            self.reader.setMarkdown(self._render(est))
        except Exception as err:
            self.reader.setMarkdown(f"_{_friendly_ai_error(err)}_")
        finally:
            QApplication.restoreOverrideCursor()
            self.reestimate_btn.setEnabled(True)

    @staticmethod
    def _render(est) -> str:
        lines = [
            f"## {est.projected_stage} — {est.projected_window}",
            f"**Confidence:** {est.confidence}",
            "",
            f"**Last known round:** {est.last_known_round}",
            "",
            est.rationale,
            "",
            "### Landmark outreach dates",
        ]
        if est.landmark_dates:
            for d in est.landmark_dates:
                lines.append(f"- **{d.get('date', '?')}** — {d.get('label', '')}: {d.get('reason', '')}")
        else:
            lines.append("_None suggested._")
        if est.sources:
            lines.append("")
            lines.append("### Sources")
            for s in est.sources:
                title = s.get("title") or s.get("url") or "source"
                url = s.get("url") or ""
                lines.append(f"- [{title}]({url})" if url else f"- {title}")
        return "\n".join(lines)
