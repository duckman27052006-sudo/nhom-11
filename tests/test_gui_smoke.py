"""Smoke test GUI (offscreen): đổi theme, lọc log, bind lỗi, đóng cửa sổ.

Chỉ chạy khi có PySide6; CI không cài extras [gui] nên module tự skip.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from PySide6.QtWidgets import QApplication, QLabel
except ImportError:  # pragma: no cover - thiếu extras [gui]
    raise unittest.SkipTest("cần PySide6 cho smoke test GUI") from None

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from src.gui.main_window import MainWindow  # noqa: E402


def pump(app: QApplication, seconds: float = 0.0) -> None:
    deadline = time.monotonic() + seconds
    while True:
        app.processEvents()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


class TestGuiSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()
        self.window.show()
        pump(self.app, 0.05)

    def tearDown(self) -> None:
        self.window.close()
        pump(self.app, 0.05)

    def _visible_blocks(self) -> int:
        block = self.window.log_view.document().begin()
        count = 0
        while block.isValid():
            if block.isVisible():
                count += 1
            block = block.next()
        return count

    def test_theme_toggle_keeps_labels_styled(self) -> None:
        self.window._toggle_theme()
        pump(self.app)
        self.assertEqual(self.window.theme_name, "dark")
        self.assertIn("color:", self.window.status_bar.message.styleSheet())
        self.window._toggle_theme()
        pump(self.app)
        self.assertEqual(self.window.theme_name, "light")

    def test_log_filter_uses_levels(self) -> None:
        self.window._on_log("DEBUG", "22:00:00 DEBUG   DATA 1")
        self.window._on_log("INFO", "22:00:01 INFO    xong")
        self.window._on_log("ERROR", "22:00:02 ERROR   hỏng")
        pump(self.app)
        all_visible = self._visible_blocks()
        self.window.log_filter.setCurrentIndex(2)
        pump(self.app)
        self.assertLess(self._visible_blocks(), all_visible)
        self.assertGreaterEqual(self._visible_blocks(), 1)
        self.window.log_filter.setCurrentIndex(0)
        pump(self.app)
        self.assertEqual(self._visible_blocks(), all_visible)

    def test_download_success_enables_open_folder(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "done.bin")
            Path(path).write_bytes(b"x")
            self.assertFalse(self.window.download_open.isEnabled())
            self.window._on_download_ok(path)
            pump(self.app)
            self.assertTrue(self.window.download_open.isEnabled())
            self.assertEqual(self.window._last_download_path, path)

    def test_copy_log_reports_success(self) -> None:
        self.window._on_log("INFO", "22:00:00 INFO    xin chao")
        pump(self.app)
        self.window._copy_log()
        pump(self.app)
        self.assertIn("Đã sao chép", self.window.status_bar.message.text())

    def test_shortcuts_dialog_lists_bindings(self) -> None:
        dialog = self.window._build_shortcuts_dialog()
        try:
            self.assertEqual(dialog.windowTitle(), "Phím tắt")
            texts = [label.text() for label in dialog.findChildren(QLabel)]
            self.assertIn("Ctrl+L", texts)
            self.assertIn("Ctrl+1 … Ctrl+4", texts)
        finally:
            dialog.close()

    def test_bind_failure_leaves_gui_retryable(self) -> None:
        self.window.host_edit.setText("203.0.113.1")
        self.window._start_server()
        pump(self.app, 1.5)
        self.assertIsNone(self.window.server)
        self.assertIsNone(self.window.server_thread)
        self.assertTrue(self.window.hero_start.isVisible())
        self.assertIn("không chạy được", self.window.status_bar.message.text())


if __name__ == "__main__":
    unittest.main()
