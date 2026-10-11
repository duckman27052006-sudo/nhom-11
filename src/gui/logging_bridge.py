"""Nối log chuẩn của Python sang Qt.

`TftpServer`/`TftpClient` chạy ở thread riêng và chỉ biết `logging`. Handler
dưới đây bắt record rồi phát signal Qt; vì receiver nằm ở thread chính nên
Qt tự chuyển sang queued connection — an toàn khi log phát ra từ thread khác.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal


class LogBridge(QObject):
    """Phát tín hiệu `(mức, dòng log)` từ bất kỳ thread nào."""

    message = Signal(str, str)  # (level, "HH:MM:SS  message")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._handler: QtLogHandler | None = None

    def attach(
        self,
        min_level: int = logging.INFO,
        callback: Callable[[str, str], None] | None = None,
    ):
        """Gắn handler vào logging root, trả về handler để tháo ra sau."""
        if callback is not None:
            self.message.connect(callback)
        self._handler = QtLogHandler(self.message, min_level)
        logging.getLogger().addHandler(self._handler)
        logging.getLogger().setLevel(logging.DEBUG)
        return self._handler

    def detach(self) -> None:
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler.close()
            self._handler = None


class QtLogHandler(logging.Handler):
    """logging.Handler phát record qua Qt signal, chỉ giữ log của tftp.*."""

    def __init__(self, signal, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._signal = signal
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S"))
        self.addFilter(NameFilter())

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # noqa: BLE001 - handler không được phép ném lỗi
            self.handleError(record)
            return
        try:
            self._signal.emit(record.levelname, text)
        except RuntimeError:
            # App đã đóng trong lúc thread khác đang log.
            pass


class NameFilter(logging.Filter):
    """Giữ log từ `tftp.*`; bỏ qua log nhiễu của thư viện khác."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.startswith("tftp")
