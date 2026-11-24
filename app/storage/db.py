"""Kết nối SQLite, lược đồ và giao dịch.

Mọi bảng danh mục đều có phiên bản (USDA theo `release_id`, công thức theo `version`,
giá theo `version`) để thực đơn cũ vẫn truy xuất được giá trị đã dùng.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);

-- ---------- USDA FoodData Central (dinh dưỡng nguyên liệu, trên 100 g) ----------
CREATE TABLE IF NOT EXISTS usda_release(
  release_id TEXT PRIMARY KEY, label TEXT, source_path TEXT, data_type TEXT,
  fingerprint TEXT, food_count INTEGER, nutrient_row_count INTEGER,
  imported_at TEXT, report_json TEXT);
CREATE TABLE IF NOT EXISTS usda_food(
  release_id TEXT, fdc_id INTEGER, description TEXT, data_type TEXT,
  food_category_id INTEGER, publication_date TEXT, ndb_number TEXT,
  PRIMARY KEY(release_id, fdc_id));
CREATE TABLE IF NOT EXISTS usda_nutrient(
  release_id TEXT, id INTEGER, name TEXT, unit_name TEXT, nutrient_nbr TEXT,
  PRIMARY KEY(release_id, id));
CREATE TABLE IF NOT EXISTS usda_food_nutrient(
  release_id TEXT, id INTEGER, fdc_id INTEGER, nutrient_id INTEGER, amount REAL,
  derivation_id INTEGER, PRIMARY KEY(release_id, fdc_id, nutrient_id));
CREATE TABLE IF NOT EXISTS usda_food_category(
  release_id TEXT, id INTEGER, code TEXT, description TEXT, PRIMARY KEY(release_id, id));
CREATE TABLE IF NOT EXISTS usda_food_portion(
  release_id TEXT, id INTEGER, fdc_id INTEGER, amount REAL, measure_unit_id INTEGER,
  modifier TEXT, gram_weight REAL, PRIMARY KEY(release_id, id));
CREATE TABLE IF NOT EXISTS usda_measure_unit(
  release_id TEXT, id INTEGER, name TEXT, PRIMARY KEY(release_id, id));

CREATE TABLE IF NOT EXISTS import_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, dataset TEXT, created_at TEXT,
  status TEXT, report_json TEXT);

-- ---------- Món ăn và công thức ----------
CREATE TABLE IF NOT EXISTS recipe(
  recipe_id TEXT, version INTEGER, dataset TEXT, name_vi TEXT, base_servings REAL,
  meal_types TEXT, prep_minutes REAL, source_ref TEXT, source_review_status TEXT,
  status TEXT, batch_id INTEGER, usda_release TEXT, imported_at TEXT, approved_at TEXT,
  PRIMARY KEY(recipe_id, version));
CREATE TABLE IF NOT EXISTS recipe_ingredient(
  recipe_id TEXT, version INTEGER, line_no INTEGER, fdc_id INTEGER,
  quantity_g_edible REAL, food_state TEXT, conversion_source TEXT,
  PRIMARY KEY(recipe_id, version, line_no));
CREATE TABLE IF NOT EXISTS recipe_active(
  recipe_id TEXT, dataset TEXT, version INTEGER, PRIMARY KEY(recipe_id, dataset));

-- ---------- Danh mục nguyên liệu do nhóm quản lý ----------
CREATE TABLE IF NOT EXISTS ingredient_info(
  fdc_id INTEGER, version INTEGER, dataset TEXT, name_vi TEXT, aliases TEXT,
  avoid_tags TEXT, review_status TEXT, source_ref TEXT, imported_at TEXT,
  PRIMARY KEY(fdc_id, dataset, version));

-- ---------- Giá và quy cách ----------
CREATE TABLE IF NOT EXISTS price_item(
  fdc_id INTEGER, dataset TEXT, version INTEGER, package_g REAL, price REAL,
  currency TEXT, mass_basis TEXT, source_ref TEXT, effective_date TEXT, imported_at TEXT,
  PRIMARY KEY(fdc_id, dataset, version));

-- ---------- Quy tắc Calories-dinh dưỡng ----------
CREATE TABLE IF NOT EXISTS nutrition_rule(
  rule_set TEXT, version INTEGER, metric TEXT, op TEXT, value REAL, unit TEXT,
  scope TEXT, audience TEXT, source_ref TEXT, review_status TEXT, label TEXT,
  PRIMARY KEY(rule_set, version, metric, op, scope));

CREATE TABLE IF NOT EXISTS health_policy(
  policy_id TEXT, version INTEGER, condition_id TEXT, mode TEXT,
  data_json TEXT NOT NULL,
  PRIMARY KEY(policy_id, version, mode));
"""

WORKSPACE_SCHEMA = """
-- ---------- Hồ sơ, thực đơn, phản hồi ----------
CREATE TABLE IF NOT EXISTS profile_version(
  profile_id INTEGER, version INTEGER, status TEXT, data_json TEXT,
  constraints_json TEXT, input_method TEXT, created_at TEXT, confirmed_at TEXT,
  PRIMARY KEY(profile_id, version));
CREATE TABLE IF NOT EXISTS plan_run(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER, profile_version INTEGER,
  request_token TEXT UNIQUE, status TEXT, detail_json TEXT, plan_id INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS plan(
  plan_id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER, profile_version INTEGER,
  dataset TEXT, current_version INTEGER, created_at TEXT);
CREATE TABLE IF NOT EXISTS plan_version(
  plan_id INTEGER, version INTEGER, status TEXT, items_json TEXT, report_json TEXT,
  shopping_json TEXT, context_json TEXT, change_note TEXT, created_at TEXT,
  PRIMARY KEY(plan_id, version));
CREATE TABLE IF NOT EXISTS plan_change(
  change_id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, base_version INTEGER,
  request_token TEXT UNIQUE, status TEXT, result_version INTEGER, detail_json TEXT,
  created_at TEXT);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER, recipe_id TEXT, liked INTEGER,
  cook_minutes REAL, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS ai_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT, action TEXT, ok INTEGER,
  detail_json TEXT, created_at TEXT);
"""
SCHEMA = CATALOG_SCHEMA + WORKSPACE_SCHEMA


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_db(conn)
    from app.storage.intake_cleanup import purge_legacy_intake, remove_unlisted_allergen
    purge_legacy_intake(conn)
    remove_unlisted_allergen(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Giao dịch ghi: tất cả hoặc không có gì."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
