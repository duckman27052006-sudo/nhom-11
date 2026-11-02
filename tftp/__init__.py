"""Triển khai TFTP (RFC 1350) kèm TFTP Option Extension (RFC 2347).

Gói tin dùng thư viện chuẩn của Python: socket, struct, threading, logging.
"""

from __future__ import annotations

import sys

__version__ = "1.0.0"

DEFAULT_PORT = 6969
WELL_KNOWN_PORT = 69

__all__ = ["DEFAULT_PORT", "WELL_KNOWN_PORT", "enable_utf8_console", "__version__"]

_console_streams: tuple[int, int] | None = None


def enable_utf8_console() -> None:
    """Cho phép in thông điệp tiếng Việt ra console Windows (mặc định cp1252).

    Idempotent trên cùng một cặp stream: gọi nhiều lần (CLI, ``setup_logging``,
    GUI) chỉ cấu hình một lần. Nếu ``sys.stdout``/``sys.stderr`` bị thay bằng
    object khác (pytest capture, redirect) thì lần gọi sau sẽ cấu hình lại —
    nếu chỉ dùng một cờ boolean, lần chạy đầu trong test sẽ "khoá" luôn và
    console thật không bao giờ được sửa.

    Thư viện thuần không tự gọi hàm này khi import (tránh side effect toàn cục);
    ai dùng như thư viện và cần in tiếng Việt trên console Windows thì gọi một
    lần sau khi import.
    """
    global _console_streams
    current = (id(sys.stdout), id(sys.stderr))
    if _console_streams == current:
        return
    _console_streams = current
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
