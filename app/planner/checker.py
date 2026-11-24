"""Bộ kiểm tra độc lập (FR-08).

Đầu vào chỉ là danh sách (ngày, bữa, mã công thức, phiên bản, khẩu phần) và các điều kiện đã
xác nhận. Bộ kiểm tra tự đọc lại công thức và dòng dinh dưỡng gốc từ CSDL bằng truy vấn riêng,
tính lại theo từng bữa, từng ngày, cả kỳ và đối chiếu mọi điều kiện bắt buộc. Nó KHÔNG nhận tổng
dinh dưỡng hay kết luận đạt từ bộ lập thực đơn (các trường như `declared_totals` bị bỏ qua).

Kết quả lưu giá trị đo, giới hạn, đơn vị, phạm vi, trạng thái và bản ghi nguồn để truy xuất.
"""
from __future__ import annotations

import math

from app.catalog.health import classify_ingredients
from app.catalog.lookup import MEAL_LABELS, MEAL_ORDER, METRICS
from app.planner.constraints import OP_TEXT, fmt
from app.planner.filtering import dish_exclusion, ingredient_exclusion
from app.planner.nutrition import compare, dish_nutrition, grams_by_food, sum_totals
from app.planner.shopping import build_shopping
from app.storage.db import get_meta, now_iso


class _Source:
    """Truy cập dữ liệu gốc riêng của bộ kiểm tra (không dùng bộ đệm của bộ lập)."""

    def __init__(self, conn, dataset, release):
        self.conn, self.dataset, self.release = conn, dataset, release

    def recipe(self, recipe_id, version):
        r = self.conn.execute("SELECT * FROM recipe WHERE recipe_id=? AND version=?", (recipe_id, version)).fetchone()
        if not r:
            return None
        rec = dict(r)
        rec["meal_types"] = [m for m in (rec["meal_types"] or "").split(";") if m]
        rec["ingredients"] = [dict(x) for x in self.conn.execute(
            "SELECT line_no, fdc_id, quantity_g_edible, food_state FROM recipe_ingredient WHERE recipe_id=? AND version=? "
            "ORDER BY line_no", (recipe_id, version))]
        return rec

    def nutrients(self, fdc_id):
        out = {}
        for r in self.conn.execute("SELECT id, nutrient_id, amount FROM usda_food_nutrient WHERE release_id=? AND fdc_id=?",
                                   (self.release, fdc_id)):
            for m, (nid, _u, _l) in METRICS.items():
                if r["nutrient_id"] == nid:
                    out[m] = {"amount_per_100g": r["amount"], "food_nutrient_id": r["id"], "release_id": self.release}
        return out

    def food(self, fdc_id):
        r = self.conn.execute("SELECT description FROM usda_food WHERE release_id=? AND fdc_id=?", (self.release, fdc_id)).fetchone()
        return r["description"] if r else None

    def info(self, fdc_id):
        r = self.conn.execute("SELECT * FROM ingredient_info WHERE fdc_id=? AND dataset=? ORDER BY version DESC LIMIT 1",
                              (fdc_id, self.dataset)).fetchone()
        return dict(r) if r else None

    def name(self, fdc_id):
        i = self.info(fdc_id)
        return (i or {}).get("name_vi") or self.food(fdc_id) or f"fdc {fdc_id}"

    def price(self, fdc_id):
        r = self.conn.execute("SELECT * FROM price_item WHERE fdc_id=? AND dataset=? ORDER BY version DESC LIMIT 1",
                              (fdc_id, self.dataset)).fetchone()
        return dict(r) if r else None


def _scope_label(scope, key):
    if scope == "meal":
        return f"Ngày {key[0]} – {MEAL_LABELS.get(key[1], key[1])}"
    if scope == "day":
        return f"Ngày {key}"
    return "Cả kỳ"


def _food_group_results(rule, dishes, name_of):
    scope = rule["scope"]
    groups = {}
    for dish in dishes:
        key = (dish["day"], dish["meal"]) if scope == "meal" else dish["day"] if scope == "day" else 0
        groups.setdefault(key, []).append(dish)

    for key, group in groups.items():
        known, missing = 0, set()
        matches = []
        for dish in group:
            count, unknown = classify_ingredients((dish.get("grams") or {}).keys(), rule)
            if count:
                known += count
                matches.append(dish["name"])
            missing.update(unknown)
        status = "fail" if known > rule["value"] else ("missing" if missing else "pass")
        detail = f"{known} món thuộc nhóm {rule['name']} (tối đa {rule['value']})"
        if matches:
            detail += " · " + ", ".join(matches)
        if missing:
            detail += ". Chưa phân loại: " + ", ".join(f"{name_of(fdc)} (FDC {fdc})" for fdc in sorted(missing))
        yield {"cid": rule["cid"], "kind": "food_group", "label": rule["label"], "scope": scope,
               "scope_key": list(key) if isinstance(key, tuple) else key, "scope_label": _scope_label(scope, key),
               "measured": known if not missing else None, "limit": rule["value"], "unit": "món",
               "status": status, "detail": detail, "source": rule["source"], "editable": False,
               "missing_records": sorted(missing)}


def verify_plan(conn, items: list[dict], profile: dict, constraints: list[dict], dataset: str,
                release_id: str | None = None) -> dict:
    release = release_id or get_meta(conn, "active_usda_release")
    src = _Source(conn, dataset, release)
    structural: list[str] = []
    dishes = []
    allowed_levels = [float(x) for x in profile.get("serving_levels", [])]

    # 1. cấu trúc thực đơn
    expected = {(d, m) for d in range(1, (profile.get("days") or 0) + 1) for m in profile.get("meals", [])}
    count: dict = {}
    for it in items:
        k = (int(it["day"]), it["meal"])
        count.setdefault(k, []).append(it)
    for k in sorted(expected, key=lambda x: (x[0], MEAL_ORDER.index(x[1]))):
        got = count.get(k, [])
        if len(got) != profile.get("dishes_per_meal"):
            structural.append(f"{_scope_label('meal', k)}: có {len(got)} món, cần {profile.get('dishes_per_meal')}")
        ids = [g["recipe_id"] for g in got]
        if len(ids) != len(set(ids)):
            structural.append(f"{_scope_label('meal', k)}: lặp món trong cùng bữa")
    for k in count:
        if k not in expected:
            structural.append(f"Ngày {k[0]} – {k[1]}: không thuộc kỳ/bữa đã chọn")

    # 2. tính lại từng món từ dữ liệu gốc
    for it in sorted(items, key=lambda x: (int(x["day"]), MEAL_ORDER.index(x["meal"]) if x["meal"] in MEAL_ORDER else 9,
                                          int(x.get("pos", 0)))):
        rec = src.recipe(it["recipe_id"], int(it["version"]))
        servings = float(it["servings"])
        d = {"day": int(it["day"]), "meal": it["meal"], "pos": int(it.get("pos", 0)), "recipe_id": it["recipe_id"],
             "version": int(it["version"]), "servings": servings}
        if rec is None:
            structural.append(f"Không tìm thấy công thức {it['recipe_id']} v{it['version']}")
            d.update(name="?", totals={m: None for m in METRICS}, lines=[], missing={m: [] for m in METRICS})
            dishes.append(d)
            continue
        if rec["status"] != "approved" or rec["dataset"] != dataset:
            structural.append(f"{rec['recipe_id']} v{rec['version']} chưa được duyệt cho bộ dữ liệu {dataset}")
        if it["meal"] not in rec["meal_types"]:
            structural.append(f"{rec['name_vi']} không dùng cho {MEAL_LABELS.get(it['meal'], it['meal'])}")
        if not any(abs(servings - lv) < 1e-9 for lv in allowed_levels) or servings <= 0:
            structural.append(f"{rec['name_vi']}: khẩu phần {fmt(servings)} không thuộc các mức đã chọn")
        missing_food = [ing["fdc_id"] for ing in rec["ingredients"] if src.food(ing["fdc_id"]) is None]
        dn = dish_nutrition(rec, servings, src.nutrients)
        for f in missing_food:
            for m in METRICS:
                dn["missing"].setdefault(m, [])
                if f not in dn["missing"][m]:
                    dn["missing"][m].append(f)
                dn["totals"][m] = None
        d.update(name=rec["name_vi"], base_servings=rec["base_servings"], prep_minutes=rec["prep_minutes"],
                 totals=dn["totals"], lines=dn["lines"], missing=dn["missing"], grams=grams_by_food(rec, servings),
                 recipe=rec)
        dishes.append(d)

    # 3. tổng theo bữa / ngày / kỳ
    meals_t, days_t = {}, {}
    for d in dishes:
        meals_t.setdefault((d["day"], d["meal"]), []).append(d["totals"])
        days_t.setdefault(d["day"], []).append(d["totals"])
    meal_totals = {k: sum_totals(v) for k, v in meals_t.items()}
    day_totals = {k: sum_totals(v) for k, v in days_t.items()}
    period_total = sum_totals([d["totals"] for d in dishes])

    # 4. chi phí và danh sách mua sắm (cùng quy tắc với lúc lưu và đổi món)
    needed: dict[int, float] = {}
    for d in dishes:
        for f, g in (d.get("grams") or {}).items():
            needed[f] = needed.get(f, 0.0) + g
    inventory = {int(i["fdc_id"]): float(i["grams"]) for i in profile.get("inventory") or []}
    shopping = build_shopping(needed, inventory, src.price, src.name)

    def usage_cost_of(ds):
        parts = []
        for d in ds:
            for f, g in (d.get("grams") or {}).items():
                p = src.price(f)
                if not p or p.get("price") is None or not p.get("package_g") or (p.get("mass_basis") or "edible") != "edible":
                    return None
                parts.append(g * p["price"] / p["package_g"])
        return math.fsum(parts)

    # 5. đối chiếu từng điều kiện bắt buộc
    from app.planner.energy import integrity_errors as energy_integrity_errors
    results = []
    for message in energy_integrity_errors(profile, constraints):
        results.append({"cid": "body_energy_integrity", "kind": "body_module", "label": "Quy tắc năng lượng cá nhân",
                        "scope": "period", "scope_label": "Hồ sơ đã xác nhận", "status": "fail", "detail": message})
    from app.planner import disease
    from app.catalog.lookup import Catalog
    for message in disease.integrity_errors(profile,constraints,Catalog(conn,dataset),dataset):
        results.append({'cid':'disease_integrity','kind':'disease_module','label':'Bộ quy tắc bệnh mô phỏng',
                        'scope':'period','scope_label':'Hồ sơ đã xác nhận','status':'fail','detail':message})
    for c in constraints:
        if not c.get("hard"):
            continue
        if c["kind"] == "nutrient":
            m = c["metric"]
            if c["scope"] == "meal":
                groups = [(k, meal_totals.get(k, {}).get(m), [d for d in dishes if (d["day"], d["meal"]) == k]) for k in
                          sorted(meal_totals, key=lambda x: (x[0], MEAL_ORDER.index(x[1])))
                          if not c.get("meal_type") or k[1] == c["meal_type"]]
            elif c["scope"] == "day":
                groups = [(k, day_totals[k].get(m), [d for d in dishes if d["day"] == k]) for k in sorted(day_totals)]
            else:
                groups = [(0, period_total.get(m), dishes)]
            for key, val, ds in groups:
                r = {"cid": c["cid"], "kind": "nutrient", "label": c["label"], "metric": m, "op": c["op"], "limit": c["value"],
                     "unit": c["unit"], "scope": c["scope"], "scope_key": key if not isinstance(key, tuple) else list(key),
                     "scope_label": _scope_label(c["scope"], key), "measured": val, "source": c.get("source"),
                     "editable": c.get("editable", False), "category": c.get("category")}
                if val is None:
                    miss = sorted({f for d in ds for f in d["missing"].get(m, [])})
                    r["status"] = "missing"
                    r["detail"] = "Thiếu dữ liệu " + METRICS[m][2] + " cho: " + ", ".join(f"{src.name(f)} (fdc {f})" for f in miss)
                    r["missing_records"] = miss
                else:
                    ok = compare(val, c["op"], c["value"])
                    r["status"] = "pass" if ok else "fail"
                    r["detail"] = (f"{METRICS[m][2]} {fmt(val)} {c['unit']} {OP_TEXT[c['op']]} {fmt(c['value'])} {c['unit']}"
                                   if ok else f"{METRICS[m][2]} {fmt(val)} {c['unit']} không thỏa {OP_TEXT[c['op']]} "
                                   f"{fmt(c['value'])} {c['unit']} (chênh {fmt(abs(val - c['value']))} {c['unit']})")
                results.append(r)
        elif c['kind'] == 'nutrient_ratio':
            if c['scope']=='meal':
                groups=[(k,t) for k,t in meal_totals.items() if not c.get('meal_type') or k[1]==c['meal_type']]
            elif c['scope']=='day':
                groups=list(day_totals.items())
            else:
                groups=[(0,period_total)]
            for key,totals in groups:
                missing=any(totals.get(m) is None for m in c['terms'])
                val=None if missing else math.fsum(totals[m]*a for m,a in c['terms'].items())
                measured=None if missing or not totals.get('energy_kcal') else totals[c['nutrient']]*c['display_scale']/totals['energy_kcal']
                status='missing' if missing else 'pass' if compare(val,c['op'],c['value']) else 'fail'
                results.append({'cid':c['cid'],'kind':'nutrient_ratio','label':c['label'],'scope':c['scope'],
                    'scope_key':list(key) if isinstance(key,tuple) else key,'scope_label':_scope_label(c['scope'],key),
                    'status':status,'measured':measured,'limit':c['threshold'],'op':c['op'],'unit':c['unit'],
                    'detail':'Thiếu dữ liệu cho tỷ lệ dinh dưỡng' if missing else f"{fmt(measured)} {c['unit']} {OP_TEXT[c['op']]} {fmt(c['threshold'])} {c['unit']}",
                    'source':c['source'],'editable':False})
        elif c["kind"] == "food_group":
            results.extend(_food_group_results(c, dishes, src.name))
        elif c["kind"] == "exclude":
            bad, unknown = [], []
            for d in dishes:
                if dish_exclusion(c, d["recipe_id"], d["name"]):
                    bad.append(f"Ngày {d['day']}: {d['name']} thuộc danh sách cần tránh")
                if not (c.get("terms") or c.get("ingredients")):
                    continue
                for ing in (d.get("recipe") or {}).get("ingredients", []):
                    fdc = ing["fdc_id"]
                    matches, unchecked = ingredient_exclusion(c, fdc, src.info(fdc), src.food(fdc))
                    bad.extend(f"Ngày {d['day']} {MEAL_LABELS.get(d['meal'])}: {d['name']} có {src.name(fdc)} ('{t}')"
                               for t in matches)
                    unknown.extend(f"{d['name']}: {src.name(fdc)} chưa rà soát với '{t}'" for t in unchecked)
            st = "fail" if bad else ("missing" if unknown else "pass")
            results.append({"cid": c["cid"], "kind": "exclude", "label": c["label"], "scope": "period", "scope_label": "Cả kỳ",
                            "status": st, "detail": "; ".join(sorted(set(bad or unknown))) or f"Đạt điều kiện {c['label'].lower()}",
                            "editable": False})
        elif c["kind"] == "budget":
            if c["mode"] == "purchase":
                val = shopping["purchase_total"]
                groups = [("period", val)]
                miss_txt = ", ".join(shopping["missing_price"] + shopping["missing_package"])
            else:
                if c["scope"] == "day":
                    groups = [(k, usage_cost_of([d for d in dishes if d["day"] == k])) for k in sorted(days_t)]
                else:
                    groups = [("period", usage_cost_of(dishes))]
                miss_txt = "giá hoặc quy cách của một số nguyên liệu"
            for key, val in groups:
                lab = "Cả kỳ" if key == "period" else f"Ngày {key}"
                r = {"cid": c["cid"], "kind": "budget", "label": c["label"], "op": "<=", "limit": c["value"], "unit": "VND",
                     "scope": c["scope"], "scope_label": lab, "measured": val, "mode": c["mode"], "editable": True}
                if val is None:
                    r["status"] = "missing"
                    r["detail"] = f"Chưa đủ dữ liệu để kiểm tra ngân sách: thiếu {miss_txt}"
                else:
                    ok = compare(val, "<=", c["value"])
                    r["status"] = "pass" if ok else "fail"
                    kind = "tiền mua gói" if c["mode"] == "purchase" else "chi phí theo lượng dùng"
                    r["detail"] = (f"{kind} {fmt(val)} VND ≤ {fmt(c['value'])} VND" if ok else
                                   f"{kind} {fmt(val)} VND vượt {fmt(c['value'])} VND (vượt {fmt(val - c['value'])} VND)")
                results.append(r)
        elif c["kind"] == "prep_time":
            bad, miss = [], []
            for d in dishes:
                pm = d.get("prep_minutes")
                if pm is None:
                    miss.append(d.get("name"))
                elif not compare(pm, "<=", c["value"]):
                    bad.append(f"{d['name']} {fmt(pm)} phút")
            st = "fail" if bad else ("missing" if miss else "pass")
            results.append({"cid": c["cid"], "kind": "prep_time", "label": c["label"], "scope": "meal", "scope_label": "Từng món",
                            "status": st, "detail": "; ".join(bad or [f"thiếu thời gian: {x}" for x in miss]) or "Mọi món đạt",
                            "editable": True})

    if structural or any(r["status"] == "fail" for r in results):
        status = "constraint_violation"
    elif any(r["status"] == "missing" for r in results):
        status = "missing_data"
    else:
        status = "validated"

    # 6. truy xuất: theo ngày & chỉ tiêu → món → dòng nguyên liệu → bản ghi USDA
    traces = {}
    for d in dishes:
        day = str(d["day"])
        for m in METRICS:
            for ln in d.get("lines", []):
                v = ln["values"].get(m)
                traces.setdefault(day, {}).setdefault(m, []).append({
                    "meal": d["meal"], "recipe_id": d["recipe_id"], "version": d["version"], "dish": d["name"],
                    "servings": d["servings"], "base_servings": d.get("base_servings"), "line_no": ln["line_no"],
                    "fdc_id": ln["fdc_id"], "ingredient": src.name(ln["fdc_id"]), "usda_description": src.food(ln["fdc_id"]),
                    "quantity_g_recipe": ln["quantity_g_recipe"], "grams": ln["grams"],
                    "per_100g": v["per_100g"] if v else None, "amount": v["amount"] if v else None,
                    "food_nutrient_id": v["food_nutrient_id"] if v else None})

    public_dishes = [{k: v for k, v in d.items() if k not in ("lines", "recipe", "grams")} for d in dishes]
    return {
        "status": status, "checked_at": now_iso(), "release_id": release, "dataset": dataset,
        "structural": structural, "results": results, "dishes": public_dishes,
        "totals": {"meals": {f"{k[0]}|{k[1]}": v for k, v in meal_totals.items()},
                   "days": {str(k): v for k, v in day_totals.items()}, "period": period_total},
        "cost": {"usage_total": shopping["usage_total"], "purchase_total": shopping["purchase_total"],
                 "purchase_status": shopping["purchase_status"]},
        "shopping": shopping, "traces": traces,
        "sources": {"recipes": sorted({(d["recipe_id"], d["version"]) for d in dishes}), "usda_release": release,
                    "price_versions": shopping["price_versions"]},
        "ignored_inputs": "Bộ kiểm tra không dùng tổng hoặc kết luận do bộ lập thực đơn gửi kèm",
    }
