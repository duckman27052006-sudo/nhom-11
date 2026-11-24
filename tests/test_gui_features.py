"""Test tính năng GUI mới: QSettings, hủy phiên, hàng đợi, accessibility.

Toàn bộ chạy offscreen, không mở socket thật: client truyền được thay bằng
đối tượng chạy trong worker thread nhưng chỉ chờ/cancel bằng Event.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QSettings, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
except ImportError:  # pragma: no cover - thiếu extras [gui]
    raise unittest.SkipTest("cần PySide6 cho test tính năng GUI") from None

from tftp.gui import theme  # noqa: E402
from tftp.gui.main_window import PAGE_TRANSFER, MainWindow  # noqa: E402
from tftp.tftp_errors import TftpCancelled  # noqa: E402


def pump(app: QApplication, seconds: float = 0.0) -> None:
    deadline = time.monotonic() + seconds
    while True:
        app.processEvents()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.01)


def wait_until(app: QApplication, predicate, timeout: float = 3.0) -> bool:
    """Pump event loop tới khi điều kiện đúng; không sleep mù cả test."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    app.processEvents()
    return bool(predicate())


class ControlledClient:
    """Client giả: giữ worker sống tới khi test release hoặc cancel."""

    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.started = threading.Event()
        self.release = threading.Event()

    def download(self, remote: str, local: str, *, progress=None) -> str:
        return self._run(local, progress)

    def upload(self, local: str, remote: str, *, progress=None) -> str:
        return self._run(remote, progress)

    def _run(self, result: str, progress) -> str:
        self.started.set()
        if progress is not None:
            progress(0, 100)
        while not self.release.is_set():
            if self.cancel_event.is_set():
                raise TftpCancelled()
            time.sleep(0.005)
        if progress is not None:
            progress(100, 100)
        return result


class GuiFeaturesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings_path = Path(self._tmp.name) / "gui.ini"
        self.window = self._make_window()
        self.window.show()
        pump(self.app, 0.05)

    def tearDown(self) -> None:
        self.window.close()
        pump(self.app, 0.05)
        theme.apply_theme(self.app, "light")
        self._tmp.cleanup()

    def _make_window(self) -> MainWindow:
        settings = QSettings(str(self.settings_path), QSettings.Format.IniFormat)
        return MainWindow(settings)

    # ------------------------------------------------------------- settings ---
    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            save_path = Path(root) / "saved.bin"
            upload_path = Path(root) / "upload.bin"
            self.window.host_edit.setText("192.0.2.10")
            self.window.port_spin.setValue(7788)
            self.window.target_edit.setText("192.0.2.10:9999")
            self.window.root_edit.setText(root)
            self.window.blksize_spin.setValue(1024)
            self.window.download_blksize.setValue(512)
            self.window.upload_blksize.setValue(512)
            self.window.download_timeout.setValue(7)
            self.window.upload_timeout.setValue(7)
            self.window.download_retries.setValue(3)
            self.window.upload_retries.setValue(3)
            self.window.download_mode.setCurrentText("netascii")
            self.window.upload_mode.setCurrentText("netascii")
            self.window.download_save.setText(str(save_path))
            self.window.upload_pick.setText(str(upload_path))
            self.window._set_theme("dark")
            # Offscreen screen chỉ 800x800; rộng 1000 (= min width) không bị
            # restoreGeometry kẹp lại, nên so kích thước được ổn định.
            self.window.resize(1000, 700)
            pump(self.app, 0.05)
            saved_size = self.window.size()
            self.window._save_settings()
            self.window.close()
            pump(self.app, 0.05)

            second = self._make_window()
            self.window = second
            second.show()
            pump(self.app, 0.05)

        self.assertEqual(second.host_edit.text(), "192.0.2.10")
        self.assertEqual(second.port_spin.value(), 7788)
        self.assertEqual(second.target_edit.text(), "192.0.2.10:9999")
        self.assertEqual(second.root_edit.text(), root)
        self.assertEqual(second.blksize_spin.value(), 1024)
        self.assertEqual(second.download_blksize.value(), 512)
        self.assertEqual(second.upload_blksize.value(), 512)
        self.assertEqual(second.download_timeout.value(), 7)
        self.assertEqual(second.upload_timeout.value(), 7)
        self.assertEqual(second.download_retries.value(), 3)
        self.assertEqual(second.upload_retries.value(), 3)
        self.assertEqual(second.download_mode.currentText(), "netascii")
        self.assertEqual(second.upload_mode.currentText(), "netascii")
        self.assertEqual(second.download_save.text(), str(save_path))
        self.assertEqual(second.upload_pick.text(), str(upload_path))
        self.assertEqual(second.theme_name, "dark")
        self.assertLessEqual(abs(second.width() - saved_size.width()), 5)
        self.assertLessEqual(abs(second.height() - saved_size.height()), 5)

    # ------------------------------------------------------------- hàng đợi ---
    def test_second_transfer_is_queued_fifo_not_refused(self) -> None:
        first = ControlledClient()
        second = ControlledClient()
        clients = [first, second]

        def factory(*_args, **_kwargs):
            return clients.pop(0)

        with tempfile.TemporaryDirectory() as root:
            upload_file = Path(root) / "upload.bin"
            upload_file.write_bytes(b"payload")
            with mock.patch("tftp.gui.main_window.TftpClient", side_effect=factory):
                self.window.target_edit.setText("127.0.0.1:6969")
                self.window.download_name.setText("a.bin")
                self.window.download_save.setText(str(Path(root) / "a.bin"))
                self.window._start_download()
                self.assertTrue(wait_until(self.app, first.started.is_set))
                self.assertIsNotNone(self.window.transfer_thread)

                # Bấm Gửi lên khi đang tải: phải vào hàng đợi, không bị từ chối.
                self.window.upload_pick.setText(str(upload_file))
                self.window.upload_remote.setText("upload.bin")
                self.window._start_upload()
                self.assertEqual(len(self.window._transfer_queue), 1)
                self.assertFalse(second.started.is_set())
                self.assertIn("1 việc chờ", self.window.queue_status.text())
                self.assertTrue(self.window.cancel_button.isEnabled())
                self.assertTrue(self.window.queue_clear_button.isEnabled())

                first.release.set()
                self.assertTrue(wait_until(self.app, second.started.is_set))
                self.assertEqual(len(self.window._transfer_queue), 0)
                # Phiên kế tiếp là upload đúng thứ tự FIFO.
                self.assertEqual(self.window._active_job.direction, "upload")

                second.release.set()
                self.assertTrue(
                    wait_until(self.app, lambda: self.window.transfer_thread is None)
                )
        self.assertIn("hàng đợi trống", self.window.queue_status.text())
        self.assertFalse(self.window.cancel_button.isEnabled())
        self.assertFalse(self.window.queue_clear_button.isEnabled())

    # ---------------------------------------------------------------- hủy ---
    def _assert_cancel_is_neutral(self, direction: str) -> None:
        client = ControlledClient()
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "upload.bin"
            source.write_bytes(b"payload")
            with mock.patch(
                "tftp.gui.main_window.TftpClient", return_value=client
            ):
                self.window.target_edit.setText("127.0.0.1:6969")
                if direction == "download":
                    self.window.download_name.setText("a.bin")
                    self.window.download_save.setText(str(Path(root) / "a.bin"))
                    self.window._start_download()
                    status = self.window.download_status
                    banner = self.window.download_banner
                else:
                    self.window.upload_pick.setText(str(source))
                    self.window.upload_remote.setText("upload.bin")
                    self.window._start_upload()
                    status = self.window.upload_status
                    banner = self.window.upload_banner
                self.assertTrue(wait_until(self.app, client.started.is_set))
                self.assertTrue(self.window.cancel_button.isEnabled())
                self.window.cancel_button.click()
                self.assertTrue(client.cancel_event.is_set())
                worker = self.window.transfer_worker
                self.assertIsNotNone(worker)
                self.assertTrue(worker.cancel_event.is_set())
                self.assertTrue(
                    wait_until(self.app, lambda: self.window.transfer_thread is None)
                )
        self.assertIn("Đã hủy", status.text())
        self.assertFalse(banner.isVisible())
        self.assertNotIn("thất bại", status.text())
        self.assertNotIn("Traceback", status.text())
        self.assertIn("Đã hủy", self.window.status_bar.message.text())
        self.assertIn("Đã hủy", self.window.toast.label.text())
        # GUI vẫn dùng được sau khi hủy.
        self.assertTrue(self.window.download_button.isEnabled())
        self.assertTrue(self.window.upload_button.isEnabled())

    def test_cancel_download_is_neutral(self) -> None:
        self._assert_cancel_is_neutral("download")

    def test_cancel_upload_is_neutral(self) -> None:
        self._assert_cancel_is_neutral("upload")

    def test_clear_queue_keeps_current_transfer(self) -> None:
        first = ControlledClient()
        second = ControlledClient()
        clients = [first, second]

        def factory(*_args, **_kwargs):
            return clients.pop(0)

        with tempfile.TemporaryDirectory() as root:
            upload_file = Path(root) / "upload.bin"
            upload_file.write_bytes(b"payload")
            with mock.patch("tftp.gui.main_window.TftpClient", side_effect=factory):
                self.window.target_edit.setText("127.0.0.1:6969")
                self.window.download_name.setText("a.bin")
                self.window.download_save.setText(str(Path(root) / "a.bin"))
                self.window._start_download()
                self.assertTrue(wait_until(self.app, first.started.is_set))
                self.window.upload_pick.setText(str(upload_file))
                self.window.upload_remote.setText("upload.bin")
                self.window._start_upload()
                self.assertEqual(len(self.window._transfer_queue), 1)

                self.window.queue_clear_button.click()
                self.assertEqual(len(self.window._transfer_queue), 0)
                self.assertTrue(self.window.transfer_thread is not None)
                self.assertFalse(second.started.is_set())

                first.release.set()
                self.assertTrue(
                    wait_until(self.app, lambda: self.window.transfer_thread is None)
                )
        self.assertFalse(second.started.is_set())

    # --------------------------------------------------------------- đóng ---
    def test_close_during_transfer_cancels_and_joins(self) -> None:
        client = ControlledClient()
        with tempfile.TemporaryDirectory() as root:
            with mock.patch(
                "tftp.gui.main_window.TftpClient", return_value=client
            ):
                self.window.target_edit.setText("127.0.0.1:6969")
                self.window.download_name.setText("a.bin")
                self.window.download_save.setText(str(Path(root) / "a.bin"))
                self.window._start_download()
                self.assertTrue(wait_until(self.app, client.started.is_set))
                started = time.monotonic()
                self.assertTrue(self.window.close())
                elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertTrue(client.cancel_event.is_set())
        self.assertIsNone(self.window.transfer_thread)
        self.assertFalse(self.window.isVisible())

    # -------------------------------------------------------- accessibility ---
    def test_keyboard_activation_and_accessible_names(self) -> None:
        self.assertEqual(QCoreApplication.organizationName(), "Nhom11")
        self.assertEqual(QCoreApplication.applicationName(), "TFTP Console")
        self.assertTrue(self.window.tile_download.accessibleName())
        self.assertTrue(self.window.tile_upload.accessibleName())
        self.assertEqual(
            self.window.tile_download.focusPolicy(), Qt.FocusPolicy.StrongFocus
        )
        self.assertEqual(
            self.window.upload_drop.focusPolicy(), Qt.FocusPolicy.StrongFocus
        )
        for widget in (
            self.window.download_button,
            self.window.upload_button,
            self.window.cancel_button,
            self.window.queue_clear_button,
            self.window.log_filter,
        ):
            self.assertTrue(widget.accessibleName(), type(widget).__name__)

        self.window.go_to(0)
        pump(self.app, 0.05)
        event = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.NoModifier
        )
        QApplication.sendEvent(self.window.tile_download, event)
        pump(self.app, 0.05)
        self.assertEqual(self.window.stack.currentIndex(), PAGE_TRANSFER)

        with mock.patch.object(type(self.window.upload_drop), "_choose_file") as choose:
            QApplication.sendEvent(
                self.window.upload_drop,
                QKeyEvent(
                    QEvent.Type.KeyPress,
                    Qt.Key.Key_Space,
                    Qt.KeyboardModifier.NoModifier,
                ),
            )
            choose.assert_called_once()


if __name__ == "__main__":
    unittest.main()
