"""Persistent accounts, opaque sessions, chat and request limits for the LAN web app."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

SESSION_SECONDS = 12 * 60 * 60


class Store:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL
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
            """)
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

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def ping(self) -> None:
        """Health check: mở DB và chạy một truy vấn tối thiểu."""
        with self.connect() as db:
            db.execute("SELECT 1").fetchone()

    def session(self, token: str):
        if not token or len(token) > 128:
            return None
        with self.connect() as db:
            row = db.execute(
                "SELECT s.*, u.username FROM sessions s LEFT JOIN users u ON u.id=s.user_id "
                "WHERE token_hash=? AND expires>?", (self.digest(token), time.time()),
            ).fetchone()
            return dict(row) if row else None

    def new_session(self, old_token: str, user_id: int | None = None):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<=? OR token_hash=?",
                       (time.time(), self.digest(old_token)))
            db.execute("INSERT INTO sessions VALUES (?, ?, ?, ?)",
                       (self.digest(token), user_id, csrf, time.time() + SESSION_SECONDS))
        return token, csrf

    def register(self, username: str, password: str) -> int:
        password_hash = generate_password_hash(password)
        with self.connect() as db:
            cur = db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                             (username, password_hash))
            return cur.lastrowid

    def authenticate(self, username: str, password: str):
        with self.connect() as db:
            row = db.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        valid = check_password_hash(row["password_hash"] if row else self.dummy_hash, password)
        return row["id"] if row and valid else None

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
            fields = "SELECT m.id, u.username, m.body, m.created FROM messages m "
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
