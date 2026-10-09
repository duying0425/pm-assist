"""Hub 用户授权凭证持久化；仅保存当前凭证和旧刷新凭证的摘要。"""
from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class HubTokenStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # 先以私有权限创建文件，避免首次连接 SQLite 时凭证文件短暂可被其他用户读取。
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_tokens (
                    client_id TEXT NOT NULL,
                    open_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    refresh_expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (client_id, open_id)
                );
                CREATE TABLE IF NOT EXISTS refresh_aliases (
                    client_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    open_id TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (client_id, token_hash)
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _hash(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def save(self, client_id: str, grant: dict, previous_refresh_token: str = "") -> dict:
        values = {
            "client_id": client_id,
            "open_id": grant["open_id"],
            "name": str(grant.get("name", "")),
            "access_token": grant["access_token"],
            "refresh_token": str(grant.get("refresh_token", "")),
            "expires_at": float(grant["expires_at"]),
            "refresh_expires_at": float(grant.get("refresh_expires_at", 0)),
            "updated_at": time.time(),
        }
        if not values["open_id"] or not values["access_token"]:
            raise ValueError("用户标识和 access_token 不能为空")
        if not all(math.isfinite(values[k]) for k in ("expires_at", "refresh_expires_at")):
            raise ValueError("凭证到期时间无效")
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO user_tokens VALUES (
                    :client_id, :open_id, :name, :access_token, :refresh_token,
                    :expires_at, :refresh_expires_at, :updated_at
                ) ON CONFLICT(client_id, open_id) DO UPDATE SET
                    name=excluded.name, access_token=excluded.access_token,
                    refresh_token=excluded.refresh_token, expires_at=excluded.expires_at,
                    refresh_expires_at=excluded.refresh_expires_at, updated_at=excluded.updated_at
            """, values)
            if values["refresh_token"]:
                conn.execute("""
                    INSERT INTO refresh_aliases VALUES (?, ?, ?, ?)
                    ON CONFLICT(client_id, token_hash) DO UPDATE SET
                        open_id=excluded.open_id, expires_at=excluded.expires_at
                """, (client_id, self._hash(values["refresh_token"]), values["open_id"],
                      values["refresh_expires_at"]))
            if previous_refresh_token:
                # 兼容尚未迁移的旧客户端；已记录的旧凭证保持原到期时间。
                conn.execute("INSERT OR IGNORE INTO refresh_aliases VALUES (?, ?, ?, ?)",
                             (client_id, self._hash(previous_refresh_token), values["open_id"],
                              values["refresh_expires_at"]))
            conn.execute("DELETE FROM refresh_aliases WHERE expires_at <= ?", (time.time(),))
        return values

    def get(self, client_id: str, open_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM user_tokens WHERE client_id=? AND open_id=?",
                               (client_id, open_id)).fetchone()
        return dict(row) if row else None

    def list_accounts(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT client_id, open_id, name, expires_at, refresh_expires_at
                FROM user_tokens ORDER BY client_id, open_id
            """).fetchall()
        return [dict(row) for row in rows]

    def find_refresh(self, client_id: str, refresh_token: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT t.* FROM refresh_aliases a JOIN user_tokens t
                ON t.client_id=a.client_id AND t.open_id=a.open_id
                WHERE a.client_id=? AND a.token_hash=? AND a.expires_at>?
            """, (client_id, self._hash(refresh_token), time.time())).fetchone()
        return dict(row) if row else None

    def invalidate_refresh(self, client_id: str, open_id: str):
        with self._connect() as conn:
            conn.execute("UPDATE user_tokens SET refresh_expires_at=0 WHERE client_id=? AND open_id=?",
                         (client_id, open_id))
