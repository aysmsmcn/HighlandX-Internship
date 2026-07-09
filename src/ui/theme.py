"""Light/dark theme palettes, applied application-wide via QApplication.

Uses the Fusion style so the custom palette is honored consistently (the native
Windows style ignores most palette overrides). Dark is the default and matches
the app's original dark-gray/white look.
"""

from PySide6.QtGui import QPalette, QColor

DARK = "dark"
LIGHT = "light"


def _dark_palette() -> QPalette:
    p = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    p.setColor(R.Window, QColor(53, 53, 53))
    p.setColor(R.WindowText, QColor(255, 255, 255))
    p.setColor(R.Base, QColor(35, 35, 35))
    p.setColor(R.AlternateBase, QColor(53, 53, 53))
    p.setColor(R.ToolTipBase, QColor(53, 53, 53))
    p.setColor(R.ToolTipText, QColor(255, 255, 255))
    p.setColor(R.Text, QColor(255, 255, 255))
    p.setColor(R.Button, QColor(53, 53, 53))
    p.setColor(R.ButtonText, QColor(255, 255, 255))
    p.setColor(R.BrightText, QColor(255, 80, 80))
    p.setColor(R.Link, QColor(42, 130, 218))
    p.setColor(R.Highlight, QColor(42, 130, 218))
    p.setColor(R.HighlightedText, QColor(255, 255, 255))
    for role in (R.Text, R.ButtonText, R.WindowText):
        p.setColor(G.Disabled, role, QColor(127, 127, 127))
    return p


def _light_palette() -> QPalette:
    p = QPalette()
    R, G = QPalette.ColorRole, QPalette.ColorGroup
    p.setColor(R.Window, QColor(240, 240, 240))
    p.setColor(R.WindowText, QColor(0, 0, 0))
    p.setColor(R.Base, QColor(255, 255, 255))
    p.setColor(R.AlternateBase, QColor(245, 245, 245))
    p.setColor(R.ToolTipBase, QColor(255, 255, 255))
    p.setColor(R.ToolTipText, QColor(0, 0, 0))
    p.setColor(R.Text, QColor(0, 0, 0))
    p.setColor(R.Button, QColor(240, 240, 240))
    p.setColor(R.ButtonText, QColor(0, 0, 0))
    p.setColor(R.BrightText, QColor(200, 0, 0))
    p.setColor(R.Link, QColor(0, 102, 204))
    p.setColor(R.Highlight, QColor(51, 140, 255))
    p.setColor(R.HighlightedText, QColor(255, 255, 255))
    for role in (R.Text, R.ButtonText, R.WindowText):
        p.setColor(G.Disabled, role, QColor(160, 160, 160))
    return p


_PALETTES = {DARK: _dark_palette, LIGHT: _light_palette}


def _scrollbar_qss(mode: str) -> str:
    """A thin, minimal, overlay-style scrollbar to replace Fusion's chunky default.
    Scoped to QScrollBar only, so it doesn't affect item-view palette rendering."""
    if mode == LIGHT:
        handle, hover = "rgba(0, 0, 0, 0.28)", "rgba(0, 0, 0, 0.45)"
    else:
        handle, hover = "rgba(255, 255, 255, 0.28)", "rgba(255, 255, 255, 0.45)"
    return f"""
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0px; }}
    QScrollBar::handle:vertical {{ background: {handle}; min-height: 28px;
        border-radius: 5px; }}
    QScrollBar::handle:vertical:hover {{ background: {hover}; }}
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0px; }}
    QScrollBar::handle:horizontal {{ background: {handle}; min-width: 28px;
        border-radius: 5px; }}
    QScrollBar::handle:horizontal:hover {{ background: {hover}; }}
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0px; }}
    QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{ background: transparent; }}
    """


def apply_theme(app, mode: str) -> None:
    """Apply a theme to the whole application (defaults to dark on an unknown mode)."""
    app.setStyle("Fusion")
    app.setPalette(_PALETTES.get(mode, _dark_palette)())
    app.setStyleSheet(_scrollbar_qss(mode))
