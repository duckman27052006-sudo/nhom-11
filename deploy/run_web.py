#!/usr/bin/env python3
"""Dựng tham số cho web gateway từ biến môi trường (dùng trong Docker Compose)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tftp import enable_utf8_console  # noqa: E402
from tftp.web_upload import main  # noqa: E402


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def build_args() -> list[str]:
    host = env("WEB_HOST")
    if not host:
        raise ValueError("Thiếu WEB_HOST (IP private của máy chủ trong LAN/VPN).")
    networks = [item.strip() for item in env("ALLOWED_NETWORKS").split(",") if item.strip()]
    if not networks:
        raise ValueError("Thiếu ALLOWED_NETWORKS (các subnet trụ sở, cách nhau bằng dấu phẩy).")
    args = [
        "--host", host,
        "--port", env("WEB_PORT", "8080"),
        "--root", env("ROOT", "/srv/tftp/files"),
        "--data-dir", env("DATA_DIR", "/srv/tftp/web-data"),
        "--tftp-server", env("TFTP_ADDRESS", "127.0.0.1:6969"),
        "--threads", env("THREADS", "8"),
    ]
    for network in networks:
        args += ["--allowed-network", network]
    for proxy in env("TRUSTED_PROXIES").split(","):
        if proxy.strip():
            args += ["--trusted-proxy", proxy.strip()]
    if env("SECURE_COOKIE").lower() in {"1", "true", "yes"}:
        args.append("--secure-cookie")
    if env("ADMIN_USER"):
        args += ["--admin", env("ADMIN_USER")]
    return args


def run() -> int:
    enable_utf8_console()
    try:
        return main(build_args())
    except ValueError as exc:
        print(f"[run_web] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(run())
