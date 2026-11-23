"""Admin roles, bans, chat restrictions and moderation history for the LAN portal."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from werkzeug.security import generate_password_hash

try:
    from tftp.web_store import SESSION_SECONDS, Store, StoreError
    from tftp.web_upload import create_app
except ModuleNotFoundError as exc:
    if exc.name not in {"flask", "werkzeug"}:
        raise
    create_app = None


@unittest.skipIf(create_app is None, 'Install web extras: pip install -e ".[web]"')
class RoleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.app = create_app(dict(TESTING=True, FILE_ROOT=base / "files",
                                   DATA_DIR=base / "data", MAX_FILE_SIZE=1024))
        self.store = self.app.extensions["web_store"]
        self.admin, self.admin_csrf = self.signup("alice")
        self.member, self.member_csrf = self.signup("bob")

    def signup(self, username: str, password: str = "password123"):
        client = self.app.test_client()
        csrf = client.get("/api/session").json["csrf"]
        response = self.post(client, csrf, "/api/register",
                             {"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.json)
        return client, response.json["csrf"]

    def post(self, client, csrf, path, body):
        return client.post(path, json=body, headers={"X-CSRF-Token": csrf,
                                                     "Origin": "http://localhost"})

    def delete(self, client, csrf, path):
        return client.delete(path, headers={"X-CSRF-Token": csrf,
                                             "Origin": "http://localhost"})

    def login(self, client, username, password: str = "password123"):
        # A revoked session makes the browser fetch a new anonymous CSRF token first.
        csrf = client.get("/api/session").json["csrf"]
        return self.post(client, csrf, "/api/login",
                         {"username": username, "password": password})

    def overview(self, client):
        response = client.get("/api/admin/overview")
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def user_id(self, username: str) -> int:
        account = self.store.user_by_name(username)
        self.assertIsNotNone(account)
        return account["id"]

    def test_first_account_is_admin_and_later_accounts_are_members(self):
        admin = self.admin.get("/api/session").json
        member = self.member.get("/api/session").json
        self.assertEqual(admin["user"], "alice")
        self.assertEqual(admin["role"], "admin")
        self.assertTrue(admin["can_moderate"])
        self.assertIsInstance(admin["id"], int)
        self.assertEqual(member["role"], "member")
        self.assertFalse(member["can_moderate"])
        self.assertFalse(member["chat"]["muted"])

    def test_members_cannot_use_moderation_endpoints(self):
        self.assertEqual(self.member.get("/api/admin/overview").status_code, 403)
        self.assertEqual(self.post(self.member, self.member_csrf,
                                   f"/api/admin/users/{self.user_id('alice')}/ban",
                                   {"active": True}).status_code, 403)
        self.assertEqual(self.post(self.member, self.member_csrf,
                                   f"/api/admin/users/{self.user_id('bob')}/mute",
                                   {"active": True}).status_code, 403)
        self.assertEqual(self.post(self.member, self.member_csrf,
                                   f"/api/admin/users/{self.user_id('bob')}/role",
                                   {"role": "admin"}).status_code, 403)
        message = self.post(self.member, self.member_csrf, "/api/messages",
                            {"body": "hello"}).json
        self.assertEqual(self.delete(self.member, self.member_csrf,
                                     f"/api/messages/{message['id']}").status_code, 200)
        self.assertEqual(self.post(self.member, self.member_csrf, "/api/messages",
                                   {"body": "again"}).status_code, 201)
        self.assertEqual(self.delete(self.member, self.member_csrf,
                                     "/api/messages").status_code, 403)

    def test_admin_overview_lists_accounts_and_stats(self):
        data = self.overview(self.admin)
        self.assertEqual(data["stats"]["users"], 2)
        self.assertEqual(data["stats"]["admins"], 1)
        self.assertEqual(data["stats"]["banned"], 0)
        self.assertEqual(data["stats"]["muted"], 0)
        usernames = [user["username"] for user in data["users"]]
        self.assertEqual(usernames, ["alice", "bob"])
        self.assertEqual(data["users"][0]["role"], "admin")
        self.assertEqual(data["me"], self.user_id("alice"))

    def test_ban_revokes_sessions_and_blocks_login(self):
        self.assertEqual(self.post(self.member, self.member_csrf, "/api/messages",
                                   {"body": "before ban"}).status_code, 201)
        response = self.post(self.admin, self.admin_csrf,
                             f"/api/admin/users/{self.user_id('bob')}/ban",
                             {"active": True, "minutes": 60, "reason": "spam"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertEqual(response.json["username"], "bob")
        self.assertTrue(response.json["banned"])
        self.assertIn("spam", response.json["message"])
        # The open session is gone, so the cookie now behaves like a guest.
        self.assertEqual(self.member.get("/api/files").status_code, 401)
        self.assertEqual(self.member.get("/api/messages").status_code, 401)
        blocked = self.login(self.member, "bob")
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("khóa", blocked.json["error"])
        self.assertIn("spam", blocked.json["error"])
        self.assertTrue(self.overview(self.admin)["users"][1]["banned"])
        self.assertEqual(self.overview(self.admin)["stats"]["banned"], 1)

    def test_unban_restores_login(self):
        target = self.user_id("bob")
        self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/ban",
                  {"active": True})
        self.assertEqual(self.login(self.member, "bob").status_code, 403)
        response = self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/ban",
                             {"active": False})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertFalse(response.json["banned"])
        restored = self.login(self.member, "bob")
        self.assertEqual(restored.status_code, 200, restored.json)
        self.member_csrf = restored.json["csrf"]

    def test_timed_ban_expires_without_manual_action(self):
        target = self.user_id("bob")
        self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/ban",
                  {"active": True, "minutes": 1})
        with self.store.connect() as db:
            db.execute("UPDATE users SET ban_until=? WHERE id=?", (time.time() - 1, target))
        restored = self.login(self.member, "bob")
        self.assertEqual(restored.status_code, 200, restored.json)
        self.member_csrf = restored.json["csrf"]
        self.assertFalse(self.overview(self.admin)["users"][1]["banned"])

    def test_mute_keeps_read_access_but_blocks_chat(self):
        target = self.user_id("bob")
        response = self.post(self.admin, self.admin_csrf,
                             f"/api/admin/users/{target}/mute",
                             {"active": True, "minutes": 1440, "reason": "flood"})
        self.assertEqual(response.status_code, 200, response.json)
        self.assertIn("1 ngày", response.json["message"])
        self.assertEqual(self.member.get("/api/messages").status_code, 200)
        blocked = self.post(self.member, self.member_csrf, "/api/messages",
                            {"body": "still here?"})
        self.assertEqual(blocked.status_code, 403)
        self.assertIn("hạn chế", blocked.json["error"])
        self.assertIn("flood", blocked.json["error"])
        # The restriction is visible in the session payload for the banner in the portal.
        self.assertTrue(self.member.get("/api/session").json["chat"]["muted"])
        unblocked = self.post(self.admin, self.admin_csrf,
                              f"/api/admin/users/{target}/mute", {"active": False})
        self.assertEqual(unblocked.status_code, 200, unblocked.json)
        self.assertEqual(self.post(self.member, self.member_csrf, "/api/messages",
                                   {"body": "back"}).status_code, 201)

    def test_restricted_account_cannot_be_promoted(self):
        target = self.user_id("bob")
        self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/mute",
                  {"active": True, "minutes": 60})
        response = self.post(self.admin, self.admin_csrf,
                             f"/api/admin/users/{target}/role", {"role": "admin"})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn("hạn chế", response.json["error"])
        self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/mute",
                  {"active": False})
        self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/ban",
                  {"active": True, "minutes": 60})
        response = self.post(self.admin, self.admin_csrf,
                             f"/api/admin/users/{target}/role", {"role": "admin"})
        self.assertEqual(response.status_code, 400, response.json)
        self.assertIn("khóa", response.json["error"])

    def test_admins_must_be_demoted_before_moderation(self):
        target = self.user_id("bob")
        self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/role",
                  {"role": "admin"})
        for action in ("ban", "mute"):
            response = self.post(self.admin, self.admin_csrf,
                                 f"/api/admin/users/{target}/{action}", {"active": True})
            self.assertEqual(response.status_code, 400, response.json)
            self.assertIn("hạ quyền", response.json["error"])

    def test_role_promotion_and_demotion(self):
        target = self.user_id("bob")
        promoted = self.post(self.admin, self.admin_csrf,
                             f"/api/admin/users/{target}/role", {"role": "admin"})
        self.assertEqual(promoted.status_code, 200, promoted.json)
        self.assertTrue(promoted.json["changed"])
        self.assertEqual(self.member.get("/api/admin/overview").status_code, 200)
        self.assertEqual(self.member.get("/api/session").json["role"], "admin")
        # Neither administrator may act on their own account.
        self.assertEqual(self.post(self.admin, self.admin_csrf,
                                   f"/api/admin/users/{self.user_id('alice')}/role",
                                   {"role": "member"}).status_code, 400)
        demoted = self.post(self.member, self.member_csrf,
                            f"/api/admin/users/{self.user_id('alice')}/role",
                            {"role": "member"})
        self.assertEqual(demoted.status_code, 200, demoted.json)
        self.assertEqual(self.admin.get("/api/session").json["role"], "member")
        self.assertEqual(self.admin.get("/api/admin/overview").status_code, 403)
        with self.assertRaises(StoreError):
            self.store.set_role(target, "member")

    def test_reason_and_duration_validation(self):
        target = self.user_id("bob")
        for body in ({}, {"active": True, "minutes": 0}, {"active": True, "minutes": 20000},
                     {"active": True, "minutes": "60"}, {"active": True, "reason": "x" * 200},
                     {"active": "yes"}):
            with self.subTest(body=body):
                self.assertEqual(self.post(self.admin, self.admin_csrf,
                                           f"/api/admin/users/{target}/ban", body)
                                 .status_code, 400)
        self.assertEqual(self.post(self.admin, self.admin_csrf,
                                   f"/api/admin/users/{target}/role", {"role": "root"})
                         .status_code, 400)
        self.assertEqual(self.post(self.admin, self.admin_csrf,
                                   "/api/admin/users/9999/ban", {"active": True})
                         .status_code, 404)

    def test_message_deletion_rights_and_epoch_bump(self):
        message = self.post(self.member, self.member_csrf, "/api/messages",
                            {"body": "delete me"}).json
        other, other_csrf = self.signup("carol")
        self.assertEqual(self.delete(other, other_csrf,
                                     f"/api/messages/{message['id']}").status_code, 403)
        before = self.member.get("/api/messages").json["epoch"]
        self.assertEqual(self.delete(self.admin, self.admin_csrf,
                                     f"/api/messages/{message['id']}").status_code, 200)
        self.assertEqual(self.member.get("/api/messages").json["messages"], [])
        after = self.member.get("/api/messages").json["epoch"]
        self.assertGreater(after, before)
        # A stale cursor from before the deletion triggers a full refresh.
        stale = self.member.get(f"/api/messages?after={message['id']}&epoch={before}").json
        self.assertTrue(stale["reset"])
        self.assertEqual(stale["messages"], [])
        self.assertEqual(self.delete(self.admin, self.admin_csrf,
                                     f"/api/messages/{message['id']}").status_code, 404)

    def test_clear_all_and_per_member_history(self):
        for body in ("một", "hai", "ba"):
            self.post(self.member, self.member_csrf, "/api/messages", {"body": body})
        self.post(self.admin, self.admin_csrf, "/api/messages", {"body": "alice"})
        target = self.user_id("bob")
        cleared = self.delete(self.admin, self.admin_csrf,
                              f"/api/messages?scope=user&user_id={target}")
        self.assertEqual(cleared.status_code, 200, cleared.json)
        self.assertEqual(cleared.json["deleted"], 3)
        remaining = self.admin.get("/api/messages").json["messages"]
        self.assertEqual([item["body"] for item in remaining], ["alice"])
        cleared = self.delete(self.admin, self.admin_csrf, "/api/messages?scope=all")
        self.assertEqual(cleared.status_code, 200, cleared.json)
        self.assertEqual(cleared.json["deleted"], 1)
        self.assertEqual(self.admin.get("/api/messages").json["messages"], [])
        self.assertEqual(self.overview(self.admin)["stats"]["messages"], 0)

    def test_moderation_history_records_actions(self):
        target = self.user_id("bob")
        self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/ban",
                  {"active": True, "minutes": 60, "reason": "spam"})
        self.post(self.admin, self.admin_csrf, f"/api/admin/users/{target}/mute",
                  {"active": True, "minutes": 60, "reason": "flood"})
        actions = self.overview(self.admin)["actions"]
        self.assertEqual([action["action"] for action in actions[:2]], ["mute", "ban"])
        self.assertEqual(actions[1]["actor"], "alice")
        self.assertEqual(actions[1]["target"], "bob")
        self.assertIn("spam", actions[1]["detail"])

    def test_banned_session_is_cleared_on_next_request(self):
        target = self.user_id("bob")
        with self.store.connect() as db:
            db.execute("UPDATE users SET banned=1, ban_reason='test' WHERE id=?", (target,))
        response = self.member.get("/api/files")
        self.assertEqual(response.status_code, 403)
        self.assertIn("khóa", response.json["error"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?",
                                        (target,)).fetchone()[0], 0)

    def test_legacy_database_is_upgraded_and_keeps_content(self):
        path = Path(self.temp.name) / "legacy" / "portal.sqlite3"
        path.parent.mkdir()
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE users (id INTEGER PRIMARY KEY,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                password_hash TEXT NOT NULL);
            CREATE TABLE sessions (token_hash TEXT PRIMARY KEY,
                user_id INTEGER REFERENCES users(id), csrf TEXT NOT NULL,
                expires REAL NOT NULL);
            CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id),
                body TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE limits (key TEXT PRIMARY KEY, started REAL NOT NULL,
                count INTEGER NOT NULL);
        """)
        user_id = db.execute(
            "INSERT INTO users (username, password_hash) VALUES (?, ?)",
            ("legacy", generate_password_hash("password123")),
        ).lastrowid
        db.execute("INSERT INTO messages (user_id, body, created) VALUES (?, ?, ?)",
                   (user_id, "tin cũ", time.time()))
        db.commit()
        db.close()
        store = Store(path)
        self.assertEqual(store.bootstrap_admin, "legacy")
        self.assertEqual(store.user_by_name("legacy")["role"], "admin")
        self.assertEqual([item["body"] for item in store.messages(None)], ["tin cũ"])
        self.assertEqual(store.epoch(), 0)
        with store.connect() as check:
            columns = {row["name"] for row in check.execute("PRAGMA table_info(users)")}
        self.assertTrue({"role", "banned", "muted", "created"} <= columns)
        self.assertEqual(SESSION_SECONDS, 12 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
