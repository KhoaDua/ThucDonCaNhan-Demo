"""Mỗi yêu cầu có một kho SQLite trong RAM và đọc cùng danh mục mẫu."""
from contextlib import closing, contextmanager
from functools import lru_cache
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Lock

from app.storage.db import WORKSPACE_SCHEMA
from trial_runtime import Runtime, prepare_catalog


_catalog_lock = Lock()


@lru_cache(maxsize=1)
def _catalog_directory():
    directory = TemporaryDirectory(prefix="ktht-catalog-")
    try:
        prepare_catalog(Runtime(Path(directory.name), 0))
    except Exception:
        directory.cleanup()
        raise
    return directory


@contextmanager
def demo_workspace():
    # Chỉ bản sao danh mục mẫu được ghi vào thư mục tạm.
    with _catalog_lock:
        seed = Path(_catalog_directory().name) / "catalog_seed.db"

    with closing(sqlite3.connect(":memory:", uri=True, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ? AS catalog", (seed.as_uri() + "?mode=ro&immutable=1",))
        conn.executescript(WORKSPACE_SCHEMA)
        yield conn
