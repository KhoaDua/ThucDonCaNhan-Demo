"""Kho phiên khách nhỏ; danh mục chỉ đọc được dùng chung trong từng tiến trình."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from functools import lru_cache
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import time

from fastapi import HTTPException

from app.storage.db import WORKSPACE_SCHEMA
from trial_runtime import Runtime, prepare_catalog


SESSION_SECONDS = 7 * 24 * 60 * 60
MAX_WORKSPACE_BYTES = 2 * 1024 * 1024
guest_database = ContextVar("guest_database", default=None)


@lru_cache(maxsize=1)
def _catalog_directory():
    temporary = TemporaryDirectory(prefix="ktht-catalog-")
    prepare_catalog(Runtime(Path(temporary.name), 0))
    return temporary


def current_guest_database():
    path = guest_database.get()
    if path is None:
        raise HTTPException(503, "Phiên dùng thử chưa sẵn sàng.")
    return path


def connect_guest(path):
    conn = sqlite3.connect(path, uri=True, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        seed = Path(_catalog_directory().name) / "catalog_seed.db"
        conn.execute("ATTACH DATABASE ? AS catalog", (seed.as_uri() + "?mode=ro&immutable=1",))
        return conn
    except BaseException:
        conn.close()
        raise


def initialize_workspace(path, snapshot):
    if snapshot is not None:
        path.write_bytes(snapshot)
    with closing(sqlite3.connect(path)) as conn:
        conn.executescript(WORKSPACE_SCHEMA)


class StorageUnavailable(Exception):
    """Lỗi kết nối với kho ngoài hoặc cấu hình chưa được cấp."""


class SessionStore:
    def __init__(self):
        self.url = os.environ.get("DATABASE_URL", "").strip()
        if self.url:
            import psycopg
            self.open_connection = lambda: psycopg.connect(
                self.url, connect_timeout=5, prepare_threshold=None,
            )
            self.error_type = psycopg.Error
        elif os.environ.get("VERCEL"):
            raise StorageUnavailable
        else:
            # Dùng SQLite chỉ khi chạy bản kiểm tra trên máy.
            directory = Path(os.environ.get("TRIAL_LOCAL_DATA", ".local-data")).resolve()
            directory.mkdir(parents=True, exist_ok=True)
            self.open_connection = lambda: sqlite3.connect(directory / "guest_sessions.db", timeout=5)
            self.error_type = sqlite3.Error
        with self.connection() as conn:
            if self.url:
                conn.execute("SELECT pg_advisory_xact_lock(215038510033741)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS public_guest_workspace (
                    guest_key TEXT PRIMARY KEY,
                    revision BIGINT NOT NULL,
                    expires_at BIGINT NOT NULL,
                    snapshot BYTEA NOT NULL
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS public_guest_expiry ON public_guest_workspace(expires_at)
            """)

    @contextmanager
    def connection(self):
        try:
            with closing(self.open_connection()) as conn, conn:
                yield conn
        except self.error_type:
            raise StorageUnavailable from None

    def query(self, conn, sql, *args):
        return conn.execute(sql.replace("?", "%s") if self.url else sql, args)

    def load(self, guest_key):
        with self.connection() as conn:
            row = self.query(conn, """
                SELECT revision, snapshot FROM public_guest_workspace
                WHERE guest_key = ? AND expires_at > ?
            """, guest_key, int(time.time())).fetchone()
        return (row[0], bytes(row[1])) if row else (0, None)

    def save(self, guest_key, revision, snapshot):
        now = int(time.time())
        expires = now + SESSION_SECONDS
        with self.connection() as conn:
            if revision:
                saved = self.query(conn, """
                    UPDATE public_guest_workspace SET revision = revision + 1,
                        expires_at = ?, snapshot = ?
                    WHERE guest_key = ? AND revision = ? AND expires_at > ?
                    RETURNING revision
                """, expires, snapshot, guest_key, revision, now).fetchone()
            else:
                saved = self.query(conn, """
                    INSERT INTO public_guest_workspace(guest_key, revision, expires_at, snapshot)
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(guest_key) DO UPDATE SET
                        revision = public_guest_workspace.revision + 1,
                        expires_at = excluded.expires_at, snapshot = excluded.snapshot
                    WHERE public_guest_workspace.expires_at <= ?
                    RETURNING revision
                """, guest_key, expires, snapshot, now).fetchone()
            # Xóa có giới hạn để dữ liệu thử hết hạn không tích lũy.
            self.query(conn, """
                DELETE FROM public_guest_workspace WHERE guest_key IN (
                    SELECT guest_key FROM public_guest_workspace WHERE expires_at <= ? LIMIT 50
                ) AND expires_at <= ?
            """, now, now)
        return saved is not None
