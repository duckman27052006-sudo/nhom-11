"""Persistent accounts, roles, moderation, opaque sessions and chat for the LAN web app."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

SESSION_SECONDS = 12 * 60 * 60
ADMIN = "admin"
MEMBER = "member"
ROLES = (ADMIN, MEMBER)
MAX_REASON = 160

# Columns added after the first release. ``Store`` upgrades an existing database in place,
# so a portal created before roles existed keeps its accounts, sessions and messages.
USER_COLUMNS = {
    "role": "TEXT NOT NULL DEFAULT 'member'",
    "created": "REAL NOT NULL DEFAULT 0",
    "banned": "INTEGER NOT NULL DEFAULT 0",
    "ban_until": "REAL",
    "ban_reason": "TEXT NOT NULL DEFAULT ''",
    "muted": "INTEGER NOT NULL DEFAULT 0",
    "mute_until": "REAL",
    "mute_reason": "TEXT NOT NULL DEFAULT ''",
}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'member',
    created REAL NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0,
    ban_until REAL, ban_reason TEXT NOT NULL DEFAULT '',
    muted INTEGER NOT NULL DEFAULT 0, mute_until REAL,
    mute_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER REFERENCES users(id),
    csrf TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL, created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS limits (
    key TEXT PRIMARY KEY, started REAL NOT NULL, count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS moderation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id INTEGER NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
    target_id INTEGER, target TEXT, detail TEXT NOT NULL DEFAULT '',
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    name TEXT PRIMARY KEY,
    uploader_id INTEGER NOT NULL REFERENCES users(id),
    uploaded REAL NOT NULL,
    size INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('chat_epoch', '0');
"""


class StoreError(ValueError):
    """A moderation request the store refuses; the message is safe to show to the user."""


class StoreNotFound(StoreError):
    """The requested account or message no longer exists."""


def is_active(flag, until: float | None, now: float | None = None) -> bool:
    """A timed flag is active until ``until``; ``None`` means "until lifted"."""
    if not flag:
        return False
    return until is None or until > (time.time() if now is None else now)


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.bootstrap_admin: str | None = None
        with self.connect() as db:
            db.executescript(SCHEMA)
            self._upgrade(db)
            self._ensure_admin(db)
        # Keep password checking work similar for unknown and known usernames.
        self.dummy_hash = generate_password_hash(secrets.token_urlsafe(32))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _upgrade(self, db):
        columns = {row["name"] for row in db.execute("PRAGMA table_info(users)")}
        for name, ddl in USER_COLUMNS.items():
            if name not in columns:
                db.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")

    def _ensure_admin(self, db):
        """Every database needs one administrator, including portals from before roles."""
        if db.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone():
            return
        row = db.execute("SELECT id, username FROM users ORDER BY id LIMIT 1").fetchone()
        if row:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (row["id"],))
            self.bootstrap_admin = row["username"]

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def ping(self) -> None:
        """Health check: mở DB và chạy một truy vấn tối thiểu."""
        with self.connect() as db:
            db.execute("SELECT 1").fetchone()

    def record_file(self, name: str, uploader_id: int, size: int) -> None:
        """Remember who uploaded a file; a re-upload refreshes the same row."""
        with self.connect() as db:
            db.execute(
                "INSERT INTO files (name, uploader_id, uploaded, size) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET uploader_id=excluded.uploader_id,"
                " uploaded=excluded.uploaded, size=excluded.size",
                (name, uploader_id, time.time(), int(size)),
            )

    def file_owner(self, name: str):
        """Upload record joined with the account name, or ``None`` when unrecorded."""
        with self.connect() as db:
            row = db.execute(
                "SELECT f.name, f.uploader_id, f.uploaded, f.size, u.username"
                " FROM files f LEFT JOIN users u ON u.id=f.uploader_id WHERE f.name=?",
                (name,),
            ).fetchone()
            return dict(row) if row else None

    def delete_file_record(self, name: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM files WHERE name=?", (name,))

    def list_records(self) -> list[dict]:
        """All upload records in name order; files without a row are simply absent."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT f.name, f.uploader_id, f.uploaded, f.size, u.username"
                " FROM files f LEFT JOIN users u ON u.id=f.uploader_id ORDER BY f.name"
            ).fetchall()
            return [dict(row) for row in rows]

    def session(self, token: str):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT s.user_id, s.csrf, s.expires, u.username, u.role, u.banned,"
                " u.ban_until, u.ban_reason, u.muted, u.mute_until, u.mute_reason, u.created"
                " FROM sessions s LEFT JOIN users u ON u.id=s.user_id"
                " WHERE token_hash=? AND expires>?", (self.digest(token), time.time()),
            ).fetchone()
            return dict(row) if row else None

    def drop_session(self, token: str):
        if not token:
            return
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (self.digest(token),))

    def new_session(self, old_token: str, user_id: int | None = None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<=? OR token_hash=?",
                       (time.time(), self.digest(old_token)))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                       (self.digest(token), user_id, csrf, time.time() + SESSION_SECONDS))
        return token, csrf

    def register(self, username: str, password: str) -> tuple[int, str]:
        password_hash = generate_password_hash(password)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None
            role = ADMIN if first else MEMBER
            cur = db.execute(
                "INSERT INTO users (username, password_hash, role, created) VALUES (?, ?, ?, ?)",
                (username, password_hash, role, time.time()),
            )
            return cur.lastrowid, role

    def authenticate(self, username: str, password: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        valid = check_password_hash(row["password_hash"] if row else self.dummy_hash, password)
        return row["id"] if row and valid else None

    def user(self, user_id: int):
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            return dict(row) if row else None

    def user_by_name(self, username: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            return dict(row) if row else None

    @staticmethod
    def state(account) -> dict:
        """Effective role and chat permissions for a session row or user row."""
        now = time.time()
        muted = is_active(account.get("muted"), account.get("mute_until"), now)
        banned = is_active(account.get("banned"), account.get("ban_until"), now)
        return {
            "role": account.get("role") or MEMBER,
            "muted": muted,
            "muted_until": account.get("mute_until") if muted else None,
            "mute_reason": account.get("mute_reason") or "",
            "banned": banned,
            "ban_until": account.get("ban_until") if banned else None,
            "ban_reason": account.get("ban_reason") or "",
        }

    def permit(self, key: str, maximum: int, seconds: int = 60) -> bool:
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM limits WHERE started<=?", (now - seconds,))
            db.execute("INSERT INTO limits VALUES (?, ?, 1) "
                       "ON CONFLICT(key) DO UPDATE SET count=count+1", (key, now))
            count = db.execute("SELECT count FROM limits WHERE key=?", (key,)).fetchone()[0]
            return count <= maximum

    def messages(self, after: int | None):
        with self.connect() as db:
            fields = ("SELECT m.id, m.user_id, m.body, m.created, u.username, u.role "
                      "FROM messages m ")
            fields += "JOIN users u ON u.id=m.user_id "
            if after is None:
                rows = list(reversed(db.execute(fields + "ORDER BY m.id DESC LIMIT 50").fetchall()))
            else:
                rows = db.execute(fields + "WHERE m.id>? ORDER BY m.id LIMIT 100", (after,))
            return [dict(row) for row in rows]

    def send(self, user_id: int, body: str) -> int:
        with self.connect() as db:
            return db.execute("INSERT INTO messages (user_id, body, created) VALUES (?, ?, ?)",
                              (user_id, body, time.time())).lastrowid

    def epoch(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key='chat_epoch'").fetchone()
            return int(row[0]) if row else 0

    @staticmethod
    def _bump_epoch(db):
        db.execute("UPDATE meta SET value=CAST(value AS INTEGER)+1 WHERE key='chat_epoch'")

    def users(self):
        now = time.time()
        with self.connect() as db:
            rows = db.execute(
                "SELECT u.id, u.username, u.role, u.created, u.banned, u.ban_until,"
                " u.ban_reason, u.muted, u.mute_until, u.mute_reason,"
                " COUNT(m.id) AS messages, MAX(m.created) AS last_message"
                " FROM users u LEFT JOIN messages m ON m.user_id=u.id"
                " GROUP BY u.id"
                " ORDER BY (u.role='admin') DESC, u.username COLLATE NOCASE"
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["banned"] = is_active(item["banned"], item["ban_until"], now)
            item["muted"] = is_active(item["muted"], item["mute_until"], now)
            if not item["banned"]:
                item["ban_until"], item["ban_reason"] = None, ""
            if not item["muted"]:
                item["mute_until"], item["mute_reason"] = None, ""
            items.append(item)
        return items

    def overview(self):
        users = self.users()
        with self.connect() as db:
            messages = db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            actions = [dict(row) for row in db.execute(
                "SELECT id, actor, action, target, target_id, detail, created"
                " FROM moderation ORDER BY id DESC LIMIT 24"
            )]
        return {
            "users": users,
            "actions": actions,
            "epoch": self.epoch(),
            "stats": {
                "users": len(users),
                "admins": sum(1 for user in users if user["role"] == ADMIN),
                "banned": sum(1 for user in users if user["banned"]),
                "muted": sum(1 for user in users if user["muted"]),
                "messages": messages,
            },
        }

    def set_role(self, user_id: int, role: str):
        if role not in ROLES:
            raise StoreError("Vai trò không hợp lệ.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, username, role, banned, ban_until, muted, mute_until"
                " FROM users WHERE id=?", (user_id,),
            ).fetchone()
            if row is None:
                raise StoreNotFound("Không tìm thấy tài khoản.")
            if row["role"] == role:
                return {"id": row["id"], "username": row["username"], "role": role,
                        "changed": False}
            if role == ADMIN and (is_active(row["banned"], row["ban_until"])
                                  or is_active(row["muted"], row["mute_until"])):
                raise StoreError("Hãy gỡ khóa và hạn chế chat trước khi cấp quyền quản trị.")
            if role == MEMBER:
                admins = db.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0]
                if admins <= 1:
                    raise StoreError("Không thể hạ quyền quản trị viên cuối cùng.")
            db.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            return {"id": row["id"], "username": row["username"], "role": role, "changed": True}

    def set_ban(self, user_id: int, banned: bool, minutes: int | None = None,
                reason: str = ""):
        reason = (reason or "").strip()[:MAX_REASON]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id, username, role FROM users WHERE id=?",
                             (user_id,)).fetchone()
            if row is None:
                raise StoreNotFound("Không tìm thấy tài khoản.")
            if row["role"] == ADMIN:
                raise StoreError("Hãy hạ quyền quản trị viên trước khi khóa tài khoản.")
            until = time.time() + minutes * 60 if banned and minutes else None
            db.execute(
                "UPDATE users SET banned=?, ban_until=?, ban_reason=? WHERE id=?",
                (int(banned), until if banned else None, reason if banned else "", user_id),
            )
            if banned:
                # A blocked account must lose every open session immediately.
                db.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return {"id": row["id"], "username": row["username"], "banned": banned,
                    "until": until, "reason": reason if banned else ""}

    def set_mute(self, user_id: int, muted: bool, minutes: int | None = None,
                 reason: str = ""):
        reason = (reason or "").strip()[:MAX_REASON]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id, username, role FROM users WHERE id=?",
                             (user_id,)).fetchone()
            if row is None:
                raise StoreNotFound("Không tìm thấy tài khoản.")
            if row["role"] == ADMIN:
                raise StoreError("Hãy hạ quyền quản trị viên trước khi hạn chế chat.")
            until = time.time() + minutes * 60 if muted and minutes else None
            db.execute(
                "UPDATE users SET muted=?, mute_until=?, mute_reason=? WHERE id=?",
                (int(muted), until if muted else None, reason if muted else "", user_id),
            )
            return {"id": row["id"], "username": row["username"], "muted": muted,
                    "until": until, "reason": reason if muted else ""}

    def delete_message(self, message_id: int, actor_id: int, *, admin: bool = False):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT m.id, m.user_id, u.username FROM messages m"
                " JOIN users u ON u.id=m.user_id WHERE m.id=?", (message_id,),
            ).fetchone()
            if row is None:
                raise StoreNotFound("Tin nhắn không còn tồn tại.")
            if not admin and row["user_id"] != actor_id:
                raise StoreError("Bạn chỉ có thể xóa tin nhắn của mình.")
            db.execute("DELETE FROM messages WHERE id=?", (message_id,))
            self._bump_epoch(db)
            return dict(row)

    def purge_messages(self, scope: str, user_id: int | None = None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            username = None
            if scope == "user":
                row = db.execute("SELECT username FROM users WHERE id=?",
                                 (user_id,)).fetchone()
                if row is None:
                    raise StoreNotFound("Không tìm thấy tài khoản.")
                username = row["username"]
                count = db.execute("DELETE FROM messages WHERE user_id=?", (user_id,)).rowcount
            elif scope == "all":
                count = db.execute("DELETE FROM messages").rowcount
            else:
                raise StoreError("Phạm vi xóa không hợp lệ.")
            if count:
                self._bump_epoch(db)
            return {"deleted": max(count, 0), "scope": scope, "username": username}

    def log(self, actor_id: int, actor: str, action: str, target_id: int | None,
            target: str | None, detail: str = ""):
        with self.connect() as db:
            db.execute(
                "INSERT INTO moderation (actor_id, actor, action, target_id, target, detail,"
                " created) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (actor_id, actor, action, target_id, target, detail[:MAX_REASON], time.time()),
            )
