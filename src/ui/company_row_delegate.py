"""Paints company list rows with a gradient blend of the company's brand colors.

Brand colors are the dominant colors of the company's cached logo (no new network
calls) — up to 3, ordered dark→light and used as stops in a smooth horizontal
gradient. Row text switches to black/white per row (by background luminance) with
a soft shadow so it stays readable over vibrant colors. Colors are cached per
domain in memory. The whole effect can be toggled off (View ▸ Company gradient).
"""

from collections import Counter

from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QImage, QColor, QLinearGradient, QBrush, QPen, QPainter
from PySide6.QtWidgets import QStyledItemDelegate, QStyle, QWidget

from services.logo_service import _cache_path

PANEL_MIX = 0.30       # panel backdrops are fainter than the full-strength row gradient

_COLOR_CACHE: dict[str, list[QColor] | None] = {}

# Global on/off for the gradient backdrop, flipped from the View menu.
GRADIENT_ENABLED = True


def set_gradient_enabled(on: bool) -> None:
    global GRADIENT_ENABLED
    GRADIENT_ENABLED = bool(on)


def _luminance(c: QColor) -> float:
    return 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()


def _dominant_colors(domain: str | None, k: int = 3) -> list[QColor] | None:
    """Up to k dominant (non-transparent, non near-black/white) logo colors, dark→light."""
    if not domain:
        return None
    if domain in _COLOR_CACHE:
        return _COLOR_CACHE[domain]
    colors = None
    path = _cache_path(domain)
    if path.exists() and path.stat().st_size > 0:
        img = QImage(str(path))
        if not img.isNull():
            img = img.convertToFormat(QImage.Format.Format_ARGB32).scaled(32, 32)
            buckets: Counter = Counter()
            for y in range(img.height()):
                for x in range(img.width()):
                    c = img.pixelColor(x, y)
                    if c.alpha() < 128:
                        continue
                    r, g, b = c.red(), c.green(), c.blue()
                    if r > 240 and g > 240 and b > 240:      # skip near-white
                        continue
                    if r < 16 and g < 16 and b < 16:          # skip near-black
                        continue
                    buckets[(r // 24, g // 24, b // 24)] += 1  # quantize
            top = buckets.most_common(k)
            if top:
                cols = [QColor(r * 24 + 12, g * 24 + 12, b * 24 + 12) for (r, g, b), _ in top]
                cols.sort(key=_luminance)                     # dark → light for a smooth blend
                colors = cols
    _COLOR_CACHE[domain] = colors
    return colors


def _gradient_stops(colors: list[QColor]) -> list[QColor]:
    """A single dominant color becomes a darker→lighter pair so it still reads as a blend."""
    if len(colors) == 1:
        c = colors[0]
        return [c.darker(135), c.lighter(135)]
    return colors


def _blend(c: QColor, bg: QColor, t: float) -> QColor:
    """Blend brand color c toward background bg; t is the fraction of brand color."""
    return QColor(round(bg.red() * (1 - t) + c.red() * t),
                  round(bg.green() * (1 - t) + c.green() * t),
                  round(bg.blue() * (1 - t) + c.blue() * t))


_LIGHT_CACHE: dict[str, bool] = {}


def _logo_is_light(domain: str | None) -> bool:
    """True if the logo's opaque pixels are on average bright — so it needs a DARK chip
    to stay visible (dark/colored logos get a light chip instead). Cached per domain."""
    if not domain:
        return False
    if domain in _LIGHT_CACHE:
        return _LIGHT_CACHE[domain]
    result = False
    path = _cache_path(domain)
    if path.exists() and path.stat().st_size > 0:
        img = QImage(str(path))
        if not img.isNull():
            img = img.convertToFormat(QImage.Format.Format_ARGB32).scaled(16, 16)
            total, n = 0.0, 0
            for y in range(16):
                for x in range(16):
                    c = img.pixelColor(x, y)
                    if c.alpha() < 128:
                        continue
                    total += _luminance(c)
                    n += 1
            if n:
                result = (total / n) > 140
    _LIGHT_CACHE[domain] = result
    return result


class GradientPanel(QWidget):
    """A container that paints a faint brand-color gradient behind its children.
    Set the company domain with set_domain(); honors the global gradient toggle.

    set_group(container) makes several side-by-side panels share ONE continuous
    gradient: each panel paints only its slice of a gradient spanning the container."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._domain: str | None = None
        self._group: QWidget | None = None

    def set_domain(self, domain: str | None) -> None:
        if domain != self._domain:
            self._domain = domain
            self.update()

    def set_group(self, group: QWidget | None) -> None:
        self._group = group

    def refresh(self) -> None:
        self.update()

    def paintEvent(self, event) -> None:
        if GRADIENT_ENABLED and (colors := _dominant_colors(self._domain)):
            stops = _gradient_stops(colors)
            bg = self.palette().window().color()
            # span the gradient across the whole group (all sibling panels) so the visible
            # panels read as one continuous gradient rather than three separate ones.
            if self._group is not None:
                offset = self.mapTo(self._group, self.rect().topLeft()).x()
                total = max(1, self._group.width())
            else:
                offset, total = 0, max(1, self.width())
            grad = QLinearGradient(float(-offset), 0.0, float(total - offset), 0.0)
            n = len(stops)
            for i, c in enumerate(stops):
                grad.setColorAt(i / (n - 1) if n > 1 else 0.0, _blend(c, bg, PANEL_MIX))
            QPainter(self).fillRect(self.rect(), QBrush(grad))
        super().paintEvent(event)


class CompanyRowDelegate(QStyledItemDelegate):
    """Draws each row: optional brand-color gradient background, then logo + readable text.
    paint_gradient=False keeps the logo + text styling but skips the gradient (e.g. reminders)."""

    def __init__(self, parent=None, paint_gradient: bool = True, fit_width: bool = True) -> None:
        super().__init__(parent)
        self._paint_gradient = paint_gradient
        self._fit_width = fit_width

    def paint(self, painter, option, index) -> None:
        painter.save()
        rect = option.rect
        pal = option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hover = bool(option.state & QStyle.StateFlag.State_MouseOver)

        text_color = pal.text().color()
        shadow_color = None
        on_gradient = False
        if selected:
            painter.fillRect(rect, pal.highlight())
            text_color = pal.highlightedText().color()
        elif (self._paint_gradient and GRADIENT_ENABLED
              and (colors := _dominant_colors(index.data(Qt.ItemDataRole.UserRole)))):
            stops = _gradient_stops(colors)
            grad = QLinearGradient(rect.topLeft(), rect.topRight())
            n = len(stops)
            for i, c in enumerate(stops):
                grad.setColorAt(i / (n - 1) if n > 1 else 0.0, c)
            painter.fillRect(rect, QBrush(grad))
            on_gradient = True
            avg = sum(_luminance(c) for c in stops) / len(stops)
            if avg > 145:
                text_color, shadow_color = QColor(20, 20, 20), QColor(255, 255, 255, 110)
            else:
                text_color, shadow_color = QColor(245, 245, 245), QColor(0, 0, 0, 120)
        elif hover:
            painter.fillRect(rect, pal.alternateBase())

        pad = 4
        x = rect.left() + pad
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None and not icon.isNull():
            size = option.decorationSize
            pm = icon.pixmap(size)
            iy = rect.top() + (rect.height() - size.height()) // 2
            # rounded "chip" behind the logo so it always stands out — dark chip for light
            # logos, light chip for dark/colored ones (contrast); on every row and both lists.
            m = 2
            chip = QRect(x - m, iy - m, size.width() + 2 * m, size.height() + 2 * m)
            chip_color = (QColor(44, 44, 44) if _logo_is_light(index.data(Qt.ItemDataRole.UserRole))
                          else QColor(248, 248, 248))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(chip_color)
            painter.drawRoundedRect(chip, 4, 4)
            painter.drawPixmap(x, iy, pm)
            x += size.width() + pad * 2

        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        painter.setFont(option.font)
        text_rect = QRect(x, rect.top(), rect.right() - x - pad, rect.height())
        elided = painter.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
        align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        if shadow_color is not None:                          # soft shadow for legibility
            painter.setPen(QPen(shadow_color))
            painter.drawText(text_rect.translated(1, 1), align, elided)
        painter.setPen(QPen(text_color))
        painter.drawText(text_rect, align, elided)
        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        s = super().sizeHint(option, index)
        h = max(s.height(), option.decorationSize.height() + 8)
        # fit_width: report a minimal width so the list never needs a horizontal scrollbar —
        # rows span the viewport (gradient rescales with the window) and text elides.
        return QSize(option.decorationSize.width(), h) if self._fit_width else QSize(s.width(), h)
