"""Thiết lập logging có rotation cho server/client CLI."""

from __future__ import annotations

import logging
import logging.handlers
import sys

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
DEFAULT_BACKUPS = 5


def setup_logging(
    *,
    log_file: str | None = None,
    verbose: bool = False,
    max_bytes: int = DEFAULT_MAX_BYTES,
    backups: int = DEFAULT_BACKUPS,
    stream=sys.stderr,
) -> list[logging.Handler]:
    """Cấu hình root logger; trả về handler đã gắn để test có thể dọn."""
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S"
    )
    handlers: list[logging.Handler] = []
    if log_file:
        handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max(0, max_bytes),
            backupCount=max(0, backups),
            encoding="utf-8",
        )
        handler.setFormatter(formatter)
        handlers.append(handler)
    stream_handler = logging.StreamHandler(stream)
    stream_handler.setFormatter(formatter)
    handlers.append(stream_handler)
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
        try:
            old.close()
        except OSError:
            pass
    for active_handler in handlers:
        root.addHandler(active_handler)
    root.setLevel(level)
    return handlers
