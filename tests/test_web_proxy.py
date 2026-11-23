"""Trusted reverse proxy, forwarded headers and CGNAT/Tailscale address support."""

from __future__ import annotations

import ipaddress
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

try:
    from tftp.web_upload import (
        COOKIE,
        UploadConfigError,
        create_app,
        forwarded_client,
        forwarded_origin,
        private_network,
        validate_bind_host,
    )
except ModuleNotFoundError as exc:
    if exc.name not in {"flask", "werkzeug"}:
        raise
    create_app = None


@unittest.skipIf(create_app is None, 'Install web extras: pip install -e ".[web]"')
class ProxyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.config = dict(
            TESTING=True,
            FILE_ROOT=base / "files",
            DATA_DIR=base / "data",
            ALLOWED_NETWORKS=["192.168.10.0/24", "192.168.20.0/24"],
            TRUSTED_PROXIES=["10.8.0.1/32"],
        )
        self.app = create_app(self.config)
        self.client = self.app.test_client()

    def forwarded(self, path="/", *, client_ip="192.168.20.9", peer="10.8.0.1",
                  headers=None, method="GET", **kwargs):
        all_headers = dict(headers or {})
        if client_ip is not None:
            all_headers.setdefault("X-Forwarded-For", client_ip)
        return self.client.open(path, method=method, headers=all_headers,
                                environ_overrides={"REMOTE_ADDR": peer}, **kwargs)

    def session(self):
        response = self.forwarded("/api/session")
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["csrf"]

    def test_forwarded_client_ip_controls_lan_acl(self):
        self.assertEqual(self.forwarded("/").status_code, 200)
        self.assertEqual(self.forwarded("/", client_ip="192.168.10.25").status_code, 200)
        # A trusted proxy without a forwarded client is still just another address.
        self.assertEqual(self.forwarded("/", client_ip=None).status_code, 403)
        # Forwarded headers from an untrusted peer are ignored.
        self.assertEqual(self.forwarded("/", peer="8.8.8.8").status_code, 403)
        # Direct requests keep working without any proxy headers.
        self.assertEqual(self.forwarded("/", client_ip=None, peer="192.168.10.7").status_code, 200)

    def test_rightmost_untrusted_forwarded_address_wins(self):
        self.assertEqual(self.forwarded("/", client_ip="203.0.113.5, 192.168.20.9")
                         .status_code, 200)
        self.assertEqual(self.forwarded("/", client_ip="192.168.20.9, 203.0.113.5")
                         .status_code, 403)
        self.assertEqual(self.forwarded("/", client_ip="192.168.20.9, 10.8.0.1")
                         .status_code, 200)
        self.assertEqual(self.forwarded("/", client_ip="garbage, 192.168.20.9")
                         .status_code, 200)

    def test_forwarded_origin_satisfies_csrf_origin_check(self):
        csrf = self.session()
        response = self.forwarded(
            "/api/register", method="POST",
            headers={"Origin": "https://chat.congty.local",
                     "X-Forwarded-Proto": "https", "X-Forwarded-Host": "chat.congty.local",
                     "X-CSRF-Token": csrf},
            json={"username": "office_user", "password": "password123"},
        )
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["user"], "office_user")
        wrong = self.forwarded(
            "/api/login", method="POST",
            headers={"Origin": "https://evil.example",
                     "X-Forwarded-Proto": "https", "X-Forwarded-Host": "chat.congty.local",
                     "X-CSRF-Token": csrf},
            json={"username": "office_user", "password": "password123"},
        )
        self.assertEqual(wrong.status_code, 403)

    def test_invalid_forwarded_origin_falls_back_safely(self):
        csrf = self.session()
        for host in ["chat.congty.local/evil", "chat.congty.local:99999", "bad host"]:
            with self.subTest(host=host):
                response = self.forwarded(
                    "/api/login", method="POST",
                    headers={"Origin": "https://chat.congty.local",
                             "X-Forwarded-Proto": "https",
                             "X-Forwarded-Host": host,
                             "X-CSRF-Token": csrf},
                    json={"username": "nobody", "password": "password123"},
                )
                self.assertEqual(response.status_code, 403)
        for scheme in ["ftp", "", "javascript"]:
            with self.subTest(scheme=scheme):
                response = self.forwarded(
                    "/api/login", method="POST",
                    headers={"Origin": "https://chat.congty.local",
                             "X-Forwarded-Proto": scheme,
                             "X-Forwarded-Host": "chat.congty.local",
                             "X-CSRF-Token": csrf},
                    json={"username": "nobody", "password": "password123"},
                )
                self.assertEqual(response.status_code, 403)

    def test_secure_cookie_for_https_proxy(self):
        app = create_app({**self.config, "COOKIE_SECURE": True})
        client = app.test_client()
        response = client.get(
            "/api/session",
            headers={"X-Forwarded-For": "192.168.20.9", "X-Forwarded-Proto": "https",
                     "X-Forwarded-Host": "chat.congty.local"},
            environ_overrides={"REMOTE_ADDR": "10.8.0.1"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure", response.headers["Set-Cookie"])

    def test_rate_limit_uses_forwarded_client(self):
        for _ in range(60):
            self.assertEqual(self.forwarded("/api/session").status_code, 200)
        self.assertEqual(self.forwarded("/api/session").status_code, 429)
        self.assertEqual(self.forwarded("/api/session", client_ip="192.168.20.10").status_code, 200)

    def test_cgnat_tailscale_range_is_supported(self):
        private_network("100.64.0.0/10")
        private_network("100.100.10.0/24")
        self.assertEqual(validate_bind_host("100.100.10.5"), "100.100.10.5")
        app = create_app({**self.config, "ALLOWED_NETWORKS": ["100.64.0.0/10"],
                          "TRUSTED_PROXIES": []})
        client = app.test_client()
        self.assertEqual(client.get("/", environ_overrides={"REMOTE_ADDR": "100.100.10.5"})
                         .status_code, 200)

    def test_trusted_proxies_must_be_private(self):
        for value in ["0.0.0.0/0", "8.8.8.0/24", "2001:4860::/32"]:
            with self.subTest(value=value), self.assertRaises(UploadConfigError):
                create_app({**self.config, "TRUSTED_PROXIES": [value]})

    def test_forwarded_helpers(self):
        trusted = [private_network("10.8.0.1/32")]
        self.assertEqual(str(forwarded_client(
            ipaddress.ip_address("10.8.0.1"),
            "192.168.20.9, 10.8.0.1", trusted)), "192.168.20.9")
        self.assertEqual(forwarded_origin(
            {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "chat.example.com:8443"},
            "http://internal"), "https://chat.example.com:8443")
        self.assertEqual(forwarded_origin(
            {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "[2001:db8::1]:8443"},
            "http://internal"), "https://[2001:db8::1]:8443")
        self.assertEqual(forwarded_origin(
            {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "chat.example.com:443"},
            "http://internal"), "https://chat.example.com")
        self.assertEqual(forwarded_origin(
            {"X-Forwarded-Proto": "http", "X-Forwarded-Host": "chat.example.com:80"},
            "http://internal"), "http://chat.example.com")
        self.assertEqual(forwarded_origin(
            {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "chat.example.com:99999"},
            "http://internal"), "http://internal")
        self.assertEqual(forwarded_origin(
            {"X-Forwarded-Proto": "https, http", "X-Forwarded-Host": "CHAT.Example.com"},
            "http://internal"), "https://chat.example.com")
        self.assertEqual(forwarded_origin({}, "http://internal"), "http://internal")

    def test_proxy_does_not_leak_session_cookie_from_another_site(self):
        # One central database means the session is valid at both sites; the cookie is
        # host-scoped by the browser, not duplicated by the gateway.
        csrf = self.session()
        response = self.forwarded(
            "/api/register", method="POST",
            headers={"Origin": "https://chat.congty.local",
                     "X-Forwarded-Proto": "https", "X-Forwarded-Host": "chat.congty.local",
                     "X-CSRF-Token": csrf},
            json={"username": "bob_proxy", "password": "password123"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.client.get_cookie(COOKIE))

    def test_waitress_does_not_strip_trusted_forwarded_headers(self):
        from waitress.server import create_server

        app = create_app({**self.config, "TRUSTED_PROXIES": ["127.0.0.1/32"]})
        server = create_server(app, host="127.0.0.1", port=0, threads=1,
                               clear_untrusted_proxy_headers=False)
        port = server.effective_port
        worker = threading.Thread(target=server.run, daemon=True)
        worker.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                headers={"X-Forwarded-For": "192.168.20.9"},
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                self.assertEqual(response.status, 200)
            direct = urllib.request.Request(f"http://127.0.0.1:{port}/")
            try:
                urllib.request.urlopen(direct, timeout=5)
            except urllib.error.HTTPError as error:
                self.assertEqual(error.code, 403)
                error.close()
            else:
                self.fail("Direct request without a trusted peer must be rejected")
        finally:
            server.close()
            worker.join(timeout=5)
        self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
