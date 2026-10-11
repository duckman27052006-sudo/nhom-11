"""Component dùng lại cho GUI.

Vì stylesheet không áp được cho toàn bộ cây widget trên Qt 6.12 (xem `theme`),
mọi bề mặt thương hiệu ở đây đều **tự vẽ bằng `QPainter`**. Widget do hệ điều
hành vẽ (input, bảng, checkbox) lấy màu từ `QPalette` của app.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractSpinBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import motion, theme

RADIUS_CARD = 10
RADIUS_TILE = 8
RADIUS_INPUT = 8


# ------------------------------------------------------------------ surface ---
class Surface(QFrame):
    """Bề mặt bo góc: nền + viền + bóng mềm, nâng nhẹ khi rê chuột.

    Lớp nền cho mọi khối giao diện. Màu lấy từ token theo tên (`bg_key`,
    `border_key`) nên đổi theme chỉ cần gọi `restyle()`.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        bg_key: str = "surface",
        border_key: str = "border",
        radius: int = RADIUS_TILE,
        shadow: bool = False,
        hover: bool = False,
        border_sides: tuple[bool, bool, bool, bool] = (True, True, True, True),
    ) -> None:
        super().__init__(parent)
        self._bg_key = bg_key
        self._border_key = border_key
        self._radius = radius
        self._shadow = shadow
        self._hover_enabled = hover
        self._sides = border_sides  # (top, right, bottom, left)
        self._lift = 0.0
        self._anim: motion.QVariantAnimation | None = None
        if hover:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, shadow)

    # ----------------------------------------------------------------- vẽ ---
    def paintEvent(self, event) -> None:  # noqa: N802 - tên do Qt đặt
        tokens = theme.current_tokens()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if self._shadow:
            self._paint_shadow(QRectF(rect))

        bg = QColor(tokens[self._bg_key])
        if self._hover_enabled and self._lift > 0:
            # Pha nhẹ sang nền thương hiệu: đủ để thấy "đang rê" mà không
            # trông như đã được chọn.
            amount = 0.35 * self._lift
            primary = QColor(tokens["primary_soft"])
            bg = QColor(
                int(bg.red() + (primary.red() - bg.red()) * amount),
                int(bg.green() + (primary.green() - bg.green()) * amount),
                int(bg.blue() + (primary.blue() - bg.blue()) * amount),
            )

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawRoundedRect(rect, self._radius, self._radius)

        border = QColor(tokens[self._border_key])
        if self._hover_enabled and self._lift > 0:
            primary_border = QColor(tokens["primary_border"])
            border = QColor(
                int(border.red() + (primary_border.red() - border.red()) * self._lift),
                int(border.green() + (primary_border.green() - border.green()) * self._lift),
                int(border.blue() + (primary_border.blue() - border.blue()) * self._lift),
            )
        top, right, bottom, left = self._sides
        widths = (int(top), int(right), int(bottom), int(left))
        for width, (x1, y1, x2, y2) in zip(
            widths,
            (
                (rect.left(), rect.top(), rect.right(), rect.top()),
                (rect.right(), rect.top(), rect.right(), rect.bottom()),
                (rect.left(), rect.bottom(), rect.right(), rect.bottom()),
                (rect.left(), rect.top(), rect.left(), rect.bottom()),
            ),
            strict=False,
        ):
            if width:
                pen = QPen(border, width)
                pen.setCapStyle(Qt.PenCapStyle.FlatCap)
                painter.setPen(pen)
                painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))
        painter.end()

    def _paint_shadow(self, rect: QRectF) -> None:
        tokens = theme.current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        shadow = QColor(tokens["shadow"])
        base = 16 if theme.is_light() else 46
        depth = int(3 + 3 * self._lift)
        for step in range(depth, 0, -1):
            shadow.setAlpha(max(2, int((base + 16 * self._lift) / (step + 1))))
            painter.setBrush(shadow)
            grown = rect.adjusted(-0.5, step * 0.6, 0.5, step * 1.4)
            painter.drawRoundedRect(grown, self._radius + step, self._radius + step)
        painter.end()

    # ---------------------------------------------------------------- hover ---
    def enterEvent(self, event) -> None:  # noqa: N802
        if self._hover_enabled:
            self._animate_lift(1.0)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._hover_enabled:
            self._animate_lift(0.0)
        super().leaveEvent(event)

    def _animate_lift(self, target: float) -> None:
        if self._anim is None:
            self._anim = motion.QVariantAnimation(self)
            self._anim.setDuration(motion.DUR_BASE)
            self._anim.setEasingCurve(motion.out_cubic())
            self._anim.valueChanged.connect(self._apply_lift)
        self._anim.stop()
        self._anim.setStartValue(self._lift)
        self._anim.setEndValue(target)
        self._anim.start()

    def _apply_lift(self, value: float) -> None:
        self._lift = value
        self.update()

    def restyle(self) -> None:
        self.update()


class Card(Surface):
    """Panel chính: nền + viền mảnh, không bóng đổ mặc định.

    Card nội dung không phải nút nên không có hover nâng (tránh affordance
    giả). Chỉ hero/toast dùng `elevated=True` để có bóng tint theo nền.
    """

    def __init__(self, parent: QWidget | None = None, *, elevated: bool = False) -> None:
        super().__init__(parent, radius=RADIUS_CARD, shadow=elevated, hover=False)
        self._body = QVBoxLayout(self)
        self._body.setContentsMargins(20, 20, 20, 24)
        self._body.setSpacing(14)

    def add(self, widget: QWidget) -> QWidget:
        self._body.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:
        self._body.addLayout(layout)

    def add_stretch(self, factor: int = 1) -> None:
        """Khoảng trống dồn về đây thay vì nở header/field (xem CardHeader)."""
        self._body.addStretch(factor)


class StatTile(Surface):
    """Ô số liệu: nhãn + số lớn + gợi ý. Số chạy tăng dần khi đổi."""

    def __init__(self, label: str, hint: str = "") -> None:
        super().__init__(radius=RADIUS_TILE)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(2)

        self.label = QLabel(label)
        self.label.setFont(theme.medium(8.0))
        self.label.setStyleSheet(f"color: {theme.current_tokens()['text_subtle']};")
        layout.addWidget(self.label)

        self.value = QLabel("—")
        self.value.setFont(theme.bold(20, mono=True))
        layout.addWidget(self.value)

        self.hint = QLabel(hint)
        self.hint.setFont(theme.font(8.5))
        self.hint.setStyleSheet(f"color: {theme.current_tokens()['text_subtle']};")
        self.hint.setVisible(bool(hint))
        layout.addWidget(self.hint)

        self._counter = motion.Counter(self._render, self)
        self._format: object = "{:d}"
        self._text = "—"

    def _render(self, number: float) -> None:
        if callable(self._format):
            self._text = str(self._format(number))
        else:
            self._text = str(self._format).format(int(number))
        self.value.setText(self._text)

    def set_number(self, number: float, fmt: object = "{:d}", animate: bool = True) -> None:
        """Đặt giá trị số. `fmt` là chuỗi định dạng hoặc hàm nhận số thực."""
        self._format = fmt
        if animate:
            self._counter.to(number)
        else:
            self._counter.set_immediate(number)

    def set_text(self, text: str, hint: str = "") -> None:
        """Đặt trực tiếp (dùng cho giá trị không phải số, ví dụ "1.9 MB")."""
        self._counter.stop()
        self._text = text
        self.value.setText(text)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))

    def restyle(self) -> None:
        super().restyle()
        tokens = theme.current_tokens()
        self.label.setStyleSheet(f"color: {tokens['text_subtle']};")
        self.hint.setStyleSheet(f"color: {tokens['text_subtle']};")


class IconGlyph(QWidget):
    """Icon vector vẽ bằng QPainter: một nét 1.6px thống nhất mọi nơi.

    Thay các glyph chữ ("↓↑⚙≡›") vốn phụ thuộc font hệ thống, nét không đều.
    `box` > 0: vẽ nền bo góc `bg_key` cỡ `box` và icon `size` căn giữa.
    """

    def __init__(
        self,
        kind: str = "dot",
        size: int = 16,
        parent: QWidget | None = None,
        *,
        color_key: str = "text_muted",
        bg_key: str | None = None,
        box: int = 0,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._size = size
        self._color_key = color_key
        self._bg_key = bg_key
        edge = box if box > 0 else size
        self.setFixedSize(edge, edge)

    def paintEvent(self, event) -> None:  # noqa: N802
        tokens = theme.current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._bg_key is not None:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(tokens[self._bg_key]))
            painter.drawRoundedRect(QRectF(self.rect()), 8, 8)
        pen = QPen(QColor(tokens[self._color_key]))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        center = QRectF(self.rect()).center()
        arm = self._size / 2
        painter.drawPath(self._path(center, arm))
        painter.end()

    def _path(self, center: QPointF, u: float) -> QPainterPath:
        path = QPainterPath()
        cx, cy = center.x(), center.y()
        kind = self._kind
        if kind == "down":
            path.moveTo(cx, cy - u)
            path.lineTo(cx, cy + u)
            path.moveTo(cx - u * 0.55, cy + u * 0.45)
            path.lineTo(cx, cy + u)
            path.lineTo(cx + u * 0.55, cy + u * 0.45)
        elif kind == "up":
            path.moveTo(cx, cy + u)
            path.lineTo(cx, cy - u)
            path.moveTo(cx - u * 0.55, cy - u * 0.45)
            path.lineTo(cx, cy - u)
            path.lineTo(cx + u * 0.55, cy - u * 0.45)
        elif kind == "upload":
            path.moveTo(cx, cy + u * 0.4)
            path.lineTo(cx, cy - u)
            path.moveTo(cx - u * 0.55, cy - u * 0.45)
            path.lineTo(cx, cy - u)
            path.lineTo(cx + u * 0.55, cy - u * 0.45)
            path.moveTo(cx - u, cy + u)
            path.lineTo(cx + u, cy + u)
        elif kind == "chevron-right":
            path.moveTo(cx - u * 0.4, cy - u * 0.7)
            path.lineTo(cx + u * 0.4, cy)
            path.lineTo(cx - u * 0.4, cy + u * 0.7)
        elif kind == "log":
            for row in (-0.5, 0.1, 0.7):
                y = cy + u * row
                path.moveTo(cx - u, y)
                path.lineTo(cx + u, y)
        elif kind == "refresh":
            path.moveTo(cx + u * 0.9, cy - u * 0.9)
            path.lineTo(cx + u * 0.9, cy - u * 0.1)
            path.lineTo(cx + u * 0.1, cy - u * 0.1)
            path.moveTo(cx, cy)
            path.arcTo(cx - u * 0.8, cy - u * 0.8, u * 1.6, u * 1.6, 45, 270)
        elif kind == "folder":
            path.moveTo(cx - u, cy - u * 0.2)
            path.lineTo(cx - u * 0.3, cy - u * 0.2)
            path.lineTo(cx, cy - u * 0.6)
            path.lineTo(cx + u * 0.4, cy - u * 0.6)
            path.lineTo(cx + u * 0.4, cy - u * 0.2)
            path.moveTo(cx - u, cy - u * 0.2)
            path.lineTo(cx - u, cy + u * 0.7)
            path.lineTo(cx + u, cy + u * 0.7)
            path.lineTo(cx + u, cy - u * 0.2)
            path.closeSubpath()
        elif kind == "file":
            path.moveTo(cx - u * 0.6, cy - u)
            path.lineTo(cx + u * 0.2, cy - u)
            path.lineTo(cx + u * 0.6, cy - u * 0.6)
            path.lineTo(cx + u * 0.6, cy + u)
            path.lineTo(cx - u * 0.6, cy + u)
            path.closeSubpath()
            path.moveTo(cx + u * 0.2, cy - u)
            path.lineTo(cx + u * 0.2, cy - u * 0.6)
            path.lineTo(cx + u * 0.6, cy - u * 0.6)
        elif kind == "x":
            path.moveTo(cx - u * 0.6, cy - u * 0.6)
            path.lineTo(cx + u * 0.6, cy + u * 0.6)
            path.moveTo(cx + u * 0.6, cy - u * 0.6)
            path.lineTo(cx - u * 0.6, cy + u * 0.6)
        else:  # "dot" và mọi kind lạ: chấm tròn đặc
            path.addEllipse(cx - 2, cy - 2, 4, 4)
        return path


class ActionTile(Surface):
    """Ô bấm được để nhảy sang trang khác."""

    clicked = Signal()

    def __init__(self, title: str, hint: str, icon: str = "") -> None:
        super().__init__(radius=RADIUS_TILE, hover=True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(12)

        self._mark = None
        if icon:
            self._mark = IconGlyph(
                icon, 16, color_key="primary", bg_key="primary_soft", box=32
            )
            layout.addWidget(self._mark)

        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        self.title = QLabel(title)
        self.title.setFont(theme.bold(10))
        text_box.addWidget(self.title)
        self.hint = QLabel(hint)
        self.hint.setFont(theme.font(8.5))
        self.hint.setWordWrap(True)
        text_box.addWidget(self.hint)
        text_box.addStretch(1)
        layout.addLayout(text_box, 1)

        self.arrow = IconGlyph("chevron-right", 14)
        layout.addWidget(self.arrow, 0, Qt.AlignmentFlag.AlignVCenter)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(
            event.position().toPoint()
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def restyle(self) -> None:
        super().restyle()
        tokens = theme.current_tokens()
        self.hint.setStyleSheet(f"color: {tokens['text_subtle']};")
        if self._mark is not None:
            self._mark.update()
        self.arrow.update()


class BrandMark(QWidget):
    """Logo nhỏ: ô bo góc màu thương hiệu + chữ."""

    def __init__(self, text: str, size: int = 34, radius: int = 10) -> None:
        super().__init__()
        self._text = text
        self.setFixedSize(size, size)
        self._radius = radius

    def paintEvent(self, event) -> None:  # noqa: N802
        tokens = theme.current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens["primary"]))
        painter.drawRoundedRect(QRectF(self.rect()), self._radius, self._radius)
        painter.setPen(QPen(QColor(tokens["on_primary"])))
        f = theme.bold(self.height() * 0.44)
        painter.setFont(f)
        # Offset +1px: chữ đơn trong Segoe/Roboto nặng phần trên, tâm quang học
        # nằm dưới tâm hình học ~1px.
        text_rect = QRectF(self.rect()).adjusted(0, 1, 0, 1)
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()


class ActivityFeed(QFrame):
    """Dải log rút gọn: vài dòng gần nhất, màu theo mức."""

    def __init__(self, lines: int = 6) -> None:
        super().__init__()
        self._surface = Surface(
            self, bg_key="surface_sunken", border_key="border", radius=RADIUS_TILE
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._surface)

        layout = QVBoxLayout(self._surface)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(5)
        self._labels: list[QLabel] = []
        for _ in range(lines):
            label = QLabel("")
            label.setFont(theme.font(8.5, mono=True))
            label.setVisible(False)
            layout.addWidget(label)
            self._labels.append(label)
        self._hint = QLabel(
            "Chưa có hoạt động nào — khởi động server hoặc truyền file "
            "để xem log tại đây."
        )
        self._hint.setFont(theme.font(8.5))
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)
        self._lines: list[tuple[str, str]] = []
        self.restyle()

    def push(self, level: str, text: str) -> None:
        self._lines.append((level, text))
        self._lines = self._lines[-len(self._labels) :]
        tokens = theme.current_tokens()
        colors = {
            "DEBUG": tokens["log_debug"],
            "INFO": tokens["text_muted"],
            "WARNING": tokens["log_warning"],
            "ERROR": tokens["log_error"],
            "CRITICAL": tokens["log_error"],
        }
        for label, (lvl, line) in zip(self._labels, self._lines, strict=False):
            label.setText(line)
            label.setStyleSheet(f"color: {colors.get(lvl, tokens['text_muted'])};")
            label.setVisible(True)
        self._hint.setVisible(not self._lines)

    def restyle(self) -> None:
        self._surface.restyle()
        tokens = theme.current_tokens()
        colors = {
            "DEBUG": tokens["log_debug"],
            "INFO": tokens["text_muted"],
            "WARNING": tokens["log_warning"],
            "ERROR": tokens["log_error"],
            "CRITICAL": tokens["log_error"],
        }
        for label, (lvl, _line) in zip(self._labels, self._lines, strict=False):
            label.setStyleSheet(f"color: {colors.get(lvl, tokens['text_muted'])};")
        self._hint.setStyleSheet(f"color: {tokens['text_subtle']};")


class CardHeader(QWidget):
    """Tiêu đề card: tiêu đề + gợi ý, badge tùy chọn bên phải."""

    def __init__(self, title: str, hint: str = "", badge: str = "") -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        self.title = QLabel(title)
        self.title.setFont(theme.bold(11))
        text_box.addWidget(self.title)
        self.hint = QLabel(hint)
        self.hint.setFont(theme.font(8.5))
        self.hint.setStyleSheet(f"color: {theme.current_tokens()['text_muted']};")
        self.hint.setWordWrap(True)
        self.hint.setVisible(bool(hint))
        text_box.addWidget(self.hint)
        text_box.addStretch(1)
        layout.addLayout(text_box, 1)

        self.badge = QLabel(badge)
        self.badge.setFont(theme.medium(8.0))
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setContentsMargins(10, 4, 10, 4)
        self.badge.setVisible(bool(badge))
        layout.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignTop)

    def set_badge(self, text: str) -> None:
        self.badge.setText(text)
        self.badge.setVisible(bool(text))
        self.badge.setStyleSheet(
            f"color: {theme.current_tokens()['text_muted']};"
            f" background: {theme.current_tokens()['surface_sunken']};"
            f" border: 1px solid {theme.current_tokens()['border']};"
            f" border-radius: 10px;"
        )

    def restyle(self) -> None:
        tokens = theme.current_tokens()
        self.hint.setStyleSheet(f"color: {tokens['text_muted']};")
        if self.badge.isVisible():
            self.set_badge(self.badge.text())


# -------------------------------------------------------------------- pill ---
class StatusPill(QWidget):
    """Pill trạng thái tự vẽ: có chấm tròn nhịp thở khi đang hoạt động."""

    _STATES = {
        "idle": ("surface_sunken", "text_subtle", "border"),
        "ok": ("success_soft", "success", "success_border"),
        "busy": ("primary_soft", "primary", "primary_border"),
        "warn": ("warning_soft", "warning", "warning_border"),
        "err": ("danger_soft", "danger", "danger_border"),
    }

    def __init__(self, text: str = "", state: str = "idle", width: int = 150) -> None:
        super().__init__()
        self._state = state
        self._text = text
        self._value = 0.0
        self.setFixedHeight(28)
        self.setMinimumWidth(width)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._pulse = motion.Pulse(self._apply, self, duration=1500)

    def _apply(self, value: float) -> None:
        self._value = value
        self.update()

    def set_state(self, state: str, text: str | None = None) -> None:
        self._state = state
        if text is not None:
            self._text = text
            # Nhãn dài sẽ bị cắt nếu giữ nguyên bề rộng tối thiểu.
            self.setMinimumWidth(max(140, 30 + 8 * len(text)))
        if state in ("ok", "busy", "warn"):
            self._pulse.start()
        else:
            self._pulse.stop()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        tokens = theme.current_tokens()
        bg_key, fg_key, border_key = self._STATES.get(self._state, self._STATES["idle"])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(QColor(tokens[border_key]), 1))
        painter.setBrush(QColor(tokens[bg_key]))
        painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)

        # Chấm trạng thái: sáng dần theo nhịp để thấy "đang sống".
        dot = QColor(tokens[fg_key])
        dot.setAlphaF(0.35 + 0.65 * self._value)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(dot)
        painter.drawEllipse(QPointF(16, self.height() / 2), 4, 4)

        painter.setPen(QPen(QColor(tokens[fg_key])))
        painter.setFont(theme.bold(9))
        text_rect = QRectF(28, 0, self.width() - 38, self.height())
        painter.drawText(
            text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self._text
        )
        painter.end()


class ShimmerProgressBar(QWidget):
    """Thanh tiến trình tự vẽ, có dải sáng chạy khi đang chạy."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(8)
        self._value = 0.0
        self._total: float | None = None
        self._state = ""
        self._busy = False
        self._pulse = motion.Pulse(self._apply, self, duration=1500)

    def _apply(self, value: float) -> None:
        self.update()

    def set_progress(self, done: int, total: int | None) -> None:
        self._value = float(done)
        self._total = float(total) if total else None
        self.set_busy(total is None)
        self.update()

    def set_state(self, state: str) -> None:
        self._state = state
        self.update()

    def reset(self) -> None:
        self._value = 0.0
        self._total = None
        self._state = ""
        self.set_busy(False)
        self.update()

    def set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        self._pulse.start() if busy else self._pulse.stop()

    def paintEvent(self, event) -> None:  # noqa: N802
        tokens = theme.current_tokens()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        radius = self.height() / 2
        track = QRectF(0, 0, self.width(), self.height())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(tokens["surface_sunken"]))
        painter.drawRoundedRect(track, radius, radius)

        if self._total:
            ratio = max(0.0, min(1.0, self._value / self._total))
        elif self._busy:
            ratio = 0.28 + 0.14 * self._value
        else:
            ratio = 0.0
        if ratio <= 0:
            painter.end()
            return

        chunk = QRectF(0, 0, self.width() * ratio, self.height())
        fill = {"ok": tokens["success"], "err": tokens["danger"]}.get(
            self._state, tokens["primary"]
        )
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(chunk, radius, radius)

        # Dải sáng chạy qua thanh để thấy tiến trình còn đang chạy.
        if self._busy or self._state == "":
            band = QColor(255, 255, 255, 46)
            band_x = chunk.width() * (self._value - 0.5)
            path = QPainterPath()
            path.addRect(QRectF(band_x, 0, self.width() * 0.22, self.height()))
            painter.setClipPath(path, Qt.ClipOperation.IntersectClip)
            painter.setBrush(band)
            painter.drawRoundedRect(chunk, radius, radius)
        painter.end()


# ------------------------------------------------------------------ button ---
class ThemedButton(QPushButton):
    """Nút tự vẽ theo biến thể: `primary` / `danger` / `subtle` / `ghost` / rỗng."""

    def __init__(
        self,
        text: str = "",
        variant: str = "",
        tooltip: str = "",
        minimum_height: int = 36,
    ) -> None:
        super().__init__(text)
        self._variant = variant
        self._radius = RADIUS_INPUT
        if tooltip:
            self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(minimum_height)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def setVariant(self, variant: str) -> None:  # noqa: N802 - đồng bộ với Qt API
        self._variant = variant
        self.update()

    def _colors(self) -> tuple[str, str, str]:
        """Trả (nền, viền, chữ) theo biến thể và trạng thái."""
        t = theme.current_tokens()
        if not self.isEnabled():
            return t["surface_sunken"], t["border"], t["text_subtle"]
        if self._variant == "primary":
            if self.isDown():
                return t["primary_pressed"], t["primary_pressed"], t["on_primary"]
            if self.underMouse():
                return t["primary_hover"], t["primary_hover"], t["on_primary"]
            return t["primary"], t["primary"], t["on_primary"]
        if self._variant == "danger":
            if self.isDown() or self.underMouse():
                return t["danger"], t["danger"], t["on_primary"]
            return t["danger_soft"], t["danger_border"], t["danger"]
        if self._variant == "nav_active":
            if self.isDown() or self.underMouse():
                return t["primary_pressed"], t["primary_pressed"], t["on_primary"]
            return t["primary_soft"], t["primary_border"], t["primary"]
        if self._variant == "seg_active":
            # Nút đang chọn trong segmented control: nền nổi + viền rõ.
            if self.isDown() or self.underMouse():
                return t["surface_alt"], t["border_strong"], t["text"]
            return t["surface"], t["border_strong"], t["text"]
        if self._variant == "ghost":
            bg = t["surface_sunken"] if self.underMouse() else "transparent"
            return bg, "transparent", t["text_muted"]
        if self._variant == "tertiary":
            bg = t["primary_soft"] if self.underMouse() else "transparent"
            return bg, "transparent", t["primary"]
        if self._variant == "subtle":
            if self.underMouse():
                return t["surface_alt"], t["primary_border"], t["primary"]
            return t["surface"], t["border_strong"], t["text"]
        # outline (mặc định)
        if self.underMouse():
            return t["surface_alt"], t["primary_border"], t["text"]
        return t["surface"], t["border_strong"], t["text"]

    def paintEvent(self, event) -> None:  # noqa: N802
        t = theme.current_tokens()
        bg, border, fg = self._colors()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        if bg != "transparent":
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(bg))
            painter.drawRoundedRect(rect, self._radius, self._radius)
        if border != "transparent":
            painter.setPen(QPen(QColor(border), 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect, self._radius, self._radius)
        if self.hasFocus():
            painter.setPen(QPen(QColor(t["primary"]), 2))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), self._radius, self._radius)

        painter.setPen(QPen(QColor(fg)))
        strong = self._variant in ("primary", "danger")
        painter.setFont(theme.bold(9.5) if strong else theme.font(9.5))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.text())
        painter.end()


# ------------------------------------------------------------------- field ---
class Field(QWidget):
    """Nhãn + control + dòng gợi ý / lỗi, dùng chung nhịp spacing 4/8.

    Stretch cuối VBox hút mọi khoảng thừa khi Field bị kéo cao hơn nội dung
    (chung hàng với Field khác có hint): không có nó, QLabel nhãn (Preferred)
    sẽ tự giãn ra và đẩy control xuống → các ô lệch nhau vài chục px.
    """

    def __init__(self, label: str, control: QWidget, hint: str = "") -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        caption = QLabel(label)
        caption.setFont(theme.label_font(8.5))
        caption.setStyleSheet(f"color: {theme.current_tokens()['text_muted']};")
        layout.addWidget(caption)
        layout.addWidget(control)
        self.control = control

        self.hint = QLabel(hint)
        self.hint.setFont(theme.font(8.5))
        self.hint.setStyleSheet(f"color: {theme.current_tokens()['text_subtle']};")
        self.hint.setWordWrap(True)
        self.hint.setVisible(bool(hint))
        layout.addWidget(self.hint)
        layout.addStretch(1)


class NumberInput(QWidget):
    """Ô nhập số gọn: QSpinBox ẩn nút tăng/giảm, chữ mono căn phải.

    `width` là chiều rộng tối thiểu; ô nở theo cột lưới để các hàng thẳng đều,
    không còn hộp số lởm chởm mỗi cái một cỡ.
    """

    def __init__(
        self,
        minimum: int = 0,
        maximum: int = 100,
        value: int = 0,
        *,
        suffix: str = "",
        special_text: str = "",
        width: int = 140,
    ) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.spin = QSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setValue(value)
        self.spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin.setMinimumHeight(34)
        self.spin.setMinimumWidth(width)
        self.spin.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.spin.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.spin.setFont(theme.font(9.5, mono=True))
        if suffix:
            self.spin.setSuffix(suffix)
        if special_text:
            self.spin.setSpecialValueText(special_text)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.spin)

    def value(self) -> int:
        return self.spin.value()

    def setValue(self, value: int) -> None:  # noqa: N802 - tương thích Qt API
        self.spin.setValue(value)

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802
        self.spin.setRange(minimum, maximum)

    def setSuffix(self, suffix: str) -> None:  # noqa: N802 - tương thích Qt API
        self.spin.setSuffix(suffix)

    def setSpecialValueText(self, text: str) -> None:  # noqa: N802 - tương thích Qt API
        self.spin.setSpecialValueText(text)


class ChevronComboBox(QComboBox):
    """ComboBox tự vẽ mũi tên để đúng màu ở cả theme sáng và tối."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(self.palette().color(self.foregroundRole())))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        path = QPainterPath()
        x = self.width() - 16
        y = self.height() / 2
        path.moveTo(x - 4, y - 2)
        path.lineTo(x, y + 2)
        path.lineTo(x + 4, y - 2)
        painter.drawPath(path)
        painter.end()


class FilePicker(QWidget):
    """Ô nhập đường dẫn + nút Chọn, dùng cho cả mở và lưu."""

    def __init__(self, caption: str, save_mode: bool = False, start_dir: str = "") -> None:
        super().__init__()
        self._save_mode = save_mode
        self._caption = caption
        self._start_dir = start_dir

        self.edit = QLineEdit()
        self.edit.setPlaceholderText(caption)
        self.edit.setMinimumHeight(34)
        self.edit.textChanged.connect(self._sync_tooltip)
        self.button = ThemedButton("Chọn...", "subtle", minimum_height=34)
        self.button.clicked.connect(self._browse)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.edit, 1)
        row.addWidget(self.button, 0)

        caption_label = QLabel(caption)
        caption_label.setFont(theme.label_font(8.5))
        caption_label.setStyleSheet(f"color: {theme.current_tokens()['text_muted']};")

        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setVisible(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(caption_label)
        layout.addLayout(row)
        layout.addWidget(self.hint)

    def _browse(self) -> None:
        start = self.edit.text().strip() or self._start_dir
        if self._save_mode:
            path, _ = QFileDialog.getSaveFileName(self, self._caption, start)
        else:
            path, _ = QFileDialog.getOpenFileName(self, self._caption, start)
        if path:
            self.edit.setText(path)
            self.set_error("")

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, value: str) -> None:  # noqa: N802 - tương thích Qt API
        self.edit.setText(value)
        # Đường dẫn dài: cuộn tới cuối để thấy tên file/thư mục + tooltip full.
        self.edit.setCursorPosition(len(value))

    def _sync_tooltip(self, text: str) -> None:
        self.edit.setToolTip(text)

    def set_error(self, message: str) -> None:
        tokens = theme.current_tokens()
        self.edit.setProperty("state", "error" if message else "")
        self.hint.setFont(theme.font(8.5))
        self.hint.setStyleSheet(f"color: {tokens['danger'] if message else tokens['text_subtle']};")
        self.hint.setText(message)
        self.hint.setVisible(bool(message))


class DropZone(QFrame):
    """Vùng kéo–thả file; chỉ nhận đường dẫn, không tự động upload."""

    fileDropped = Signal(str)

    def __init__(self, text: str = "Kéo file vào đây") -> None:
        super().__init__()
        self.setAcceptDrops(True)
        self.setFixedHeight(58)
        self.label = QLabel(text)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setFont(theme.font(8.5))
        self.label.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.label)
        self._style(False)

    def _style(self, active: bool) -> None:
        tokens = theme.current_tokens()
        border = tokens["primary"] if active else tokens["border_strong"]
        background = tokens["primary_soft"] if active else tokens["surface"]
        self.setStyleSheet(
            f"QFrame {{ border: 1px dashed {border}; border-radius: {RADIUS_TILE}px;"
            f" background: {background}; }}"
        )
        self.label.setStyleSheet(f"color: {tokens['text_subtle']};")

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self._style(True)

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        self._style(False)

    def dropEvent(self, event) -> None:  # noqa: N802
        self._style(False)
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                self.label.setText(path)
                self.fileDropped.emit(path)
                event.acceptProposedAction()
                return

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        # Bấm vào vùng cũng mở hộp chọn file (không chỉ kéo–thả).
        if event.button() == Qt.MouseButton.LeftButton:
            path, _ = QFileDialog.getOpenFileName(self, "Chọn file", "")
            if path:
                self.label.setText(path)
                self.fileDropped.emit(path)
        super().mouseReleaseEvent(event)

    def restyle(self) -> None:
        self._style(False)


# ------------------------------------------------------------- trạng thái ---
class EmptyState(QWidget):
    """Trạng thái trống có thiết kế: icon + tiêu đề + mô tả + nút hành động."""

    actioned = Signal()

    def __init__(self, icon: str = "file", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 24, 16, 24)
        layout.setSpacing(8)
        layout.addStretch(1)
        self.icon = IconGlyph(icon, 22)
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignHCenter)
        self.title = QLabel("")
        self.title.setFont(theme.bold(10))
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.title)
        self.hint = QLabel("")
        self.hint.setFont(theme.font(8.5))
        self.hint.setWordWrap(True)
        self.hint.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.hint)
        self.action = ThemedButton("", "subtle", minimum_height=34)
        self.action.setVisible(False)
        self.action.clicked.connect(self.actioned.emit)
        layout.addWidget(self.action, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        self.restyle()

    def configure(self, title: str, hint: str = "", action: str = "") -> None:
        self.title.setText(title)
        self.hint.setText(hint)
        self.hint.setVisible(bool(hint))
        self.action.setText(action)
        self.action.setVisible(bool(action))

    def restyle(self) -> None:
        tokens = theme.current_tokens()
        self.title.setStyleSheet(f"color: {tokens['text']};")
        self.hint.setStyleSheet(f"color: {tokens['text_subtle']};")
        self.icon.update()


class ErrorBanner(QWidget):
    """Dải lỗi inline trong card: icon + thông điệp, ẩn khi không có lỗi."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(10)
        self.icon = IconGlyph("x", 14, color_key="danger")
        layout.addWidget(self.icon, 0, Qt.AlignmentFlag.AlignVCenter)
        self.label = QLabel("")
        self.label.setFont(theme.font(9))
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self.setVisible(False)
        self.restyle()

    def set_message(self, text: str) -> None:
        self.label.setText(text)
        self.setVisible(bool(text))

    def clear(self) -> None:
        self.set_message("")

    def restyle(self) -> None:
        tokens = theme.current_tokens()
        self.setStyleSheet(
            f"background: {tokens['danger_soft']};"
            f" border: 1px solid {tokens['danger_border']};"
            f" border-radius: {RADIUS_TILE}px;"
        )
        self.label.setStyleSheet(f"color: {tokens['danger']}; background: transparent;")
        self.icon.update()


class TableHeader(QHeaderView):
    """Header bảng tự vẽ: nền surface + viền đáy, chữ nhỏ xám.

    QHeaderView của Fusion tự đổ gradient xám dù đã ghim palette, nên vẽ tay
    để header trắng đều với hàng dữ liệu.
    """

    def __init__(
        self, orientation: Qt.Orientation, parent: QWidget | None = None
    ) -> None:
        super().__init__(orientation, parent)
        self._right: set[int] = set()
        self.setHighlightSections(False)
        self.setSectionsClickable(False)

    def set_right_aligned(self, columns: tuple[int, ...]) -> None:
        self._right = set(columns)
        self.update()

    def paintSection(  # noqa: N802 - tên do Qt đặt
        self, painter: QPainter, rect, logicalIndex: int
    ) -> None:
        tokens = theme.current_tokens()
        painter.save()
        painter.fillRect(rect, QColor(tokens["surface"]))
        painter.setPen(QPen(QColor(tokens["border"]), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        model = self.model()
        text = (
            str(model.headerData(logicalIndex, self.orientation()))
            if model is not None
            else ""
        )
        painter.setPen(QPen(QColor(tokens["text_muted"])))
        painter.setFont(theme.medium(8.5))
        align = Qt.AlignmentFlag.AlignVCenter | (
            Qt.AlignmentFlag.AlignRight
            if logicalIndex in self._right
            else Qt.AlignmentFlag.AlignLeft
        )
        painter.drawText(rect.adjusted(8, 0, -8, 0), align, text)
        painter.restore()


class DataTable(QWidget):
    """Bảng dữ liệu gọn: header mảnh + zebra + empty state tích hợp.

    Bọc QTableWidget để call site cũ (`.table.setItem…`) vẫn dùng được.
    """

    def __init__(
        self,
        headers: list[str],
        parent: QWidget | None = None,
        *,
        stretch_column: int = 0,
        minimum_height: int = 220,
        numeric_columns: tuple[int, ...] = (),
    ) -> None:
        super().__init__(parent)
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(list(headers))
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setShowGrid(False)
        self.table.setMinimumHeight(minimum_height)
        header = TableHeader(Qt.Orientation.Horizontal, self.table)
        header.set_right_aligned(numeric_columns)
        self.table.setHorizontalHeader(header)
        head = self.table.horizontalHeader()
        for index in range(len(headers)):
            mode = (
                QHeaderView.ResizeMode.Stretch
                if index == stretch_column
                else QHeaderView.ResizeMode.ResizeToContents
            )
            head.setSectionResizeMode(index, mode)
        self.empty = EmptyState("file")
        self.empty.setVisible(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty, 1)

    def clear_rows(self) -> None:
        self.table.setRowCount(0)
        self.table.setVisible(True)
        self.empty.setVisible(False)

    def add_row(self, cells: list[str], *, right_align: tuple[int, ...] = ()) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, text in enumerate(cells):
            item = QTableWidgetItem(text)
            if col in right_align:
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
                )
            self.table.setItem(row, col, item)
        self.table.setVisible(True)
        self.empty.setVisible(False)

    def show_empty(self, title: str, hint: str = "", action: str = "") -> None:
        self.table.setVisible(False)
        self.empty.configure(title, hint, action)
        self.empty.setVisible(True)

    def restyle(self) -> None:
        self.empty.restyle()
        self.table.update()
        self.table.horizontalHeader().update()


class Toast(Surface):
    """Thông báo nổi góc phải dưới, tự ẩn sau vài giây."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent, bg_key="surface", border_key="border_strong",
            radius=RADIUS_TILE, shadow=True,
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 9, 14, 9)
        layout.setSpacing(10)
        self.dot = QLabel("●")
        self.dot.setFont(theme.font(10))
        layout.addWidget(self.dot, 0, Qt.AlignmentFlag.AlignVCenter)
        self.label = QLabel("")
        self.label.setFont(theme.font(9))
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self.setMaximumWidth(420)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(3200)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text: str, state: str = "ok") -> None:
        color = {
            "ok": "success",
            "warn": "warning",
            "err": "danger",
            "info": "primary",
        }.get(state, "primary")
        tokens = theme.current_tokens()
        self.dot.setStyleSheet(f"color: {tokens[color]};")
        self.label.setStyleSheet(f"color: {tokens['text']};")
        self.label.setText(text)
        self.show()
        self.raise_()
        self.reposition()
        self._timer.start()

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        margin = 24
        x = parent.width() - self.width() - margin
        # Nằm trên status bar cao 30px.
        y = parent.height() - 30 - 12 - self.height()
        self.move(max(margin, x), max(margin, y))


def make_button(
    text: str,
    variant: str = "",
    tooltip: str = "",
    *,
    minimum_height: int = 36,
) -> ThemedButton:
    """Nút với biến thể `primary` / `danger` / `subtle` / `ghost` / `tertiary`
    (rỗng = outline)."""
    return ThemedButton(text, variant, tooltip, minimum_height=minimum_height)


def restyle_tree(root: QWidget) -> None:
    """Gọi `restyle()` cho mọi component tự vẽ sau khi đổi theme."""
    for kind in (
        Surface,
        StatTile,
        ActionTile,
        ActivityFeed,
        DropZone,
        CardHeader,
        EmptyState,
        ErrorBanner,
        DataTable,
    ):
        for widget in root.findChildren(kind):
            widget.restyle()
    root.update()


__all__ = [
    "ActionTile",
    "ActivityFeed",
    "BrandMark",
    "Card",
    "CardHeader",
    "ChevronComboBox",
    "DataTable",
    "DropZone",
    "EmptyState",
    "ErrorBanner",
    "Field",
    "FilePicker",
    "IconGlyph",
    "NumberInput",
    "ShimmerProgressBar",
    "StatTile",
    "StatusPill",
    "Surface",
    "TableHeader",
    "ThemedButton",
    "Toast",
    "make_button",
    "restyle_tree",
]
