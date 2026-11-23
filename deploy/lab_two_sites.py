#!/usr/bin/env python3
"""Mô phỏng hai trụ sở trên một máy — chỉ dùng cho demo, không phải VPN thật.

Một cổng chat trung tâm chạy ở 127.0.0.1:8093. Hai reverse proxy nhỏ đặt trước nó,
mỗi proxy gắn một IP nguồn giả cho một trụ sở:

    Site A  http://127.0.0.1:8094   X-Forwarded-For: 192.168.10.10
    Site B  http://[::1]:8095       X-Forwarded-For: 192.168.20.20
    Site C  http://127.0.0.1:8096   X-Forwarded-For: 203.0.113.5  (bị ACL chặn)

Cả A và B dùng chung một SQLite, một phòng chat, một bảng quản trị. Site C cho thấy
ACL từ chối subnet lạ. Đây là cách kiểm thử mô hình nhiều trụ sở mà không cần
WireGuard; khi triển khai thật, thay proxy bằng VPN và dùng deploy/README.md.
"""

from __future__ import annotations

import argparse
import http.client
import logging
import signal
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from waitress.server import create_server as create_web_server  # noqa: E402

from tftp import enable_utf8_console  # noqa: E402
from tftp.tftp_server import TftpServer  # noqa: E402
from tftp.web_store import Store  # noqa: E402
from tftp.web_upload import create_app  # noqa: E402

WEB_PORT = 8093
SITE_A_PORT = 8094
SITE_B_PORT = 8095
SITE_C_PORT = 8096
TFTP_PORT = 6971
SITE_A_NET = "192.168.10.0/24"
SITE_B_NET = "192.168.20.0/24"
PASSWORD = "matkhau123"


def sample_data(data_dir: Path):
    files = data_dir / "files"
    files.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "portal.sqlite3")
    if store.user_by_name("alice_nguyen"):
        return store
    accounts = {}
    for name in ("alice_nguyen", "bob_tran", "chi_le", "duc_pham", "hoa_vo"):
        user_id, _role = store.register(name, PASSWORD)
        accounts[name] = user_id
    store.set_mute(accounts["chi_le"], True, 1440, "Gửi tin nhắn trùng lặp quá nhanh")
    store.set_ban(accounts["duc_pham"], True, 10080, "Spam liên kết lạ")
    for author, body in (
        ("alice_nguyen", "Chào cả nhóm! Báo cáo TFTP đã nằm trong mục Tệp, mọi người xem giúp."),
        ("bob_tran", "Phần bắt tay ba bước đọc rõ rồi. Có cần thêm ảnh minh hoạ gói ACK không?"),
        ("chi_le", "Mình thấy nên thêm bảng so sánh throughput giữa block 512 và 1024 byte."),
        ("hoa_vo", "Mình vừa gửi hướng dẫn sử dụng, có thêm mục xử lý timeout."),
        ("alice_nguyen", "Cảm ơn cả nhóm, buổi báo cáo hôm nay chạy rất mượt."),
    ):
        store.send(accounts[author], body)
    store.log(accounts["alice_nguyen"], "alice_nguyen", "mute", accounts["chi_le"],
              "chi_le", "Hạn chế chat 1 ngày · Gửi tin nhắn trùng lặp quá nhanh")
    (files / "huong_dan_su_dung.txt").write_text(
        "Hướng dẫn sử dụng cổng chat nội bộ.\nGửi file, tải file và trò chuyện trong LAN.\n",
        encoding="utf-8",
    )
    (files / "Bao_cao_TFTP.pdf").write_bytes(b"%PDF-1.4\n% demo file for the two-site lab\n")
    (files / "anh_nhom.jpg").write_bytes(b"\xff\xd8\xff\xe0 demo jpeg data")
    return store


class SiteProxyHandler(BaseHTTPRequestHandler):
    """Reverse proxy nhỏ, gắn X-Forwarded-* cố định cho một site."""

    protocol_version = "HTTP/1.0"
    site_ip = "192.168.10.10"
    backend = ("127.0.0.1", WEB_PORT)
    hop_headers = {
        "host", "content-length", "connection", "keep-alive", "accept-encoding",
        "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
        "proxy-authenticate", "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
    }

    def log_message(self, *args):
        return

    def proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {
            key: value for key, value in self.headers.items()
            if key.lower() not in self.hop_headers
        }
        headers.update({
            "Host": f"{self.backend[0]}:{self.backend[1]}",
            "X-Forwarded-For": self.site_ip,
            "X-Forwarded-Proto": "http",
            "X-Forwarded-Host": self.headers.get("Host", f"127.0.0.1:{self.server.server_port}"),
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        connection = http.client.HTTPConnection(*self.backend, timeout=60)
        try:
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read()
            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() not in {"transfer-encoding", "connection", "content-length",
                                       "server", "date"}:
                    self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
        except OSError as exc:
            self.send_error(502, f"Backend không phản hồi: {exc}")
        finally:
            connection.close()

    do_GET = proxy
    do_HEAD = proxy
    do_POST = proxy
    do_DELETE = proxy
    do_PUT = proxy


class ThreadingHTTPServerV6(ThreadingHTTPServer):
    address_family = socket.AF_INET6


def proxy_class(site_ip: str):
    return type(f"SiteProxy{site_ip.replace('.', '_')}", (SiteProxyHandler,), {"site_ip": site_ip})


def start_proxy(port: int, site_ip: str, *, host: str = "127.0.0.1",
                server_class=ThreadingHTTPServer):
    server = server_class((host, port), proxy_class(site_ip))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def port_free(port: int, host: str = "127.0.0.1", family: int = socket.AF_INET) -> bool:
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def udp_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def wait_health(url: str, site_ip: str, seconds: float = 15.0):
    deadline = time.monotonic() + seconds
    request = urllib.request.Request(url, headers={"X-Forwarded-For": site_ip})
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError):
            time.sleep(0.2)
    raise RuntimeError(f"Web gateway không sẵn sàng tại {url}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Lab mô phỏng hai trụ sở cho cổng chat")
    parser.add_argument("--data-dir", type=Path, help="Thư mục dữ liệu (mặc định: thư mục tạm)")
    args = parser.parse_args(argv)
    enable_utf8_console()

    for port in (WEB_PORT, SITE_A_PORT, SITE_B_PORT, SITE_C_PORT):
        if not port_free(port):
            print(f"[lab] Cổng TCP {port} đang bận. Dừng tiến trình cũ rồi thử lại.",
                  file=sys.stderr)
            return 2
    if not udp_port_free(TFTP_PORT):
        print(f"[lab] Cổng UDP {TFTP_PORT} đang bận. Dừng tiến trình cũ rồi thử lại.",
              file=sys.stderr)
        return 2

    if args.data_dir:
        data_dir = args.data_dir.resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
    else:
        data_dir = Path(tempfile.mkdtemp(prefix="tftp-two-sites-"))
    files_dir = data_dir / "files"
    sample_data(data_dir)

    logging.getLogger("waitress").setLevel(logging.WARNING)
    tftp_log = logging.getLogger("tftp.lab")
    tftp_log.setLevel(logging.WARNING)
    app = create_app({
        "FILE_ROOT": str(files_dir),
        "DATA_DIR": str(data_dir),
        "ALLOWED_NETWORKS": [SITE_A_NET, SITE_B_NET],
        "TRUSTED_PROXIES": ["127.0.0.1/32"],
        "TFTP_HOST": "127.0.0.1",
        "TFTP_PORT": TFTP_PORT,
        "MAX_FILE_SIZE": 25 * 1024 * 1024,
    })
    web = create_web_server(app, host="127.0.0.1", port=WEB_PORT, threads=8,
                            clear_untrusted_proxy_headers=False)
    web_thread = threading.Thread(target=web.run, daemon=True)
    web_thread.start()

    tftp = TftpServer(host="127.0.0.1", port=TFTP_PORT, root=str(files_dir),
                      timeout=2, retries=2, logger=tftp_log)
    tftp_thread = threading.Thread(target=tftp.serve_forever, daemon=True)
    tftp_thread.start()

    proxies = []
    try:
        wait_health(f"http://127.0.0.1:{WEB_PORT}/healthz", "192.168.10.10")
        proxies.append(start_proxy(SITE_A_PORT, "192.168.10.10"))
        site_b_url = f"http://[::1]:{SITE_B_PORT}"
        try:
            proxies.append(start_proxy(SITE_B_PORT, "192.168.20.20", host="::1",
                                       server_class=ThreadingHTTPServerV6))
        except OSError:
            site_b_url = f"http://127.0.0.1:{SITE_B_PORT}"
            proxies.append(start_proxy(SITE_B_PORT, "192.168.20.20"))
            print("[lab] IPv6 loopback không dùng được — site B dùng 127.0.0.1; "
                  "mở cửa sổ ẩn danh nếu muốn đăng nhập tài khoản khác.")
        proxies.append(start_proxy(SITE_C_PORT, "203.0.113.5"))

        print()
        print("  LAB HAI TRỤ SỞ — một phòng chat, hai địa chỉ truy cập")
        print(f"  Trụ sở A : http://127.0.0.1:{SITE_A_PORT}")
        print(f"  Trụ sở B : {site_b_url}" + ("  (hoặc http://localhost:"
                                            f"{SITE_B_PORT})" if "::1" in site_b_url else ""))
        print(f"  Site lạ  : http://127.0.0.1:{SITE_C_PORT}  (ACL trả 403)")
        print()
        print("  Tài khoản: alice_nguyen / matkhau123  (quản trị)")
        print("             bob_tran / matkhau123        (thành viên)")
        print(f"  Dữ liệu  : {data_dir}")
        print("  Ctrl+C để dừng.")
        print(flush=True)
        stop = threading.Event()

        def handle_signal(_signum, _frame):
            stop.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
        while not stop.wait(0.5):
            pass
    finally:
        for server, thread in proxies:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
        web.close()
        web_thread.join(timeout=5)
        tftp.shutdown()
        tftp_thread.join(timeout=5)
        print("[lab] Đã dừng.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
