"""Quản trị danh mục (FR-04): nhập công thức, danh mục nguyên liệu, giá/quy cách; duyệt và phiên bản.

Quy tắc:
- Công thức nhập vào ở trạng thái `pending_review`. Chỉ sau bước duyệt (`approve_recipe`) mới
  trở thành phiên bản hoạt động trong bộ dữ liệu (`recipe_active`) và được dùng để lập thực đơn.
- Không ghi đè một phiên bản đã có bằng nội dung khác: phải tăng `version`.
- Bản ghi sai được báo lỗi cụ thể (tệp, dòng, trường) và không được đưa vào danh mục.
- Dữ liệu demo luôn mang `dataset = "demo"`, tách khỏi `operational`.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass, field

from app.storage.db import get_meta, now_iso, transaction

DATASETS = ("operational", "demo")
MEAL_ALIASES = {"breakfast": "breakfast", "sáng": "breakfast", "bữa sáng": "breakfast",
                "lunch": "lunch", "trưa": "lunch", "bữa trưa": "lunch",
                "dinner": "dinner", "tối": "dinner", "bữa tối": "dinner"}
RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
RECIPE_COLS = ["recipe_id", "name_vi", "base_servings", "meal_types", "prep_minutes", "source_ref", "review_status", "version"]
LINE_COLS = ["recipe_id", "line_no", "fdc_id", "quantity_g_edible", "food_state"]
INFO_COLS = ["fdc_id", "name_vi", "aliases", "avoid_tags", "review_status", "source_ref", "version"]
PRICE_COLS = ["fdc_id", "package_g", "price", "currency", "mass_basis", "source_ref", "effective_date", "version"]


@dataclass
class BatchReport:
    kind: str
    dataset: str
    batch_id: int | None = None
    status: str = "ok"
    accepted: list = field(default_factory=list)
    unchanged: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def to_dict(self):
        return self.__dict__.copy()


def _read_csv(text: str, required: list[str], fname: str, rep: BatchReport):
    if text.startswith("﻿"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    header = [h.strip() for h in (reader.fieldnames or [])]
    missing = [c for c in required if c not in header]
    if missing:
        rep.errors.append({"file": fname, "line": 1, "field": ",".join(missing), "message": f"Thiếu cột: {', '.join(missing)}"})
        rep.status = "failed"
        return None
    rows = []
    for i, row in enumerate(reader, start=2):
        rows.append((i, {(k or "").strip(): (v or "").strip() for k, v in row.items()}))
    return rows


def _num(v: str):
    v = (v or "").strip().replace(",", ".")
    if v == "":
        return None
    return float(v)


def active_release(conn) -> str | None:
    return get_meta(conn, "active_usda_release")


def _food_desc(conn, release, fdc_id):
    row = conn.execute("SELECT description FROM usda_food WHERE release_id=? AND fdc_id=?", (release, fdc_id)).fetchone()
    return row["description"] if row else None


def _new_batch(conn, kind, dataset):
    cur = conn.execute("INSERT INTO import_log(kind, dataset, created_at, status, report_json) VALUES(?,?,?,?,?)",
                       (kind, dataset, now_iso(), "running", "{}"))
    return cur.lastrowid


def _finish_batch(conn, rep: BatchReport):
    if rep.status != "failed":
        rep.status = "ok" if not rep.rejected and not rep.errors else ("partial" if rep.accepted or rep.unchanged else "failed")
    conn.execute("UPDATE import_log SET status=?, report_json=? WHERE id=?",
                 (rep.status, json.dumps(rep.to_dict(), ensure_ascii=False), rep.batch_id))


# ---------------------------------------------------------------- công thức

def import_recipes(conn, recipes_text: str, ingredients_text: str, dataset: str = "operational",
                   expected_desc: dict[int, str] | None = None) -> BatchReport:
    rep = BatchReport(kind="recipes", dataset=dataset)
    if dataset not in DATASETS:
        rep.status = "failed"
        rep.errors.append({"file": "-", "line": 0, "field": "dataset", "message": f"dataset phải là {DATASETS}"})
        return rep
    release = active_release(conn)
    if not release:
        rep.status = "failed"
        rep.errors.append({"file": "-", "line": 0, "field": "usda", "message": "Chưa nhập USDA: không thể kiểm tra fdc_id"})
        return rep
    rrows = _read_csv(recipes_text, RECIPE_COLS, "recipes.csv", rep)
    lrows = _read_csv(ingredients_text, LINE_COLS, "recipe_ingredients.csv", rep)
    if rrows is None or lrows is None:
        return rep

    recipes: dict[str, dict] = {}
    bad: dict[str, list] = {}

    def err(rid, fname, line, fld, msg):
        e = {"file": fname, "line": line, "field": fld, "recipe_id": rid, "message": msg}
        rep.errors.append(e)
        bad.setdefault(rid or "?", []).append(e)

    for line, r in rrows:
        rid = r.get("recipe_id", "")
        if not RECIPE_ID_RE.match(rid):
            err(rid, "recipes.csv", line, "recipe_id", "recipe_id trống hoặc chứa ký tự không hợp lệ")
            continue
        if rid in recipes:
            err(rid, "recipes.csv", line, "recipe_id", "recipe_id bị trùng trong tệp")
            continue
        rec = {"recipe_id": rid, "line": line, "lines": {}}
        rec["name_vi"] = r.get("name_vi", "")
        if not rec["name_vi"]:
            err(rid, "recipes.csv", line, "name_vi", "Thiếu tên món")
        try:
            b = _num(r.get("base_servings"))
            if b is None or b <= 0:
                raise ValueError
            rec["base_servings"] = b
        except ValueError:
            err(rid, "recipes.csv", line, "base_servings", "base_servings phải là số dương")
        meals = []
        for m in re.split(r"[;|,]", r.get("meal_types", "")):
            m = m.strip().lower()
            if not m:
                continue
            if m not in MEAL_ALIASES:
                err(rid, "recipes.csv", line, "meal_types", f"Bữa không hợp lệ: {m}")
            else:
                meals.append(MEAL_ALIASES[m])
        if not meals:
            err(rid, "recipes.csv", line, "meal_types", "Cần ít nhất một bữa (breakfast/lunch/dinner)")
        rec["meal_types"] = ";".join(sorted(set(meals), key=["breakfast", "lunch", "dinner"].index))
        try:
            p = _num(r.get("prep_minutes"))
            if p is not None and p < 0:
                raise ValueError
            rec["prep_minutes"] = p
        except ValueError:
            err(rid, "recipes.csv", line, "prep_minutes", "prep_minutes phải là số không âm hoặc để trống")
        rec["source_ref"] = r.get("source_ref", "")
        if not rec["source_ref"]:
            err(rid, "recipes.csv", line, "source_ref", "Thiếu nguồn công thức")
        rec["source_review_status"] = r.get("review_status", "")
        try:
            v = int(r.get("version") or "")
            if v < 1:
                raise ValueError
            rec["version"] = v
        except ValueError:
            err(rid, "recipes.csv", line, "version", "version phải là số nguyên ≥ 1")
        recipes[rid] = rec

    for line, r in lrows:
        rid = r.get("recipe_id", "")
        if rid not in recipes:
            err(rid, "recipe_ingredients.csv", line, "recipe_id", "Dòng nguyên liệu tham chiếu món không có trong recipes.csv")
            continue
        rec = recipes[rid]
        try:
            ln = int(r.get("line_no", ""))
        except ValueError:
            err(rid, "recipe_ingredients.csv", line, "line_no", "line_no phải là số nguyên")
            continue
        if ln in rec["lines"]:
            err(rid, "recipe_ingredients.csv", line, "line_no", f"line_no {ln} bị trùng")
            continue
        try:
            fdc = int(r.get("fdc_id", ""))
        except ValueError:
            err(rid, "recipe_ingredients.csv", line, "fdc_id", "fdc_id phải là số nguyên")
            continue
        desc = _food_desc(conn, release, fdc)
        if desc is None:
            err(rid, "recipe_ingredients.csv", line, "fdc_id", f"fdc_id {fdc} không tồn tại trong USDA {release}")
            continue
        if expected_desc and fdc in expected_desc and not desc.startswith(expected_desc[fdc]):
            err(rid, "recipe_ingredients.csv", line, "fdc_id",
                f"fdc_id {fdc} có mô tả '{desc}', không khớp '{expected_desc[fdc]}'")
            continue
        try:
            q = _num(r.get("quantity_g_edible"))
            if q is None or q <= 0:
                raise ValueError
        except ValueError:
            err(rid, "recipe_ingredients.csv", line, "quantity_g_edible", "quantity_g_edible phải là số gam dương")
            continue
        state = r.get("food_state", "")
        if not state:
            err(rid, "recipe_ingredients.csv", line, "food_state", "Thiếu food_state đã đối chiếu với mô tả USDA")
            continue
        if state.lower() not in desc.lower() and desc.lower() not in state.lower():
            rep.warnings.append(f"{rid} dòng {ln}: food_state '{state}' khác mô tả USDA '{desc}' – cần rà soát khi duyệt")
        rec["lines"][ln] = (fdc, q, state, r.get("conversion_source", ""))

    for rid, rec in recipes.items():
        if not rec["lines"] and rid not in bad:
            err(rid, "recipe_ingredients.csv", rec["line"], "recipe_id", "Món không có dòng nguyên liệu")

    with transaction(conn):
        rep.batch_id = _new_batch(conn, "recipes", dataset)
        for rid, rec in recipes.items():
            if rid in bad:
                rep.rejected.append(rid)
                continue
            existing = conn.execute("SELECT * FROM recipe WHERE recipe_id=? AND version=?", (rid, rec["version"])).fetchone()
            if existing:
                old_lines = {r["line_no"]: (r["fdc_id"], r["quantity_g_edible"], r["food_state"], r["conversion_source"] or "")
                             for r in conn.execute("SELECT * FROM recipe_ingredient WHERE recipe_id=? AND version=?", (rid, rec["version"]))}
                same = (existing["name_vi"] == rec["name_vi"] and existing["base_servings"] == rec["base_servings"]
                        and existing["meal_types"] == rec["meal_types"] and existing["dataset"] == dataset
                        and existing["prep_minutes"] == rec["prep_minutes"] and old_lines == rec["lines"])
                if same:
                    rep.unchanged.append(rid)
                else:
                    rep.rejected.append(rid)
                    rep.errors.append({"file": "recipes.csv", "line": rec["line"], "field": "version", "recipe_id": rid,
                                       "message": f"Phiên bản {rec['version']} đã tồn tại với nội dung khác – hãy tăng version"})
                continue
            conn.execute("INSERT INTO recipe VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (rid, rec["version"], dataset, rec["name_vi"], rec["base_servings"], rec["meal_types"],
                          rec["prep_minutes"], rec["source_ref"], rec["source_review_status"], "pending_review",
                          rep.batch_id, release, now_iso(), None))
            conn.executemany("INSERT INTO recipe_ingredient VALUES(?,?,?,?,?,?,?)",
                             [(rid, rec["version"], ln, f, q, s, c) for ln, (f, q, s, c) in sorted(rec["lines"].items())])
            rep.accepted.append(f"{rid} v{rec['version']}")
        _finish_batch(conn, rep)
    return rep


def approve_recipe(conn, recipe_id: str, version: int, reviewer_note: str = "") -> tuple[bool, str]:
    row = conn.execute("SELECT * FROM recipe WHERE recipe_id=? AND version=?", (recipe_id, version)).fetchone()
    if not row:
        return False, "Không tìm thấy công thức"
    if row["status"] not in ("pending_review", "approved"):
        return False, f"Không duyệt được công thức ở trạng thái {row['status']}"
    release = active_release(conn)
    for ln in conn.execute("SELECT fdc_id FROM recipe_ingredient WHERE recipe_id=? AND version=?", (recipe_id, version)):
        if _food_desc(conn, release, ln["fdc_id"]) is None:
            return False, f"fdc_id {ln['fdc_id']} không có trong USDA đang dùng"
    with transaction(conn):
        conn.execute("UPDATE recipe SET status='approved', approved_at=? WHERE recipe_id=? AND version=?",
                     (now_iso(), recipe_id, version))
        conn.execute("INSERT INTO recipe_active(recipe_id, dataset, version) VALUES(?,?,?) "
                     "ON CONFLICT(recipe_id, dataset) DO UPDATE SET version=excluded.version",
                     (recipe_id, row["dataset"], version))
    return True, f"Đã duyệt {recipe_id} v{version} cho bộ {row['dataset']}"


def reject_recipe(conn, recipe_id: str, version: int) -> tuple[bool, str]:
    with transaction(conn):
        cur = conn.execute("UPDATE recipe SET status='rejected' WHERE recipe_id=? AND version=? AND status='pending_review'",
                           (recipe_id, version))
    return (cur.rowcount > 0), ("Đã từ chối" if cur.rowcount else "Không có bản chờ duyệt")


def approve_batch(conn, batch_id: int) -> list[str]:
    msgs = []
    for r in conn.execute("SELECT recipe_id, version FROM recipe WHERE batch_id=? AND status='pending_review'", (batch_id,)).fetchall():
        ok, msg = approve_recipe(conn, r["recipe_id"], r["version"])
        msgs.append(msg)
    return msgs


# ---------------------------------------------------------------- danh mục nguyên liệu

def import_ingredient_info(conn, text: str, dataset: str = "operational") -> BatchReport:
    rep = BatchReport(kind="ingredient_info", dataset=dataset)
    rows = _read_csv(text, INFO_COLS, "ingredients.csv", rep)
    if rows is None:
        return rep
    release = active_release(conn)
    with transaction(conn):
        rep.batch_id = _new_batch(conn, "ingredient_info", dataset)
        for line, r in rows:
            try:
                fdc = int(r["fdc_id"])
                ver = int(r["version"] or "1")
            except ValueError:
                rep.errors.append({"file": "ingredients.csv", "line": line, "field": "fdc_id/version", "message": "Phải là số nguyên"})
                rep.rejected.append(r.get("fdc_id"))
                continue
            if _food_desc(conn, release, fdc) is None:
                rep.errors.append({"file": "ingredients.csv", "line": line, "field": "fdc_id", "message": f"fdc_id {fdc} không tồn tại"})
                rep.rejected.append(fdc)
                continue
            status = (r["review_status"] or "unknown").lower()
            if status not in ("reviewed", "unknown", "needs_review"):
                rep.errors.append({"file": "ingredients.csv", "line": line, "field": "review_status",
                                   "message": "review_status phải là reviewed/unknown/needs_review"})
                rep.rejected.append(fdc)
                continue
            conn.execute("INSERT OR REPLACE INTO ingredient_info VALUES(?,?,?,?,?,?,?,?,?)",
                         (fdc, ver, dataset, r["name_vi"], r["aliases"], r["avoid_tags"], status, r["source_ref"], now_iso()))
            rep.accepted.append(fdc)
        _finish_batch(conn, rep)
    return rep


# ---------------------------------------------------------------- giá và quy cách

def import_prices(conn, text: str, dataset: str = "operational") -> BatchReport:
    rep = BatchReport(kind="prices", dataset=dataset)
    rows = _read_csv(text, PRICE_COLS, "prices.csv", rep)
    if rows is None:
        return rep
    release = active_release(conn)
    with transaction(conn):
        rep.batch_id = _new_batch(conn, "prices", dataset)
        for line, r in rows:
            def bad(fld, msg):
                rep.errors.append({"file": "prices.csv", "line": line, "field": fld, "message": msg})
                rep.rejected.append(r.get("fdc_id"))
            try:
                fdc = int(r["fdc_id"])
                ver = int(r["version"] or "1")
            except ValueError:
                bad("fdc_id/version", "Phải là số nguyên")
                continue
            if _food_desc(conn, release, fdc) is None:
                bad("fdc_id", f"fdc_id {fdc} không tồn tại")
                continue
            try:
                pkg = _num(r["package_g"])
                price = _num(r["price"])
            except ValueError:
                bad("package_g/price", "package_g và price phải là số")
                continue
            if pkg is not None and pkg <= 0:
                bad("package_g", "package_g phải dương hoặc để trống")
                continue
            if price is not None and price < 0:
                bad("price", "price không được âm")
                continue
            basis = (r["mass_basis"] or "edible").lower()
            if basis not in ("edible", "as_purchased"):
                bad("mass_basis", "mass_basis phải là edible hoặc as_purchased")
                continue
            cur = (pkg, price, (r["currency"] or "VND").upper(), basis, r["source_ref"], r["effective_date"])
            old = conn.execute("SELECT package_g, price, currency, mass_basis, source_ref, effective_date FROM price_item "
                               "WHERE fdc_id=? AND dataset=? AND version=?", (fdc, dataset, ver)).fetchone()
            if old:
                if tuple(old) == cur:
                    rep.unchanged.append(fdc)
                else:
                    bad("version", f"Giá fdc_id {fdc} phiên bản {ver} đã có nội dung khác – hãy tăng version")
                continue
            conn.execute("INSERT INTO price_item VALUES(?,?,?,?,?,?,?,?,?,?)", (fdc, dataset, ver, *cur, now_iso()))
            rep.accepted.append(f"{fdc} v{ver}")
        _finish_batch(conn, rep)
    return rep


# ---------------------------------------------------------------- quy tắc dinh dưỡng

def upsert_rules(conn, rule_set: str, version: int, rules: list[tuple], source_ref: str, review_status: str, label: str,
                 audience: str = "") -> None:
    with transaction(conn):
        conn.execute("DELETE FROM nutrition_rule WHERE rule_set=? AND version=?", (rule_set, version))
        for metric, op, value, unit, scope in rules:
            conn.execute("INSERT INTO nutrition_rule VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (rule_set, version, metric, op, value, unit, scope, audience, source_ref, review_status, label))


# ---------------------------------------------------------------- demo

def load_demo(conn) -> dict:
    from app.catalog import demo
    from app.catalog.health import store_policy

    out = {}
    expected = {k: v[3] for k, v in demo.DEMO_INGREDIENTS.items()}
    out["ingredients"] = import_ingredient_info(conn, demo.ingredient_info_csv(), "demo").to_dict()
    rep = import_recipes(conn, demo.recipes_csv(), demo.recipe_ingredients_csv(), "demo", expected_desc=expected)
    out["recipes"] = rep.to_dict()
    approved = []
    for r in conn.execute("SELECT recipe_id, version FROM recipe WHERE dataset='demo' AND status='pending_review'").fetchall():
        ok, msg = approve_recipe(conn, r["recipe_id"], r["version"])
        approved.append(msg)
    out["approved"] = approved
    out["prices"] = import_prices(conn, demo.prices_csv(), "demo").to_dict()
    upsert_rules(conn, demo.DEMO_RULE_SET, 1, demo.DEMO_RULES, "Bộ quy tắc giả lập – chỉ để kiểm thử, không phải khuyến nghị",
                 "demo", "demo", "Người dùng thử nghiệm (giả lập)")
    out["rules"] = demo.DEMO_RULE_SET
    for policy in demo.demo_health_policies():
        store_policy(conn, policy, mode="demo")
    return out
