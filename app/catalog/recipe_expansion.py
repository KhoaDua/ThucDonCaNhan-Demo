"""Nạp lô 50 món Việt mô phỏng, không nạp giá hoặc thay đổi công thức cũ.

Kiểm tra trên bản sao trong bộ nhớ bằng bộ nhập chuẩn trước khi ghi một giao dịch.
Nguồn và trạng thái nguyên liệu chỉ phục vụ tính toán/truy xuất nội bộ.
"""
from __future__ import annotations

import csv
import io
import json
import math
import sqlite3
from pathlib import Path

from app.catalog import manage
from app.catalog.demo import DEMO_INGREDIENTS
from app.catalog.lookup import Catalog
from app.storage.db import now_iso, transaction

DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "recipe_expansion"
REQUIRED = {"energy_kcal", "protein_g", "fat_g", "carb_g", "fiber_g", "sodium_mg", "saturated_fat_g"}
TABLES = ("ingredient_info", "recipe", "recipe_ingredient", "recipe_active")


def read_payload(directory=DATA_DIR):
    root = Path(directory)
    return (json.loads((root / "recipes.json").read_text(encoding="utf-8")),
            json.loads((root / "ingredients.json").read_text(encoding="utf-8")))


def _csv(header, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


def _snapshot(conn):
    # Small catalogue tables only; never snapshot/copy user profiles or plans here.
    return {table: [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1,2,3")]
            for table in TABLES}


def load(conn, directory=DATA_DIR):
    payload, infos = read_payload(directory)
    recipes = payload["recipes"]
    if payload.get("dataset") != "demo" or len(recipes) != 50 or len(infos) != 14:
        raise ValueError("Lô mở rộng phải gồm 50 món và 14 nguyên liệu trong dataset demo")
    if len({r["recipe_id"] for r in recipes}) != 50 or len({i["fdc_id"] for i in infos}) != 14:
        raise ValueError("Trùng mã món hoặc mã nguyên liệu trong lô")
    expected = {fid: fields[3] for fid, fields in DEMO_INGREDIENTS.items()}
    expected.update({i["fdc_id"]: i["description"] for i in infos})
    cat = Catalog(conn, "demo")
    used = {i["fdc_id"] for r in recipes for i in r["ingredients"]}
    for fid in used:
        food = cat.get_food(fid)
        if fid not in expected or not food or not food["description"].startswith(expected[fid]):
            raise ValueError(f"Sai hoặc thiếu định danh USDA: {fid}")
        nutrients = cat.get_food_nutrients(fid)
        if REQUIRED - nutrients.keys() or any(not math.isfinite(float(nutrients[m]["amount_per_100g"]))
                                              or nutrients[m]["amount_per_100g"] < 0 for m in REQUIRED & nutrients.keys()):
            raise ValueError(f"Thiếu hoặc sai chỉ tiêu dinh dưỡng bắt buộc: {fid}")
    for rec in recipes:
        numbers = [rec["base_servings"], rec["prep_minutes"]] + [i["quantity_g_edible"] for i in rec["ingredients"]]
        if not all(math.isfinite(float(n)) and float(n) > 0 for n in numbers):
            raise ValueError(f"Định lượng không hợp lệ: {rec['recipe_id']}")
    source = payload["source_ref"]
    info_rows = [[i["fdc_id"], i["name_vi"], i["aliases"], i["avoid_tags"], "reviewed", source, i["version"]] for i in infos]
    rec_rows = [[r["recipe_id"], r["name_vi"], r["base_servings"], ";".join(r["meal_types"]),
                 r["prep_minutes"], source, "demo", r["version"]] for r in recipes]
    line_rows = [[r["recipe_id"], n, i["fdc_id"], i["quantity_g_edible"], expected[i["fdc_id"]],
                  "Gam phần ăn được đúng trạng thái USDA; không quy đổi sống/chín hoặc hao hụt nấu"]
                 for r in recipes for n, i in enumerate(r["ingredients"], 1)]
    before = _snapshot(conn)
    staged = sqlite3.connect(":memory:", isolation_level=None)
    staged.row_factory = sqlite3.Row
    try:
        conn.backup(staged)
        missing_infos = []
        for row in info_rows:
            old = staged.execute("SELECT * FROM ingredient_info WHERE fdc_id=? AND dataset='demo' ORDER BY version DESC LIMIT 1", (row[0],)).fetchone()
            if old:
                fields = ("fdc_id", "name_vi", "aliases", "avoid_tags", "review_status", "source_ref", "version")
                if [old[f] for f in fields] != row:
                    raise ValueError(f"Nguyên liệu {row[0]} đã có nội dung khác; không ghi đè")
            else:
                missing_infos.append(row)
        if missing_infos:
            ir = manage.import_ingredient_info(staged, _csv(manage.INFO_COLS, missing_infos), "demo")
            if ir.errors:
                raise ValueError(str(ir.errors))
        rr = manage.import_recipes(staged, _csv(manage.RECIPE_COLS, rec_rows),
                                   _csv(manage.LINE_COLS + ["conversion_source"], line_rows), "demo", expected)
        if rr.errors:
            raise ValueError(str(rr.errors))
        for r in recipes:
            old = staged.execute("SELECT * FROM recipe WHERE recipe_id=? AND version=?", (r["recipe_id"], r["version"])).fetchone()
            active = staged.execute("SELECT version FROM recipe_active WHERE recipe_id=? AND dataset='demo'", (r["recipe_id"],)).fetchone()
            if active and active["version"] != r["version"]:
                raise ValueError(f"{r['recipe_id']} đã có phiên bản hoạt động khác; không hạ phiên bản")
            if old["status"] != "approved" or not active:
                ok, message = manage.approve_recipe(staged, r["recipe_id"], r["version"], "Khoa Dứa cho phép công thức mô phỏng")
                if not ok:
                    raise ValueError(message)
        after = _snapshot(staged)
        # Importers validate data on the copy. Publish only the new rows, atomically.
        additions = {table: [row for row in after[table] if row not in set(before[table])] for table in TABLES}
        for table in TABLES:
            if any(row not in set(after[table]) for row in before[table]):
                raise ValueError(f"Lô nạp định thay đổi bản ghi có sẵn ở {table}")
        result = {"batch": payload["batch_id"], "accepted": rr.accepted, "unchanged": rr.unchanged,
                  "ingredients_added": len(missing_infos), "prices_added": 0}
        if any(additions.values()):
            with transaction(conn):
                if _snapshot(conn) != before:
                    raise ValueError("Danh mục vừa thay đổi ở phiên khác; hãy nạp lại")
                batch = conn.execute("INSERT INTO import_log(kind,dataset,created_at,status,report_json) VALUES(?,?,?,?,?)",
                                     ("recipe_expansion", "demo", now_iso(), "ok", json.dumps(result, ensure_ascii=False))).lastrowid
                for table in TABLES:
                    for values in additions[table]:
                        values = list(values)
                        if table == "recipe":
                            values[10] = batch
                        conn.execute(f"INSERT INTO {table} VALUES({','.join('?' for _ in values)})", values)
        return result
    finally:
        staged.close()
