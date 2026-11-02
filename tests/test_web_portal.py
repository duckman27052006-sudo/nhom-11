"""HTTP auth/chat and real gateway -> TFTP integration (no external services)."""

from __future__ import annotations

import io
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from tftp.web_upload import COOKIE, create_app, main, private_network
except ModuleNotFoundError as exc:
    if exc.name not in {"flask", "werkzeug"}:
        raise
    create_app = None

from tests.test_e2e import ServerHarness


@unittest.skipIf(create_app is None, 'Install web extras: pip install -e ".[web]"')
class WebTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.config = dict(TESTING=True, FILE_ROOT=base / "files", DATA_DIR=base / "data",
                           MAX_FILE_SIZE=1024, TFTP_TIMEOUT=1, TFTP_RETRIES=1)
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.store = self.app.extensions["web_store"]
        self.csrf = self.client.get("/api/session").json["csrf"]

    def post(self, path, body, *, client=None, csrf=None):
        return (client or self.client).post(path, json=body,
                                           headers={"X-CSRF-Token": csrf or self.csrf,
                                                    "Origin": "http://localhost"})

    def register(self, username="alice", client=None, csrf=None):
        response = self.post("/api/register", {"username": username, "password": "password123"},
                             client=client, csrf=csrf)
        self.assertEqual(response.status_code, 200, response.json)
        if client is None:
            self.csrf = response.json["csrf"]
        return response.json["csrf"]

    def test_page_and_assets(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"portal.js", response.data)
        for name in ["portal.js", "portal.css"]:
            with self.client.get("/web_static/" + name) as asset:
                self.assertEqual(asset.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])

    def test_guest_cannot_access_files_or_chat(self):
        for path in ["/api/files", "/api/files/hello.txt", "/api/messages"]:
            self.assertEqual(self.client.get(path).status_code, 401)
        for path in ["/api/files", "/api/messages"]:
            self.assertEqual(self.post(path, {}).status_code, 401)

    def test_register_rotation_cookie_and_hash(self):
        old = self.client.get_cookie(COOKIE).value
        self.register()
        cookie = self.client.get_cookie(COOKIE)
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, "Lax")
        self.assertNotEqual(cookie.value, old)
        self.assertIsNone(self.store.session(old))
        with self.store.connect() as db:
            password = db.execute("SELECT password_hash FROM users").fetchone()[0]
            tokens = [row[0] for row in db.execute("SELECT token_hash FROM sessions")]
        self.assertNotIn("password123", password)
        self.assertNotIn(cookie.value, tokens)
        self.assertEqual(self.client.get("/api/session").json["user"], "alice")

    def test_case_insensitive_duplicate(self):
        self.register()
        response = self.post("/api/register", {"username": "ALICE", "password": "password123"})
        self.assertEqual(response.status_code, 409)

    def test_registration_validation(self):
        for body in [{}, {"username": "ab", "password": "password123"},
                     {"username": "valid", "password": "short"},
                     {"username": [], "password": "password123"}, []]:
            with self.subTest(body=body):
                self.assertEqual(self.post("/api/register", body).status_code, 400)

    def test_login_logout_and_revocation(self):
        self.register()
        token = self.client.get_cookie(COOKIE).value
        self.csrf = self.post("/api/logout", {}).json["csrf"]
        self.assertIsNone(self.store.session(token))
        self.assertEqual(self.client.get("/api/files").status_code, 401)
        for username, password in [("alice", "wrongpass"), ("unknown", "password123")]:
            self.assertEqual(self.post("/api/login", dict(username=username, password=password))
                             .status_code, 401)
        response = self.post("/api/login", dict(username="ALICE", password="password123"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["user"], "alice")

    def test_session_expiry(self):
        self.register()
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
        self.assertEqual(self.client.get("/api/messages").status_code, 401)
        session = self.client.get("/api/session").json
        self.assertIsNone(session["user"])
        self.assertNotEqual(session["csrf"], self.csrf)

    def test_csrf_and_origin(self):
        self.register()
        self.assertEqual(self.client.post("/api/messages", json={"body": "hello"}).status_code, 403)
        self.assertEqual(self.client.post("/api/logout", headers={"X-CSRF-Token": "bad"})
                         .status_code, 403)
        response = self.client.post("/api/messages", json={"body": "hello"},
                                    headers={"X-CSRF-Token": self.csrf,
                                             "Origin": "http://other.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.post("/api/logout", headers={"X-CSRF-Token": "é"})
                         .status_code, 403)

    def test_network_and_spoofed_proxy(self):
        response = self.client.get("/", environ_overrides={"REMOTE_ADDR": "8.8.8.8"},
                                   headers={"X-Forwarded-For": "127.0.0.1"})
        self.assertEqual(response.status_code, 403)
        for network in ["0.0.0.0/0", "8.8.8.0/24", "::/0"]:
            with self.subTest(network=network), self.assertRaises(ValueError):
                private_network(network)
        for network in ["192.168.1.0/24", "10.0.0.0/8", "127.0.0.0/8", "fd12::/64"]:
            private_network(network)

    def test_unsafe_data_directory(self):
        with self.assertRaises(ValueError):
            create_app({**self.config, "DATA_DIR": self.config["FILE_ROOT"] / "secret"})

    def test_persistent_users_session_messages(self):
        self.register()
        self.post("/api/messages", {"body": "persisted"})
        token = self.client.get_cookie(COOKIE).value
        other = create_app(self.config).test_client()
        other.set_cookie(COOKIE, token)
        self.assertEqual(other.get("/api/session").json["user"], "alice")
        self.assertEqual(other.get("/api/messages").json["messages"][0]["body"], "persisted")

    def test_two_accounts_can_chat(self):
        self.register()
        other = self.app.test_client()
        other_csrf = other.get("/api/session").json["csrf"]
        other_csrf = self.register("bobby", client=other, csrf=other_csrf)
        first = self.post("/api/messages", {"body": "  Chào bạn <script>alert(1)</script>  "})
        self.assertEqual(first.status_code, 201)
        messages = other.get("/api/messages").json["messages"]
        self.assertEqual(messages[0]["username"], "alice")
        self.assertEqual(messages[0]["body"], "Chào bạn <script>alert(1)</script>")
        self.post("/api/messages", {"body": "Chào Alice"}, client=other, csrf=other_csrf)
        new = self.client.get(f"/api/messages?after={first.json['id']}").json["messages"]
        self.assertEqual(len(new), 1)
        self.assertEqual(new[0]["username"], "bobby")

    def test_chat_validation_limits_and_cursor(self):
        self.register()
        for body in [{"body": " "}, {"body": "a" * 2001}, {"body": 1}, []]:
            self.assertEqual(self.post("/api/messages", body).status_code, 400)
        for cursor in ["-1", "abc", "9" * 30]:
            self.assertEqual(self.client.get("/api/messages?after=" + cursor).status_code, 400)
        for _ in range(30):
            self.assertEqual(self.post("/api/messages", {"body": "hello"}).status_code, 201)
        self.assertEqual(self.post("/api/messages", {"body": "blocked"}).status_code, 429)
        self.assertEqual(self.client.get("/api/messages?after=0").json["messages"][0]["id"], 1)

    def test_chat_backlog_pagination(self):
        self.register()
        user_id = self.store.session(self.client.get_cookie(COOKIE).value)["user_id"]
        with self.store.connect() as db:
            db.executemany("INSERT INTO messages (user_id, body, created) VALUES (?, ?, ?)",
                           [(user_id, str(i), time.time()) for i in range(160)])
        initial = self.client.get("/api/messages").json["messages"]
        self.assertEqual(len(initial), 50)
        self.assertEqual(initial[0]["id"], 111)
        page = self.client.get("/api/messages?after=0").json["messages"]
        self.assertEqual(len(page), 100)
        self.assertEqual(page[-1]["id"], 100)

    def test_login_and_registration_rate_limits(self):
        for _ in range(10):
            self.assertEqual(self.post("/api/login", dict(username="nobody", password="badpass123"))
                             .status_code, 401)
        self.assertEqual(self.post("/api/login", {}).status_code, 429)
        for _ in range(5):
            self.post("/api/register", {})
        self.assertEqual(self.post("/api/register", {}).status_code, 429)
        with self.store.connect() as db:
            db.execute("UPDATE limits SET started=?", (time.time() - 65,))
        self.assertEqual(self.post("/api/login", {}).status_code, 401)

    def test_file_list_and_path_restrictions(self):
        self.register()
        root = self.config["FILE_ROOT"]
        (root / "hello.txt").write_bytes(b"hello")
        (root / "subdir").mkdir()
        listing = self.client.get("/api/files").json["files"]
        self.assertEqual([file["name"] for file in listing], ["hello.txt"])
        for name in ["..", "CON", "a:b", "foo%5Cbar", "trailing."]:
            self.assertEqual(self.client.get("/api/files/" + name).status_code, 400)
        self.assertEqual(self.client.get("/api/files/missing.txt").status_code, 404)

    def test_symlink_not_listed_or_downloaded(self):
        self.register()
        target = Path(self.temp.name) / "secret.txt"
        target.write_text("secret")
        link = self.config["FILE_ROOT"] / "linked.txt"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("OS does not allow creating symlinks")
        self.assertEqual(self.client.get("/api/files").json["files"], [])
        self.assertEqual(self.client.get("/api/files/linked.txt").status_code, 400)

    def test_real_tftp_upload_download_and_empty(self):
        self.register()
        with ServerHarness({"hello.txt": b"hello", "empty.bin": b""},
                           allow_overwrite=False) as server:
            token = self.client.get_cookie(COOKIE).value
            self.app = create_app({**self.config, "FILE_ROOT": server.root,
                                   "TFTP_PORT": server.port})
            self.client = self.app.test_client()
            self.client.set_cookie(COOKIE, token)
            for name, expected in [("hello.txt", b"hello"), ("empty.bin", b"")]:
                response = self.client.get("/api/files/" + name)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, expected)
                self.assertIn("attachment", response.headers["Content-Disposition"])
                response.close()
            response = self.client.post(
                "/api/files", data={"file": (io.BytesIO(b"uploaded"), "up.txt")},
                headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"})
            self.assertEqual(response.status_code, 201, response.json)
            self.assertEqual(server.read("up.txt"), b"uploaded")
            server.server.wait_for_idle()
            self.assertIn("up.txt", [item["name"] for item in
                                     self.client.get("/api/files").json["files"]])
            with self.assertLogs("tftp.web", level="ERROR"):
                duplicate = self.client.post(
                    "/api/files", data={"file": (io.BytesIO(b"replacement"), "up.txt")},
                    headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"})
            self.assertEqual(duplicate.status_code, 409)
            self.assertEqual(server.read("up.txt"), b"uploaded")
            vietnamese = self.client.post(
                "/api/files", data={"file": (io.BytesIO(b"vietnamese name"), "Đề tài.txt")},
                headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"})
            self.assertEqual(vietnamese.status_code, 201, vietnamese.json)
            self.assertEqual(vietnamese.json["filename"], "De_tai.txt")
            self.assertEqual(server.read("De_tai.txt"), b"vietnamese name")
            with self.client.get("/api/files/De_tai.txt") as downloaded:
                self.assertEqual(downloaded.data, b"vietnamese name")
        self.assertEqual(list((self.config["DATA_DIR"] / "temporary").iterdir()), [])

    def test_download_cleanup_when_closed_before_consuming(self):
        self.register()
        (self.config["FILE_ROOT"] / "data.txt").write_bytes(b"x")

        def download(remote, local, **kwargs):
            Path(local).write_bytes(b"payload")

        with patch("tftp.web_upload.TftpClient.download", side_effect=download):
            for _ in range(6):
                response = self.client.get("/api/files/data.txt", buffered=False)
                self.assertEqual(response.status_code, 200)
                response.close()
        self.assertEqual(list((self.config["DATA_DIR"] / "temporary").iterdir()), [])

    def test_transfer_capacity_released_after_disconnect(self):
        self.register()
        (self.config["FILE_ROOT"] / "data.txt").write_bytes(b"x")

        def download(remote, local, **kwargs):
            Path(local).write_bytes(b"payload")

        with patch("tftp.web_upload.TftpClient.download", side_effect=download):
            responses = []
            try:
                for _ in range(4):
                    responses.append(self.client.get("/api/files/data.txt", buffered=False))
                    self.assertEqual(responses[-1].status_code, 200)
                self.assertEqual(self.client.get("/api/files/data.txt").status_code, 503)
                responses.pop().close()
                responses.append(self.client.get("/api/files/data.txt", buffered=False))
                self.assertEqual(responses[-1].status_code, 200)
            finally:
                for response in responses:
                    response.close()
        self.assertEqual(list((self.config["DATA_DIR"] / "temporary").iterdir()), [])

    def test_download_temporary_directory_failure_releases_capacity(self):
        self.register()
        (self.config["FILE_ROOT"] / "data.txt").write_bytes(b"x")
        with patch("tftp.web_upload.tempfile.TemporaryDirectory", side_effect=OSError("disk full")):
            with self.assertLogs("tftp.web", level="ERROR"):
                for _ in range(5):
                    self.assertEqual(self.client.get("/api/files/data.txt").status_code, 502)

    def test_small_json_body_limit(self):
        self.register()
        response = self.post("/api/messages", {"body": "x" * 20000})
        self.assertEqual(response.status_code, 413)

    def test_mocked_symlink_is_hidden_even_without_os_privilege(self):
        self.register()
        (self.config["FILE_ROOT"] / "linked.txt").write_text("secret")
        with patch("tftp.web_upload.Path.is_symlink", return_value=True):
            self.assertEqual(self.client.get("/api/files").json["files"], [])
            self.assertEqual(self.client.get("/api/files/linked.txt").status_code, 400)

    def test_transfer_size_limits_and_cleanup(self):
        self.register()
        (self.config["FILE_ROOT"] / "large.txt").write_bytes(b"x" * 1025)
        self.assertEqual(self.client.get("/api/files/large.txt").status_code, 413)
        (self.config["FILE_ROOT"] / "large.txt").write_bytes(b"x")

        def too_large(remote, local, *, progress):
            Path(local).write_bytes(b"x" * 1025)
            progress(1025, 1025)

        with patch("tftp.web_upload.TftpClient.download", side_effect=too_large):
            self.assertEqual(self.client.get("/api/files/large.txt").status_code, 413)
        for payload in [b"x" * 1025, b"x" * 70000]:
            response = self.client.post(
                "/api/files", data={"file": (io.BytesIO(payload), "large.txt")},
                headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"})
            self.assertEqual(response.status_code, 413)
        self.assertEqual(list((self.config["DATA_DIR"] / "temporary").iterdir()), [])

    def test_tftp_failure_is_reported_and_cleaned(self):
        self.register()
        (self.config["FILE_ROOT"] / "hello.txt").write_bytes(b"hello")
        from tftp.tftp_errors import TftpTimeout
        with patch("tftp.web_upload.TftpClient.download", side_effect=TftpTimeout()):
            with self.assertLogs("tftp.web", level="ERROR"):
                response = self.client.get("/api/files/hello.txt")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(self.temp.name, response.get_data(as_text=True))
        self.assertEqual(list((self.config["DATA_DIR"] / "temporary").iterdir()), [])

    def test_upload_validation(self):
        self.register()
        self.assertEqual(self.post("/api/files", {}).status_code, 400)
        for name in ["../escape.txt", "CON", "C:\\file.txt"]:
            response = self.client.post("/api/files", data={"file": (io.BytesIO(b"x"), name)},
                                        headers={"X-CSRF-Token": self.csrf, "Origin": "http://localhost"})
            self.assertEqual(response.status_code, 400)

    def test_cli_rejects_public_wildcard_and_missing_allowlist(self):
        for args in [["--host", "0.0.0.0"], ["--host", "8.8.8.8"],
                     ["--host", "192.168.1.10"], ["--max-file-size", "0"],
                     ["--tftp-server", "[::1]:70000"]]:
            with self.subTest(args=args), patch("sys.stderr", new=io.StringIO()):
                with self.assertRaises(SystemExit) as exc:
                    main(args)
                self.assertEqual(exc.exception.code, 2)

    def test_database_error_hides_details(self):
        with patch.object(self.store, "session", side_effect=sqlite3.OperationalError("secret")):
            with self.assertLogs("tftp.web", level="ERROR"):
                response = self.client.get("/api/session")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"secret", response.data)


if __name__ == "__main__":
    unittest.main()
