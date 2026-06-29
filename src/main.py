"""HighlandX — application entry point.

Boots Qt and merges asyncio into the Qt event loop via qasync, so service-layers
code can `await` Microsoft Graph / Playwright calls directly later on.
"""

import asyncio
import sys
from pathlib import Path

# Allow `from ui...`, `from services...` etc. when running this file directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from qasync import QEventLoop

from ui.main_window import MainWindow
from data.database import init_db

def main() -> None:
    # Required before QApplication for the embedded QtWebEngine (PitchBook panel).
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    app = QApplication(sys.argv)
    # Stable identity so the embedded web profile (saved logins) persists across launches.
    app.setApplicationName("HighlandX")
    app.setOrganizationName("HighlandX")

    init_db()

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
