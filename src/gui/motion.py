"""Chuyển động cho GUI: trôi trang, đếm số, nhịp.

Tập trung toàn bộ animation vào một chỗ để phần còn lại của GUI chỉ gọi hàm,
không rải `QPropertyAnimation` khắp nơi.

Nguyên tắc:
  - Thời gian ngắn (220–500 ms) và easing OutCubic: cảm giác nhanh nhưng mềm.
  - Một thời điểm chỉ một lớp chuyển động để không rung giật.
  - Mọi animation tự huỷ khi widget bị đóng; không giữ tham chiếu vòng.
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QObject,
    QPoint,
    QPropertyAnimation,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QStackedWidget, QWidget

DUR_BASE = 220
DUR_DRIFT = 500
DRIFT_DY = 28


def out_cubic() -> QEasingCurve:
    return QEasingCurve(QEasingCurve.Type.OutCubic)


def in_out() -> QEasingCurve:
    return QEasingCurve(QEasingCurve.Type.InOutQuad)


class PageStack(QStackedWidget):
    """StackedWidget chuyển trang bằng cú trôi lên nhẹ.

    QStackedWidget mặc định đổi trang không hiệu ứng. Trượt ngang hai trang
    cùng lúc gây rung (repaint đè + layout giằng vị trí), nên ở đây trang cũ
    ẩn ngay, trang mới trôi từ dưới lên một mình — chậm, mềm, không giật.
    """

    changed = Signal(int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._anims: list[QPropertyAnimation] = []
        self._leaving: QWidget | None = None

    def drift_to(self, index: int) -> None:
        """Chuyển sang trang `index`: trang mới trôi từ dưới lên."""
        if index == self.currentIndex() or not 0 <= index < self.count():
            return
        self._settle()

        leaving = self.currentWidget()
        # Tắt vẽ trong lúc định vị để không chớp 1 frame rồi mới chạy.
        self.setUpdatesEnabled(False)
        self.setCurrentIndex(index)
        arriving = self.currentWidget()
        if leaving is not None and leaving is not arriving:
            leaving.setVisible(False)
            leaving.move(0, 0)
        self._leaving = None
        arriving.move(0, DRIFT_DY)
        self.setUpdatesEnabled(True)

        drift = QPropertyAnimation(arriving, b"pos", self)
        drift.setDuration(DUR_DRIFT)
        drift.setStartValue(QPoint(0, DRIFT_DY))
        drift.setEndValue(QPoint(0, 0))
        drift.setEasingCurve(out_cubic())
        drift.finished.connect(self._settle)

        self._anims = [drift]
        drift.start()
        self.changed.emit(index)

    def _settle(self) -> None:
        if self._leaving is not None:
            self._leaving.setVisible(False)
            self._leaving.move(0, 0)
            self._leaving = None
        # Animation bị ngắt giữa chừng (resize) có thể để trang hiện lệch.
        current = self.currentWidget()
        if current is not None:
            current.move(0, 0)
        self._stop_anims()

    def _stop_anims(self) -> None:
        for anim in self._anims:
            anim.stop()
        self._anims.clear()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - tên do Qt đặt
        self._settle()
        super().resizeEvent(event)


class Counter(QObject):
    """Đếm số tăng dần cho ô số liệu (dễ chú ý thay vì nhảy thẳng tới giá trị)."""

    def __init__(self, apply, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._apply = apply
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(700)
        self._anim.setEasingCurve(out_cubic())
        self._anim.valueChanged.connect(lambda value: self._apply(float(value)))
        self._current = 0.0

    def to(self, target: float) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._current)
        self._anim.setEndValue(float(target))
        self._anim.start()

    def stop(self) -> None:
        self._anim.stop()

    def set_immediate(self, target: float) -> None:
        self._anim.stop()
        self._current = float(target)
        self._apply(float(target))


class Pulse(QObject):
    """Vòng lặp 0→1→0, dùng cho chấm trạng thái và shimmer."""

    def __init__(self, apply, parent: QObject | None = None, duration: int = 1400) -> None:
        super().__init__(parent)
        self._apply = apply
        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setKeyValueAt(0.5, 1.0)
        self._anim.setEndValue(0.0)
        self._anim.setDuration(duration)
        self._anim.setLoopCount(-1)
        self._anim.setEasingCurve(in_out())
        self._anim.valueChanged.connect(lambda value: self._apply(float(value)))

    def start(self) -> None:
        self._anim.start()

    def stop(self) -> None:
        self._anim.stop()
        self._apply(0.0)


class Ticker(QObject):
    """Bộ đếm thời gian định kỳ (uptime, đồng hồ).

    Tự cộng thời gian **trước** khi phát tín hiệu, để handler chỉ cần đọc
    `seconds` — nếu để handler tự gọi lại thì sẽ đệ quy vô hạn.
    """

    tick = Signal()

    def __init__(self, interval_ms: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._on_timeout)
        self._elapsed_ms = 0

    def _on_timeout(self) -> None:
        self._elapsed_ms += self._timer.interval()
        self.tick.emit()

    def start(self) -> None:
        self._elapsed_ms = 0
        self._timer.start()
        self.tick.emit()

    def stop(self) -> None:
        self._timer.stop()

    @property
    def seconds(self) -> float:
        return self._elapsed_ms / 1000.0


def human_clock(seconds: float) -> str:
    """Đồng hồ từ giây: 01:23 hoặc 1:02:03."""
    seconds = int(max(0, seconds))
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"
