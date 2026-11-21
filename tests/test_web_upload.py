"""LAN upload portal tests."""

from __future__ import annotations

import logging
import socket
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tftp import tftp_errors
from tftp.tftp_server import TftpServer

try:
    from tftp.web_upload import COOKIE, UploadConfigError, create_app, validate_bind_host
except ModuleNotFoundError as exc:
    if exc.name not in {"flask", "werkzeug"}:
        raise
    create_app = None

TEST_HOSTS = ["upload.lan", "localhost", "127.0.0.1", "::1"]


def authenticated_client(app):
    """Exercise registration before using the upload API: it now requires an account."""
    client = app.test_client()
    csrf = client.get("/api/session", base_url="http://upload.lan").json["csrf"]
    response = client.post(
        "/api/register", json={"username": "upload_test", "password": "test-password123"},
        base_url="http://upload.lan", headers={"X-CSRF-Token": csrf})
    if response.status_code == 409:
        response = client.post(
            "/api/login", json={"username": "upload_test", "password": "test-password123"},
            base_url="http://upload.lan", headers={"X-CSRF-Token": csrf})
    if response.status_code != 200:
        raise AssertionError(response.json)
    return client, response.json["csrf"]


@unittest.skipIf(create_app is None, 'Install web extras: pip install -e ".[web]"')
class TestWebUpload(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.tftp_root = Path(self.temp.name) / "tftp-root"
        self.tftp_root.mkdir()
        self.app = create_app(
            {"FILE_ROOT": self.tftp_root, "DATA_DIR": Path(self.temp.name) / "data",
             "ALLOWED_HOSTS": TEST_HOSTS},
            allowed_networks=["192.168.10.0/24", "127.0.0.1/32"],
            tftp_host="127.0.0.1",
            tftp_port=6969,
            max_file_size=1024,
        )
        self.app.config["TESTING"] = True
        self.client, self.csrf = authenticated_client(self.app)

    def tearDown(self):
        self.temp.cleanup()

    def upload_headers(self, host="upload.lan"):
        return {
            "Origin": f"http://{host}",
            "Host": host,
            "X-CSRF-Token": self.csrf,
        }

    def test_page_and_session_provide_csrf_and_no_store_header(self):
        response = self.client.get("/", environ_base={"REMOTE_ADDR": "192.168.10.8"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"portal.js", response.data)
        session = self.client.get("/api/session", base_url="http://upload.lan").json
        self.assertEqual(session["csrf"], self.csrf)
        self.assertEqual(session["max_file_size"], 1024)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("Content-Security-Policy", response.headers)

    def test_remote_outside_allowed_lan_is_denied(self):
        response = self.client.get("/", environ_base={"REMOTE_ADDR": "203.0.113.8"})
        self.assertEqual(response.status_code, 403)

    def test_host_header_must_be_allowlisted(self):
        self.assertEqual(self.client.get("/", headers={"Host": "upload.lan"}).status_code, 200)
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 400)
        # IPv6 literal: port phải bị bỏ đúng, không cắt nhầm tại dấu ':' đầu tiên.
        self.assertEqual(self.client.get("/", headers={"Host": "[::1]:8080"}).status_code, 200)
        self.assertEqual(
            self.client.get("/", headers={"Host": "[2001:db8::1]:8080"}).status_code, 400
        )

    def test_upload_forwards_file_to_tftp_server(self):
        client_instances = []
        staged_paths = []
        tftp_root = self.tftp_root

        class FakeTftpClient:
            def __init__(self, host, port, **kwargs):
                client_instances.append((host, port))

            def upload(self, local, remote):
                staged_paths.append(Path(local))
                (tftp_root / remote).write_bytes(Path(local).read_bytes())

        with patch("tftp.web_upload.TftpClient", FakeTftpClient):
            response = self.client.post(
                "/api/upload",
                data={"file": (bytes_file("report.txt", b"hello"), "report.txt")},
                headers=self.upload_headers(),
                environ_base={"REMOTE_ADDR": "192.168.10.8"},
            )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(client_instances, [("127.0.0.1", 6969)])
        self.assertEqual((self.tftp_root / "report.txt").read_bytes(), b"hello")
        self.assertEqual(response.json["size"], 5)
        self.assertEqual(len(staged_paths), 1)
        self.assertFalse(staged_paths[0].exists())

    def test_browser_upload_is_received_by_real_tftp_server(self):
        server = TftpServer(
            host="127.0.0.1",
            port=0,
            root=str(self.tftp_root),
            timeout=0.5,
            retries=2,
            logger=logging.getLogger("tftp.test.web"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                if server.bound_port:
                    break
                time.sleep(0.01)
            self.assertGreater(server.bound_port, 0)
            self.app = create_app(
                {"FILE_ROOT": self.tftp_root, "DATA_DIR": Path(self.temp.name) / "data2",
                 "ALLOWED_HOSTS": TEST_HOSTS},
                allowed_networks=["192.168.10.0/24", "127.0.0.1/32"],
                tftp_host="127.0.0.1",
                tftp_port=server.bound_port,
                max_file_size=1024,
            )
            self.app.config["TESTING"] = True
            self.client, self.csrf = authenticated_client(self.app)
            payload = b"received through TFTP"
            response = self.client.post(
                "/api/upload",
                data={"file": (bytes_file("from-browser.txt", payload), "from-browser.txt")},
                headers=self.upload_headers(),
                environ_base={"REMOTE_ADDR": "192.168.10.8"},
            )

            self.assertEqual(response.status_code, 201)
            self.assertEqual((self.tftp_root / "from-browser.txt").read_bytes(), payload)
        finally:
            server.shutdown()
            server.wait_for_idle(timeout=5)
            thread.join(timeout=5)

    def test_healthz_reports_database_and_tftp(self):
        server = TftpServer(
            host="127.0.0.1",
            port=0,
            root=str(self.tftp_root),
            timeout=0.5,
            retries=1,
            logger=logging.getLogger("tftp.test.health"),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.bound_port and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertGreater(server.bound_port, 0)
            app = create_app(
                {"FILE_ROOT": self.tftp_root,
                 "DATA_DIR": Path(self.temp.name) / "health",
                 "ALLOWED_HOSTS": TEST_HOSTS, "HEALTH_CACHE_SECONDS": 0},
                allowed_networks=["192.168.10.0/24", "127.0.0.1/32"],
                tftp_host="127.0.0.1",
                tftp_port=server.bound_port,
            )
            response = app.test_client().get("/healthz")
            self.assertEqual(response.status_code, 200, response.json)
            self.assertEqual(response.json["checks"], {"database": "ok", "tftp": "ok"})
        finally:
            server.shutdown()
            server.wait_for_idle(timeout=5)
            thread.join(timeout=5)

    def test_healthz_reports_unreachable_tftp_and_database(self):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = create_app(
            {"FILE_ROOT": self.tftp_root,
             "DATA_DIR": Path(self.temp.name) / "health-down",
             "ALLOWED_HOSTS": TEST_HOSTS, "HEALTH_CACHE_SECONDS": 0,
             "HEALTH_TIMEOUT": 0.2},
            allowed_networks=["192.168.10.0/24", "127.0.0.1/32"],
            tftp_host="127.0.0.1",
            tftp_port=port,
        )
        client = app.test_client()
        response = client.get("/healthz")
        self.assertEqual(response.status_code, 503, response.json)
        self.assertEqual(response.json["checks"]["database"], "ok")
        self.assertEqual(response.json["checks"]["tftp"], "unreachable")

        store = app.extensions["web_store"]
        with patch.object(store, "ping", side_effect=sqlite3.OperationalError("db down")):
            response = client.get("/healthz")
        self.assertEqual(response.status_code, 503, response.json)
        self.assertEqual(response.json["checks"]["database"], "error")

    def test_tftp_server_duplicate_is_returned_without_overwrite(self):
        def reject_duplicate(_local, _remote):
            raise tftp_errors.TftpRemoteError(tftp_errors.ERR_FILE_EXISTS, "file already exists")

        with patch("tftp.web_upload.TftpClient") as tftp_client:
            tftp_client.return_value.upload.side_effect = reject_duplicate
            response = self.client.post(
                "/api/upload",
                data={"file": (bytes_file("same.txt", b"new"), "same.txt")},
                headers=self.upload_headers(),
                environ_base={"REMOTE_ADDR": "192.168.10.8"},
            )

        self.assertEqual(response.status_code, 409)
        self.assertIn("TFTP server", response.json["error"])

    def test_tftp_timeout_is_reported_as_gateway_timeout(self):
        with patch("tftp.web_upload.TftpClient") as tftp_client:
            tftp_client.return_value.upload.side_effect = tftp_errors.TftpTimeout("server timeout")
            response = self.client.post(
                "/api/upload",
                data={"file": (bytes_file("report.txt", b"x"), "report.txt")},
                headers=self.upload_headers(),
                environ_base={"REMOTE_ADDR": "192.168.10.8"},
            )

        self.assertEqual(response.status_code, 504)
        self.assertIn("TFTP server", response.json["error"])

    def test_invalid_csrf_or_origin_is_rejected(self):
        for headers in (
            {"Origin": "http://upload.lan", "Host": "upload.lan", "X-CSRF-Token": "bad"},
            {
                "Origin": "http://attacker.example",
                "Host": "upload.lan",
                "X-CSRF-Token": self.csrf,
            },
            {"Host": "upload.lan", "X-CSRF-Token": self.csrf},
        ):
            with self.subTest(headers=headers):
                response = self.client.post(
                    "/api/upload",
                    data={"file": (bytes_file("report.txt", b"x"), "report.txt")},
                    headers=headers,
                    environ_base={"REMOTE_ADDR": "192.168.10.8"},
                )
                self.assertEqual(response.status_code, 403)

    def test_upload_size_limit_and_temporary_cleanup(self):
        with patch("tftp.web_upload.TftpClient") as tftp_client:
            response = self.client.post(
                "/api/upload",
                data={"file": (bytes_file("large.bin", b"x" * 2048), "large.bin")},
                headers=self.upload_headers(),
                environ_base={"REMOTE_ADDR": "192.168.10.8"},
            )
        self.assertEqual(response.status_code, 413)
        tftp_client.return_value.upload.assert_not_called()

    def test_only_allows_configured_number_of_concurrent_uploads(self):
        app = create_app(
            {"FILE_ROOT": self.tftp_root, "DATA_DIR": Path(self.temp.name) / "concurrent",
             "ALLOWED_HOSTS": TEST_HOSTS},
            allowed_networks=["192.168.10.0/24", "127.0.0.1/32"],
            max_file_size=1024,
            max_concurrent_uploads=1,
        )
        app.config["TESTING"] = True
        entered_tftp = threading.Event()
        finish_tftp = threading.Event()
        first_responses = []

        class BlockingTftpClient:
            def __init__(self, _host, _port, **kwargs):
                pass

            def upload(self, _local, _remote):
                entered_tftp.set()
                if not finish_tftp.wait(timeout=5):
                    raise TimeoutError("test upload was not released")

        authenticated, csrf = authenticated_client(app)
        token = authenticated.get_cookie(COOKIE, domain="upload.lan").value
        headers = {
            "Origin": "http://upload.lan",
            "Host": "upload.lan",
            "X-CSRF-Token": csrf,
        }

        def first_upload():
            with app.test_client() as client:
                client.set_cookie(COOKIE, token, domain="upload.lan")
                first_responses.append(
                    client.post(
                        "/api/upload",
                        data={"file": (bytes_file("first.txt", b"first"), "first.txt")},
                        headers=headers,
                        environ_base={"REMOTE_ADDR": "192.168.10.8"},
                    )
                )

        with patch("tftp.web_upload.TftpClient", BlockingTftpClient):
            worker = threading.Thread(target=first_upload)
            worker.start()
            try:
                self.assertTrue(entered_tftp.wait(timeout=5))
                with app.test_client() as client:
                    client.set_cookie(COOKIE, token, domain="upload.lan")
                    response = client.post(
                        "/api/upload",
                        data={"file": (bytes_file("second.txt", b"second"), "second.txt")},
                        headers=headers,
                        environ_base={"REMOTE_ADDR": "192.168.10.8"},
                    )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers["Retry-After"], "5")
            finally:
                finish_tftp.set()
                worker.join(timeout=5)

        self.assertFalse(worker.is_alive())
        self.assertEqual(first_responses[0].status_code, 201)
        semaphore = app.config["UPLOAD_SEMAPHORE"]
        self.assertTrue(semaphore.acquire(blocking=False))
        semaphore.release()

    def test_private_bind_validation(self):
        self.assertEqual(validate_bind_host("192.168.1.10"), "192.168.1.10")
        self.assertEqual(validate_bind_host("127.0.0.1"), "127.0.0.1")
        for address in ("0.0.0.0", "8.8.8.8", "::"):
            with self.subTest(address=address), self.assertRaises(UploadConfigError):
                validate_bind_host(address)

    def test_public_allowlist_and_invalid_tftp_port_are_rejected(self):
        with self.assertRaises(UploadConfigError):
            create_app(allowed_networks=["0.0.0.0/0"])
        with self.assertRaises(UploadConfigError):
            create_app(allowed_networks=["127.0.0.1/32"], tftp_port=70000)
        with self.assertRaises(UploadConfigError):
            create_app(
                allowed_networks=["127.0.0.1/32"],
                max_concurrent_uploads=0,
            )
        with self.assertRaises(UploadConfigError):
            create_app(allowed_hosts=["bad host"])


def bytes_file(_filename: str, content: bytes) -> BytesIO:
    """Create an in-memory upload stream for Flask's test client."""
    return BytesIO(content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
