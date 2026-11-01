"""Test cổng web upload (extras ``[web]``): ACL, CSRF, giới hạn và luồng TFTP thật."""

from __future__ import annotations

import io
import logging
import re
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import flask  # noqa: F401
except ImportError:  # pragma: no cover - môi trường chưa cài extras [web]
    raise unittest.SkipTest('cần extras [web]: python -m pip install -e ".[web]"') from None

from src.tftp_errors import TftpRemoteError
from src.tftp_server import TftpServer
from src.web_upload import (
    create_app,
    parse_networks,
    safe_remote_name,
    validate_host,
)

QUIET = logging.getLogger("tftp.test.web")
QUIET.setLevel(logging.CRITICAL)
logging.getLogger("tftp.web").setLevel(logging.CRITICAL)
logging.getLogger("tftp.client").setLevel(logging.CRITICAL)


class TftpHarness:
    """TFTP server thật trên port ngẫu nhiên, phục vụ thư mục tạm."""

    def __init__(self) -> None:
        self.dir = tempfile.TemporaryDirectory(prefix="tftp-web-test-")
        self.root = self.dir.name
        self.server = TftpServer(
            host="127.0.0.1",
            port=0,
            root=self.root,
            timeout=1.0,
            retries=3,
            logger=QUIET,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> TftpHarness:
        self.thread.start()
        for _ in range(200):
            if self.server.bound_port:
                break
            threading.Event().wait(0.01)
        return self

    def __exit__(self, *exc_info) -> None:
        self.server.shutdown()
        self.server.wait_for_idle(timeout=5.0)
        self.thread.join(timeout=5)
        self.dir.cleanup()

    @property
    def port(self) -> int:
        return self.server.bound_port

    def read(self, name: str) -> bytes:
        return Path(self.root, name).read_bytes()


def make_app(harness: TftpHarness, **overrides):
    options = {
        "allowed_networks": parse_networks(["127.0.0.1/32"]),
        "tftp_host": "127.0.0.1",
        "tftp_port": harness.port,
        "max_file_bytes": 8 * 1024 * 1024,
        "tftp_timeout": 1.0,
        "tftp_retries": 2,
        "secret_key": "test-secret",
    }
    options.update(overrides)
    return create_app(**options)


def csrf_token(client) -> str:
    body = client.get("/").get_data(as_text=True)
    match = re.search(r'name="csrf-token" content="([^"]+)"', body)
    assert match is not None, "trang chủ phải có CSRF token"
    return match.group(1)


def upload(client, payload: bytes, filename: str, token: str, headers=None, **kwargs):
    request_headers = {"X-CSRF-Token": token}
    request_headers.update(headers or {})
    return client.post(
        "/upload",
        data={"file": (io.BytesIO(payload), filename)},
        content_type="multipart/form-data",
        headers=request_headers,
        **kwargs,
    )


class TestConfigValidation(unittest.TestCase):
    def test_host_must_be_specific_private_or_loopback(self):
        self.assertEqual(validate_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(validate_host("192.168.1.10"), "192.168.1.10")
        self.assertEqual(validate_host("::1"), "::1")
        self.assertEqual(validate_host("fd12:3456:789a::10"), "fd12:3456:789a::10")
        for bad in ("0.0.0.0", "::", "8.8.8.8", "example.com", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_host(bad)

    def test_networks_must_be_private(self):
        networks = parse_networks(["192.168.1.0/24", "fd12:3456:789a::/64"])
        self.assertEqual(len(networks), 2)
        for bad in ([], ["0.0.0.0/0"], ["8.8.8.0/24"], ["::/0"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_networks(bad)

    def test_safe_remote_name(self):
        self.assertEqual(safe_remote_name("../../evil.txt"), "evil.txt")
        self.assertEqual(safe_remote_name("C:\\tmp\\bao.zip"), "bao.zip")
        self.assertEqual(safe_remote_name("bao\x00cao.txt"), "baocao.txt")
        self.assertIsNone(safe_remote_name(""))
        self.assertIsNone(safe_remote_name("CON.txt"))
        self.assertIsNone(safe_remote_name(".."))


class TestPage(unittest.TestCase):
    def test_page_has_token_and_security_headers(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            response = client.get("/")
            self.assertEqual(response.status_code, 200)
            body = response.get_data(as_text=True)
            self.assertIn('name="csrf-token"', body)
            csp = response.headers["Content-Security-Policy"]
            self.assertIn("default-src 'none'", csp)
            self.assertIn("frame-ancestors 'none'", csp)
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
            self.assertEqual(response.headers["X-Frame-Options"], "DENY")
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertNotIn("Access-Control-Allow-Origin", response.headers)

    def test_page_supports_multiple_files_and_drop(self):
        with TftpHarness() as harness:
            body = make_app(harness).test_client().get("/").get_data(as_text=True)
            self.assertIn('multiple', body)
            self.assertIn('id="drop"', body)
            self.assertIn('id="picked"', body)

    def test_no_listing_or_download_routes(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            for path in ("/files", "/download", "/files/hello.txt"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path).status_code, 404)


class TestHealthz(unittest.TestCase):
    def test_healthz_reports_config_without_probe(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            response = client.get("/healthz")
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data["ok"])
            self.assertGreaterEqual(data["uptime_s"], 0)
            self.assertEqual(data["tftp"]["host"], "127.0.0.1")
            self.assertEqual(data["tftp"]["port"], harness.port)
            self.assertNotIn("reachable", data["tftp"])

    def test_healthz_probe_reaches_running_server(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            response = client.get("/healthz?probe=1")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.get_json()["tftp"]["reachable"])

    def test_healthz_probe_dead_server_returns_503(self):
        probe_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe_sock.bind(("127.0.0.1", 0))
        dead_port = probe_sock.getsockname()[1]
        probe_sock.close()
        with TftpHarness() as harness:
            client = make_app(harness, tftp_port=dead_port).test_client()
            response = client.get("/healthz?probe=1")
            self.assertEqual(response.status_code, 503)
            data = response.get_json()
            self.assertFalse(data["ok"])
            self.assertFalse(data["tftp"]["reachable"])

    def test_healthz_obeys_network_acl(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            response = client.get("/healthz", environ_base={"REMOTE_ADDR": "10.9.9.9"})
            self.assertEqual(response.status_code, 403)


class TestUpload(unittest.TestCase):
    def test_upload_reaches_tftp_server(self):
        payload = bytes(range(256)) * 40
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            response = upload(client, payload, "hello.bin", csrf_token(client))
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            data = response.get_json()
            self.assertTrue(data["ok"])
            self.assertEqual(data["filename"], "hello.bin")
            self.assertEqual(data["bytes"], len(payload))
            self.assertEqual(harness.read("hello.bin"), payload)

    def test_multiple_files_uploaded_sequentially(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            token = csrf_token(client)
            for name, payload in (("first.bin", b"first"), ("second.bin", b"second")):
                response = upload(client, payload, name, token)
                self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(harness.read("first.bin"), b"first")
            self.assertEqual(harness.read("second.bin"), b"second")

    def test_empty_file_is_allowed(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            response = upload(client, b"", "empty.dat", csrf_token(client))
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(harness.read("empty.dat"), b"")

    def test_upload_without_csrf_is_rejected(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            response = client.post(
                "/upload",
                data={"file": (io.BytesIO(b"x"), "x.bin")},
                content_type="multipart/form-data",
            )
            self.assertEqual(response.status_code, 403)
            self.assertFalse(Path(harness.root, "x.bin").exists())

    def test_foreign_peer_is_rejected(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            token = csrf_token(client)
            response = upload(
                client,
                b"x",
                "x.bin",
                token,
                environ_base={"REMOTE_ADDR": "10.9.9.9"},
            )
            self.assertEqual(response.status_code, 403)
            self.assertIn("mạng", response.get_json()["error"])

    def test_origin_mismatch_is_rejected(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            token = csrf_token(client)
            response = upload(
                client,
                b"x",
                "x.bin",
                token,
                headers={
                    "X-CSRF-Token": token,
                    "Origin": "http://evil.example",
                },
            )
            self.assertEqual(response.status_code, 403)

    def test_oversized_file_is_rejected(self):
        with TftpHarness() as harness:
            client = make_app(harness, max_file_bytes=1024).test_client()
            response = upload(client, b"a" * 4096, "big.bin", csrf_token(client))
            self.assertEqual(response.status_code, 413)
            self.assertFalse(Path(harness.root, "big.bin").exists())

    def test_unsafe_filename_is_sanitised(self):
        with TftpHarness() as harness:
            client = make_app(harness).test_client()
            response = upload(client, b"payload", "../../evil.txt", csrf_token(client))
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            self.assertEqual(response.get_json()["filename"], "evil.txt")
            self.assertEqual(harness.read("evil.txt"), b"payload")
            self.assertFalse((Path(harness.root).parent / "evil.txt").exists())

    def test_tftp_error_maps_to_bad_gateway(self):
        with TftpHarness() as harness:
            app = make_app(harness)
            client = app.test_client()
            token = csrf_token(client)
            with mock.patch("src.web_upload.TftpClient") as fake:
                fake.return_value.upload.side_effect = TftpRemoteError(6, "đã tồn tại")
                response = upload(client, b"x", "x.bin", token)
            self.assertEqual(response.status_code, 502)
            self.assertFalse(response.get_json()["ok"])


if __name__ == "__main__":
    unittest.main()
