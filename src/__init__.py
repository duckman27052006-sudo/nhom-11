"""Triển khai TFTP (RFC 1350) kèm TFTP Option Extension (RFC 2347).

Gói tin dùng thư viện chuẩn của Python: socket, struct, threading, logging.
"""

from __future__ import annotations

import sys

__version__ = "1.0.0"

DEFAULT_PORT = 6969
WELL_KNOWN_PORT = 69

__all__ = ["DEFAULT_PORT", "WELL_KNOWN_PORT", "enable_utf8_console", "__version__"]


def enable_utf8_console() -> None:
    """Cho phép in thông điệp tiếng Việt ra console Windows (mặc định cp1252)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
