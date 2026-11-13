"""GUI TFTP viết bằng PySide6 (Qt).

Chạy::

    python -m tftp.tftp_gui
"""

from __future__ import annotations

import argparse
import os
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from . import enable_utf8_console
from .gui import theme
from .gui.main_window import MainWindow


def build_application(argv: list[str] | None = None, *,
                      start_server: bool = False) -> tuple[QApplication, MainWindow]:
    """Tạo QApplication + cửa sổ chính (tách riêng để test được).

    Thứ tự: dựng widget trước, đổi theme sau — để mọi widget nhận palette
    ngay từ đầu thay vì phải được đổi màu hai lần.
    """
    # Biến môi trường này (nếu máy người dùng đặt) sẽ ép style native
    # (windowsvista) khiến input vẽ xám lệch khỏi card; app luôn dùng Fusion.
    os.environ.pop("QT_STYLE_OVERRIDE", None)
    app = QApplication(argv if argv is not None else sys.argv)
    # Fusion: nền vẽ giống nhau trên mọi nền tảng, dễ tô màu nhất quán.
    app.setStyle("Fusion")
    theme.apply_fonts(app)
    theme.apply_theme(app, "light")
    window = MainWindow()
    if start_server:
        # The web gateway connects through loopback. Keep raw unauthenticated
        # TFTP off the LAN when the launcher starts it for browser users.
        window.host_edit.setText("127.0.0.1")
        QTimer.singleShot(0, window._start_server)
    return app, window


def main(argv: list[str] | None = None) -> int:
    enable_utf8_console()
    parser = argparse.ArgumentParser(description="Giao diện TFTP Console")
    parser.add_argument("--start-server", action="store_true",
                        help="Tự khởi động TFTP qua loopback để phục vụ web LAN")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])
    app, window = build_application([sys.argv[0], *qt_args], start_server=args.start_server)
    window.show()
    window.play_intro()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
