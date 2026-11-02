"""Smoke test GUI (offscreen): đổi theme, lọc log, bind lỗi, đóng cửa sổ.

Chỉ chạy khi có PySide6; CI không cài extras [gui] nên module tự skip.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - thiếu extras [gui]
    raise unittest.SkipTest("cần PySide6 cho smoke test GUI") from None

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tftp.gui.main_window import MainWindow  # noqa: E402


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

    def test_bind_failure_leaves_gui_retryable(self) -> None:
        self.window.host_edit.setText("203.0.113.1")
        self.window._start_server()
        pump(self.app, 1.5)
        self.assertIsNone(self.window.server)
        self.assertIsNone(self.window.server_thread)
        self.assertTrue(self.window.hero_start.isVisible())
        self.assertIn("không chạy được", self.window.status_bar.message.text())

    def test_file_table_hides_names_the_server_cannot_serve(self) -> None:
        # GUI từng liệt kê cả file có dấu tiếng Việt và file ẩn; bấm vào chỉ
        # nhận lỗi vì server/client không phục vụ được những tên đó.
        with tempfile.TemporaryDirectory() as root:
            Path(root, "ok.txt").write_text("x", encoding="utf-8")
            Path(root, "báo-cáo.pdf").write_bytes(b"x")
            Path(root, ".hidden").write_text("x", encoding="utf-8")
            Path(root, "CON").write_text("x", encoding="utf-8")
            self.window.root_edit.setText(root)
            self.window._refresh_files(quiet=True)
            pump(self.app)
            table = self.window.files_table.table
            names = [table.item(row, 0).text() for row in range(table.rowCount())]
            self.assertEqual(names, ["ok.txt"])
            self.assertIn("bỏ qua 3", self.window.files_hint.text())

    def test_start_and_stop_server_without_blocking(self) -> None:
        # `_stop_server` phải chỉ đánh thức listener rồi trả về ngay; việc
        # shutdown/join diễn ra trong ServerThread và kết thúc bằng tín hiệu.
        with tempfile.TemporaryDirectory() as root:
            Path(root, "a.txt").write_text("x", encoding="utf-8")
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()

            self.window.host_edit.setText("127.0.0.1")
            self.window.port_spin.setValue(port)
            self.window.root_edit.setText(root)
            self.window._start_server()
            pump(self.app, 1.0)
            self.assertIsNotNone(self.window.server)
            # Form cấu hình phải khoá lại khi server đang chạy (đổi giá trị
            # không có tác dụng gì cho tới lần khởi động sau).
            self.assertFalse(self.window.root_edit.isEnabled())
            self.assertFalse(self.window.port_spin.isEnabled())

            self.window._stop_server()
            deadline = time.monotonic() + 5.0
            while self.window.server is not None and time.monotonic() < deadline:
                pump(self.app, 0.02)
            self.assertIsNone(self.window.server)
            self.assertIsNone(self.window.server_thread)
            self.assertIn("đã dừng", self.window.status_bar.message.text())
            self.assertTrue(self.window.root_edit.isEnabled())

    def test_long_log_line_is_elided_and_does_not_widen_page(self) -> None:
        # QLabel không word-wrap có minimumSizeHint bằng cả chuỗi: một dòng log
        # dài từng đẩy trang Tổng quan rộng hơn cửa sổ và cắt mất phần bên phải.
        long_line = "L" * 500
        self.window._on_log("INFO", long_line)
        pump(self.app, 0.1)
        label = next(
            item for item in self.window.activity._labels if item.full_text() == long_line
        )
        self.assertTrue(label.text().endswith("…"), label.text())
        self.assertEqual(label.toolTip(), long_line)
        # Không có ElidedLabel, QLabel đòi ~3000 px cho 500 ký tự và cả trang
        # rộng ra ngoài cửa sổ; có nó thì nhãn chỉ đòi đúng bề rộng đang có.
        self.assertLess(label.minimumSizeHint().width(), self.window.width())

    def test_launcher_autostart_allows_web_upload_without_clicking_start(self) -> None:
        try:
            from tftp.tftp_gui import build_application
            from tftp.web_upload import create_app
        except ModuleNotFoundError:
            self.skipTest("cần extras web cho luồng GUI -> web -> TFTP")
        from tests.test_web_upload import authenticated_client

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "files"
            root.mkdir()
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            self.window.root_edit.setText(str(root))
            self.window.port_spin.setValue(port)
            with (mock.patch("tftp.tftp_gui.QApplication", return_value=self.app),
                  mock.patch("tftp.tftp_gui.MainWindow", return_value=self.window)):
                build_application(["tftp-gui"], start_server=True)
            try:
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    pump(self.app, 0.02)
                    if self.window.server and self.window.server.bound_port:
                        break
                self.assertIsNotNone(self.window.server)
                self.assertEqual(self.window.server.bound_port, port)
                self.assertEqual(self.window.host_edit.text(), "127.0.0.1")
                web = create_app({"FILE_ROOT": root, "DATA_DIR": Path(directory) / "web-data",
                                  "TFTP_PORT": port, "TESTING": True})
                client, csrf = authenticated_client(web)
                import io

                payload = b"uploaded through autostarted GUI server"
                response = client.post(
                    "/api/files", data={"file": (io.BytesIO(payload), "from-web.txt")},
                    base_url="http://upload.lan",
                    headers={"X-CSRF-Token": csrf, "Origin": "http://upload.lan"})
                self.assertEqual(response.status_code, 201, response.json)
                self.assertEqual((root / "from-web.txt").read_bytes(), payload)
                with client.get("/api/files/from-web.txt", base_url="http://upload.lan") as result:
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(result.data, payload)
            finally:
                self.window._stop_server()
                deadline = time.monotonic() + 5.0
                while self.window.server is not None and time.monotonic() < deadline:
                    pump(self.app, 0.02)


if __name__ == "__main__":
    unittest.main()
