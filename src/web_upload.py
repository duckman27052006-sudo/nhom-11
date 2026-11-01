"""Cổng web upload file qua TFTP cho mạng LAN (tuỳ chọn, cần extras ``[web]``).

Trình duyệt gửi HTTP multipart tới cổng này; cổng ghi file tạm rồi dùng
:class:`~src.tftp_client.TftpClient` gửi WRQ tới TFTP server. File cuối cùng
nằm trong thư mục gốc của TFTP server, không nằm trong thư mục của web app.

Chạy::

    python -m src.web_upload --host 192.168.1.10 --port 8080 \
        --allowed-network 192.168.1.0/24 --tftp-server 127.0.0.1:6969

Thiết kế bảo mật (fail-closed):

- Chỉ bind vào một địa chỉ private/loopback cụ thể; wildcard và IP public bị từ chối.
- Phải có ít nhất một ``--allowed-network`` private/loopback; ACL dựa trên peer IP
  thật (không đọc ``X-Forwarded-For``).
- Kiểm tra ``Origin`` và CSRF token cho mọi request ghi.
- Giới hạn kích thước request/file, thêm CSP + security headers, không bật CORS.
- Không có route liệt kê hay tải file xuống.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sys
import tempfile

from flask import Flask, Response, g, render_template_string, request, session

from . import __version__, enable_utf8_console
from .tftp_client import TftpClient, _parse_server
from .tftp_errors import TftpError

LOG = logging.getLogger("tftp.web")

Network = ipaddress.IPv4Network | ipaddress.IPv6Network

DEFAULT_HTTP_PORT = 8080
DEFAULT_MAX_FILE_MIB = 100
DEFAULT_TFTP_BLOCKSIZE = 1468
MULTIPART_OVERHEAD = 64 * 1024
"""Dung lượng dư cho header multipart khi đặt ``MAX_CONTENT_LENGTH``."""

_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

PAGE_TEMPLATE = """<!doctype html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="csrf-token" content="{{ csrf }}">
<title>Gửi file · TFTP</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #f4f6f4; --panel: #ffffff; --ink: #16211e; --muted: #5b6a65;
  --line: #dde4e0; --accent: #1f6f5c; --accent-ink: #ffffff; --danger: #a4372c;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1513; --panel: #17211f; --ink: #e9efec; --muted: #9aa8a2;
    --line: #2a3733; --accent: #5ad1ab; --accent-ink: #0f1513; --danger: #ff8f80;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: grid; place-items: center;
  background: var(--bg); color: var(--ink); padding: 24px;
  font: 16px/1.55 ui-sans-serif, system-ui, "Segoe UI", sans-serif;
}
main {
  width: min(560px, 100%); background: var(--panel); border: 1px solid var(--line);
  border-radius: 18px; padding: 30px;
}
.eyebrow {
  margin: 0 0 10px; font-size: 12px; letter-spacing: .14em;
  text-transform: uppercase; color: var(--muted);
}
h1 { margin: 0 0 10px; font-size: 26px; line-height: 1.2; letter-spacing: -.01em; }
.muted { color: var(--muted); margin: 0 0 22px; }
label { display: block; font-weight: 600; margin-bottom: 8px; }
input[type=file] {
  width: 100%; padding: 12px; border: 1px dashed var(--line); border-radius: 12px;
  background: transparent; color: inherit;
}
button {
  margin-top: 16px; width: 100%; padding: 12px 16px; border: 0; border-radius: 12px;
  background: var(--accent); color: var(--accent-ink); font: inherit; font-weight: 650;
  cursor: pointer;
}
button:disabled { opacity: .6; cursor: progress; }
#status { min-height: 1.5em; margin: 16px 0 0; color: var(--muted); }
#status.error { color: var(--danger); }
.small { font-size: 13px; margin-top: 18px; }
</style>
</head>
<body>
<main>
  <p class="eyebrow">Cổng gửi file trong LAN</p>
  <h1>Gửi file tới máy chủ TFTP</h1>
  <p class="muted">File được chuyển thẳng tới thư mục gốc của TFTP server,
  không lưu trên trang này.</p>
  <form id="upload-form">
    <label for="file">Chọn file cần gửi</label>
    <input id="file" name="file" type="file" required>
    <button type="submit" id="submit">Gửi file</button>
  </form>
  <p id="status" role="status" aria-live="polite"></p>
  <p class="muted small">Tối đa {{ max_mib }} MiB mỗi file. Chỉ dùng trong mạng
  nội bộ tin cậy — kết nối HTTP này không mã hoá.</p>
</main>
<script nonce="{{ nonce }}">
const form = document.getElementById("upload-form");
const input = document.getElementById("file");
const button = document.getElementById("submit");
const status = document.getElementById("status");
const token = document.querySelector('meta[name="csrf-token"]').content;

function report(message, isError) {
  status.textContent = message;
  status.classList.toggle("error", Boolean(isError));
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!input.files.length) return;
  const body = new FormData();
  body.append("file", input.files[0]);
  button.disabled = true;
  report("Đang gửi…", false);
  try {
    const response = await fetch("/upload", {
      method: "POST",
      body,
      headers: { "X-CSRF-Token": token },
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || ("HTTP " + response.status));
    report("Đã gửi '" + data.filename + "' (" + data.bytes + " byte).", false);
    form.reset();
  } catch (error) {
    report("Gửi thất bại: " + error.message, true);
  } finally {
    button.disabled = false;
  }
});
</script>
</body>
</html>
"""


# ------------------------------------------------------------------ tiện ích ---
def validate_host(value: str) -> str:
    """Chỉ chấp nhận địa chỉ private/loopback cụ thể (fail-closed)."""
    text = (value or "").strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise ValueError("--host phải là địa chỉ IP cụ thể (không dùng hostname)") from exc
    if address.is_unspecified:
        raise ValueError("--host không được là wildcard 0.0.0.0/::")
    if not (address.is_private or address.is_loopback):
        raise ValueError("--host phải là địa chỉ private/loopback")
    return str(address)


def parse_networks(values: list[str] | None) -> list[Network]:
    """Chuyển các CIDR thành network; bắt buộc ít nhất một mạng private/loopback."""
    if not values:
        raise ValueError("cần ít nhất một --allowed-network private/loopback")
    networks: list[Network] = []
    for raw in values:
        text = (raw or "").strip()
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError as exc:
            raise ValueError(f"--allowed-network không hợp lệ: {raw!r}") from exc
        if not (network.is_private or network.is_loopback):
            raise ValueError(f"--allowed-network phải là private/loopback: {raw!r}")
        networks.append(network)
    return networks


def safe_remote_name(raw: str) -> str | None:
    """Làm sạch tên file do trình duyệt gửi trước khi đưa vào WRQ."""
    if not raw:
        return None
    name = raw.replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ord(ch) >= 32 and ch != "\x7f")
    name = "".join(ch for ch in name if ch not in '<>:"|?*')
    name = name.strip().strip(".")
    if not name or name in (".", ".."):
        return None
    if len(name.encode("utf-8", "replace")) > 255:
        return None
    if name.split(".")[0].lower() in _WINDOWS_RESERVED:
        return None
    return name


def _peer_allowed(peer: str | None, networks: tuple) -> bool:
    if not peer:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    # Khi bind IPv6, peer IPv4 có thể hiện dạng ::ffff:a.b.c.d — quy về IPv4
    # để allowlist IPv4 vẫn khớp.
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


def _origin_allowed() -> bool:
    """True nếu request không có Origin (curl) hoặc Origin khớp host hiện tại."""
    origin = request.headers.get("Origin")
    if origin is None:
        return True
    return origin.rstrip("/") == request.host_url.rstrip("/")


def _json_response(payload: dict, status: int = 200) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        mimetype="application/json",
    )


def _json_error(status: int, message: str) -> Response:
    return _json_response({"ok": False, "error": message}, status=status)


# ---------------------------------------------------------------------- app ---
def create_app(
    *,
    allowed_networks: list[Network],
    tftp_host: str,
    tftp_port: int,
    max_file_bytes: int,
    tftp_blocksize: int = DEFAULT_TFTP_BLOCKSIZE,
    tftp_timeout: float = 5.0,
    tftp_retries: int = 5,
    secret_key: str | None = None,
) -> Flask:
    """Tạo Flask app. Tách khỏi CLI để test bằng ``app.test_client()``."""
    if max_file_bytes <= 0:
        raise ValueError("max_file_bytes phải > 0")

    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secret_key or secrets.token_hex(32),
        MAX_CONTENT_LENGTH=max_file_bytes + MULTIPART_OVERHEAD,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_SECURE=False,
        ALLOWED_NETWORKS=tuple(allowed_networks),
        MAX_FILE_BYTES=max_file_bytes,
        TFTP_HOST=tftp_host,
        TFTP_PORT=tftp_port,
        TFTP_BLOCKSIZE=tftp_blocksize,
        TFTP_TIMEOUT=tftp_timeout,
        TFTP_RETRIES=tftp_retries,
    )

    @app.before_request
    def _guard() -> Response | None:
        if not _peer_allowed(request.remote_addr, app.config["ALLOWED_NETWORKS"]):
            LOG.warning("từ chối %s: ngoài mạng cho phép", request.remote_addr)
            return _json_error(403, "địa chỉ của bạn không nằm trong mạng được phép")
        if request.method not in ("GET", "HEAD") and not _origin_allowed():
            LOG.warning("từ chối %s: Origin không khớp", request.remote_addr)
            return _json_error(403, "Origin không hợp lệ")
        return None

    @app.after_request
    def _security_headers(response: Response) -> Response:
        nonce = getattr(g, "csp_nonce", None)
        script_src = f"'nonce-{nonce}'" if nonce else "'none'"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; "
            f"script-src {script_src}; "
            "style-src 'unsafe-inline'; "
            "connect-src 'self'; "
            "img-src 'self' data:; "
            "form-action 'self'; "
            "base-uri 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers.pop("Access-Control-Allow-Origin", None)
        return response

    @app.errorhandler(413)
    def _too_large(_error) -> Response:
        limit_mib = max_file_bytes / (1024 * 1024)
        return _json_error(413, f"file vượt giới hạn {limit_mib:.0f} MiB")

    def _csrf_token() -> str:
        token = session.get("csrf")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf"] = token
        return token

    @app.get("/")
    def index() -> Response:
        nonce = secrets.token_urlsafe(16)
        g.csp_nonce = nonce
        html = render_template_string(
            PAGE_TEMPLATE,
            csrf=_csrf_token(),
            nonce=nonce,
            max_mib=max_file_bytes // (1024 * 1024),
        )
        return Response(html, mimetype="text/html; charset=utf-8")

    @app.post("/upload")
    def upload() -> Response:
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        expected = session.get("csrf") or ""
        if not expected or not hmac.compare_digest(token, expected):
            return _json_error(403, "CSRF token không hợp lệ")

        incoming = request.files.get("file")
        if incoming is None or not incoming.filename:
            return _json_error(400, "thiếu file trong form")
        name = safe_remote_name(incoming.filename)
        if name is None:
            return _json_error(400, "tên file không hợp lệ")

        handle = tempfile.NamedTemporaryFile(
            prefix="tftp-web-", suffix=".part", delete=False
        )
        temp_path = handle.name
        try:
            with handle:
                incoming.save(handle)
            size = os.path.getsize(temp_path)
            if size > max_file_bytes:
                return _json_error(
                    413, f"file vượt giới hạn {max_file_bytes // (1024 * 1024)} MiB"
                )
            client = TftpClient(
                app.config["TFTP_HOST"],
                app.config["TFTP_PORT"],
                timeout=app.config["TFTP_TIMEOUT"],
                retries=app.config["TFTP_RETRIES"],
                blksize=app.config["TFTP_BLOCKSIZE"],
            )
            client.upload(temp_path, name)
        except TftpError as exc:
            LOG.warning("gửi '%s' (%d byte) thất bại: %s", name, size, exc)
            return _json_error(502, f"TFTP server từ chối: {exc}")
        except OSError as exc:
            LOG.error("lỗi hệ thống khi gửi '%s': %s", name, exc)
            return _json_error(500, "lỗi hệ thống khi gửi file")
        finally:
            try:
                os.unlink(temp_path)
            except OSError:
                pass

        LOG.info(
            "đã gửi '%s' (%d byte) từ %s tới TFTP %s:%s",
            name,
            size,
            request.remote_addr,
            app.config["TFTP_HOST"],
            app.config["TFTP_PORT"],
        )
        return _json_response({"ok": True, "filename": name, "bytes": size})

    return app


# ---------------------------------------------------------------------- CLI ---
def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.web_upload",
        description="Cổng web upload file qua TFTP cho mạng LAN (HTTP, không TLS).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="địa chỉ IP private/loopback để bind (mặc định 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_HTTP_PORT, help="cổng HTTP (mặc định 8080)"
    )
    parser.add_argument(
        "--allowed-network",
        action="append",
        default=[],
        metavar="CIDR",
        help="mạng private/loopback được phép upload (lặp lại được, bắt buộc)",
    )
    parser.add_argument(
        "--tftp-server",
        default="127.0.0.1:6969",
        help="địa chỉ TFTP server host[:port] (IPv6 dạng [::1]:6969)",
    )
    parser.add_argument(
        "--max-file-size",
        type=int,
        default=DEFAULT_MAX_FILE_MIB,
        metavar="MIB",
        help=f"giới hạn mỗi file theo MiB (mặc định {DEFAULT_MAX_FILE_MIB})",
    )
    parser.add_argument(
        "--tftp-blocksize",
        type=int,
        default=DEFAULT_TFTP_BLOCKSIZE,
        help=f"blksize khi gửi WRQ (mặc định {DEFAULT_TFTP_BLOCKSIZE})",
    )
    parser.add_argument("--tftp-timeout", type=float, default=5.0, help="timeout TFTP (giây)")
    parser.add_argument("--tftp-retries", type=int, default=5, help="số lần thử lại TFTP")
    parser.add_argument("--threads", type=int, default=4, help="số thread của Waitress")
    parser.add_argument("-v", "--verbose", action="store_true", help="log chi tiết")
    return parser


def main(argv: list[str] | None = None) -> int:
    enable_utf8_console()
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        host = validate_host(args.host)
        networks = parse_networks(args.allowed_network)
        tftp_host, tftp_port = _parse_server(args.tftp_server)
        if args.max_file_size <= 0:
            raise ValueError("--max-file-size phải > 0")
        if not 1 <= args.port <= 65535:
            raise ValueError("--port ngoài khoảng 1..65535")
    except ValueError as exc:
        parser.error(str(exc))
        return 2

    app = create_app(
        allowed_networks=networks,
        tftp_host=tftp_host,
        tftp_port=tftp_port,
        max_file_bytes=args.max_file_size * 1024 * 1024,
        tftp_blocksize=args.tftp_blocksize,
        tftp_timeout=args.tftp_timeout,
        tftp_retries=args.tftp_retries,
    )
    try:
        from waitress import serve
    except ImportError:
        LOG.error('thiếu waitress — cài bằng: python -m pip install -e ".[web]"')
        return 2

    shown_host = f"[{host}]" if ":" in host else host
    LOG.info(
        "cổng upload sẵn sàng: http://%s:%d (mạng cho phép: %s), TFTP=%s:%s",
        shown_host,
        args.port,
        ", ".join(str(network) for network in networks),
        tftp_host,
        tftp_port,
    )
    try:
        serve(app, host=host, port=args.port, threads=args.threads, ident="tftp-web")
    except OSError as exc:
        LOG.error("không khởi động được cổng web: %s", exc)
        return 2
    except KeyboardInterrupt:
        LOG.info("đã dừng cổng web")
    return 0


if __name__ == "__main__":
    sys.exit(main())
