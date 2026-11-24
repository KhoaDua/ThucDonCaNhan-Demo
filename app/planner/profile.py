"""Hồ sơ và nhu cầu (FR-01, FR-02): đọc checklist, kiểm tra kiểu dữ liệu, đơn vị và giới hạn.

Hồ sơ được lưu dạng JSON có phiên bản. Lỗi trả theo từng trường để giao diện báo ngay tại ô nhập.
"""
from __future__ import annotations

import re
from decimal import Decimal

from app.catalog.lookup import MEAL_ORDER, METRICS

MAX_DAYS = 14
MAX_DISHES = 3
MAX_LEVELS = 5
LIMIT_ROWS = 6
UNIT_CHOICES = {"kcal": ("kcal", "kJ"), "g": ("g", "mg"), "mg": ("mg", "g")}
SCOPES = {"meal": "mỗi bữa", "day": "mỗi ngày", "period": "cả kỳ"}
CATEGORIES = {"health_limit": "Giới hạn sức khỏe đã xác nhận", "personal_goal": "Mục tiêu cá nhân"}


def default_profile() -> dict:
    return {
        "days": 2, "meals": ["breakfast", "lunch", "dinner"], "dishes_per_meal": 1, "serving_levels": [1.0, 1.5],
        "rule_set": "", "rule_set_confirmed": False, "nutrient_limits": [], "avoid": [], "likes": [], "dislikes": [],
        "prefer_variety": True, "max_prep_minutes": None, "prep_hard": False,
        "budget": None, "inventory": [],
    }


def parse_number(s) -> float:
    """Nhận '1,5', '1.5', '200.000', '200k', '1,2 triệu'. Ném ValueError nếu không hiểu."""
    if isinstance(s, (int, float)):
        return float(s)
    t = (s or "").strip().lower().replace(" ", "").replace("đ", "").replace("vnd", "").replace("đồng", "")
    if t == "":
        raise ValueError("trống")
    mult = 1.0
    for suf, m in (("triệu", 1e6), ("tr", 1e6), ("nghìn", 1e3), ("ngàn", 1e3), ("k", 1e3)):
        if t.endswith(suf):
            t, mult = t[: -len(suf)], m
            break
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")
    elif "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        raise ValueError(f"không phải số: {s!r}")
    return float(t) * mult


def _split_terms(s: str) -> list[str]:
    out = []
    for t in re.split(r"[,;\n]", s or ""):
        t = t.strip().lower()
        if t and t not in out:
            out.append(t)
    return out


def _first(form: dict, key: str, default: str = "") -> str:
    v = form.get(key)
    if isinstance(v, list):
        return (v[0] if v else default) or default
    return v if v is not None else default


def _all(form: dict, key: str) -> list[str]:
    v = form.get(key)
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def parse_form(form: dict) -> tuple[dict, dict]:
    """Chuyển dữ liệu checklist (dict tên trường → danh sách giá trị) thành hồ sơ + lỗi theo trường."""
    p = default_profile()
    errors: dict[str, str] = {}
    raw: dict = {}

    def num(key, *, integer=False, required=True, lo=None, hi=None, label=""):
        s = _first(form, key).strip()
        raw[key] = s
        if s == "":
            if required:
                errors[key] = f"Vui lòng nhập {label}"
            return None
        try:
            v = parse_number(s)
        except ValueError:
            errors[key] = f"{label} phải là số"
            return None
        if integer and v != int(v):
            errors[key] = f"{label} phải là số nguyên"
            return None
        if lo is not None and v < lo:
            errors[key] = f"{label} phải ≥ {lo:g}"
            return None
        if hi is not None and v > hi:
            errors[key] = f"{label} phải ≤ {hi:g}"
            return None
        return int(v) if integer else v

    p["days"] = num("days", integer=True, lo=1, hi=MAX_DAYS, label="Số ngày")
    meals = [m for m in MEAL_ORDER if m in _all(form, "meals")]
    if not meals:
        errors["meals"] = "Chọn ít nhất một bữa"
    p["meals"] = meals
    p["dishes_per_meal"] = num("dishes_per_meal", integer=True, lo=1, hi=MAX_DISHES, label="Số món mỗi bữa")

    levels_raw = _first(form, "serving_levels")
    levels = []
    for part in re.split(r"[;\s]+", levels_raw.strip()):
        if not part:
            continue
        try:
            v = parse_number(part)
        except ValueError:
            errors["serving_levels"] = f"Khẩu phần '{part}' không phải số"
            break
        if v <= 0:
            errors["serving_levels"] = "Khẩu phần phải lớn hơn 0"
            break
        if v > 5:
            errors["serving_levels"] = "Khẩu phần tối đa 5 cho một món"
            break
        if v not in levels:
            levels.append(v)
    if not levels and "serving_levels" not in errors:
        errors["serving_levels"] = "Nhập ít nhất một mức khẩu phần, ví dụ 1; 1,5"
    if len(levels) > MAX_LEVELS:
        errors["serving_levels"] = f"Tối đa {MAX_LEVELS} mức khẩu phần"
    p["serving_levels"] = sorted(levels)

    p["rule_set"] = _first(form, "rule_set").strip()
    p["rule_set_confirmed"] = bool(_first(form, "rule_set_confirmed"))

    limits = []
    for i in range(LIMIT_ROWS):
        metric = _first(form, f"limit_metric_{i}").strip()
        value_s = _first(form, f"limit_value_{i}").strip()
        if not metric and not value_s:
            continue
        key = f"limit_{i}"
        if metric not in METRICS:
            errors[key] = "Chọn chỉ tiêu"
            continue
        op = _first(form, f"limit_op_{i}", "<=")
        if op not in ("<=", "<", ">=", ">"):
            errors[key] = "Phép so sánh không hợp lệ"
            continue
        try:
            value = parse_number(value_s)
        except ValueError:
            errors[key] = "Giá trị giới hạn phải là số"
            continue
        if value <= 0:
            errors[key] = "Giá trị giới hạn phải lớn hơn 0"
            continue
        base_unit = METRICS[metric][1]
        unit = _first(form, f"limit_unit_{i}", base_unit)
        if unit not in UNIT_CHOICES[base_unit]:
            errors[key] = f"Đơn vị của {METRICS[metric][2]} phải là {' hoặc '.join(UNIT_CHOICES[base_unit])}"
            continue
        scope = _first(form, f"limit_scope_{i}", "day")
        if scope not in SCOPES:
            errors[key] = "Kỳ áp dụng không hợp lệ"
            continue
        category = _first(form, f"limit_category_{i}", "health_limit")
        if category not in CATEGORIES:
            category = "health_limit"
        source = _first(form, f"limit_source_{i}").strip()
        confirmed = bool(_first(form, f"limit_confirmed_{i}"))
        if not confirmed or not source:
            errors[key] = "Ghi nguồn và đánh dấu xác nhận giới hạn trước khi áp dụng"
        limits.append({"metric": metric, "op": op, "value": value, "unit": unit, "scope": scope,
                       "category": category, "source": source, "confirmed": confirmed, "row": i})
    p["nutrient_limits"] = limits

    p["avoid"] = _split_terms(_first(form, "avoid")) + [t.lower() for t in _all(form, "avoid_tag") if t]
    p["avoid"] = list(dict.fromkeys(p["avoid"]))
    p["likes"] = _split_terms(_first(form, "likes"))
    p["dislikes"] = _split_terms(_first(form, "dislikes"))
    p["prefer_variety"] = bool(_first(form, "prefer_variety"))
    p["max_prep_minutes"] = num("max_prep_minutes", required=False, lo=1, hi=600, label="Thời gian nấu tối đa")
    p["prep_hard"] = bool(_first(form, "prep_hard"))
    if p["prep_hard"] and p["max_prep_minutes"] is None and "max_prep_minutes" not in errors:
        errors["max_prep_minutes"] = "Nhập số phút nếu thời gian nấu là điều kiện bắt buộc"

    amount = num("budget_amount", required=False, lo=1, label="Ngân sách")
    if amount is not None:
        b = {"amount": amount, "currency": "VND", "scope": _first(form, "budget_scope", "period"),
             "mode": _first(form, "budget_mode", ""), "hard": bool(_first(form, "budget_hard")),
             "drop_cost_if_missing": bool(_first(form, "budget_drop_cost_if_missing"))}
        if b["scope"] not in ("period", "day"):
            errors["budget_scope"] = "Chọn kỳ áp dụng ngân sách"
        if b["mode"] not in ("usage", "purchase"):
            errors["budget_mode"] = "Chọn cách tính ngân sách: theo lượng sử dụng hoặc theo tiền mua gói"
        elif b["mode"] == "purchase" and b["scope"] != "period":
            errors["budget_mode"] = "Ngân sách theo tiền mua gói chỉ áp dụng cho cả kỳ"
        p["budget"] = b

    inv = []
    for ln_no, line in enumerate((_first(form, "inventory") or "").splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        m = re.fullmatch(r"(\d+)\s*[:=\s]\s*([\d.,]+)\s*(g|kg)?", line, flags=re.I)
        if not m:
            errors["inventory"] = f"Dòng {ln_no}: dùng dạng 'fdc_id: số gam', ví dụ 171287: 100"
            break
        try:
            grams = parse_number(m.group(2)) * (1000 if (m.group(3) or "").lower() == "kg" else 1)
        except ValueError:
            errors["inventory"] = f"Dòng {ln_no}: lượng tồn kho phải là số"
            break
        if grams <= 0:
            errors["inventory"] = f"Dòng {ln_no}: lượng phải lớn hơn 0"
            break
        inv.append({"fdc_id": int(m.group(1)), "grams": grams})
    merged: dict[int, float] = {}
    for it in inv:
        merged[it["fdc_id"]] = merged.get(it["fdc_id"], 0) + it["grams"]
    p["inventory"] = [{"fdc_id": k, "grams": v} for k, v in merged.items()]

    errors.update(cross_check(p))
    return p, errors


def cross_check(p: dict) -> dict:
    """Mâu thuẫn giữa các điều kiện (ví dụ ≥ 2000 và ≤ 1500 cùng chỉ tiêu, cùng kỳ)."""
    errors = {}
    lims = p.get("nutrient_limits") or []
    for a in lims:
        for b in lims:
            if a is b or a["metric"] != b["metric"] or a["scope"] != b["scope"]:
                continue
            if a["op"] in (">=", ">") and b["op"] in ("<=", "<"):
                va, vb = normalize_value(a["metric"], a["value"], a["unit"]), normalize_value(b["metric"], b["value"], b["unit"])
                if va > vb or (va == vb and (a["op"] == ">" or b["op"] == "<")):
                    msg = (f"Mâu thuẫn: {METRICS[a['metric']][2]} {a['op']} {a['value']:g} {a['unit']} và "
                           f"{b['op']} {b['value']:g} {b['unit']} cùng kỳ – hãy xem lại một trong hai điều kiện")
                    errors[f"limit_{a.get('row', 0)}"] = msg
                    errors[f"limit_{b.get('row', 0)}"] = msg
    return errors


def normalize_value(metric: str, value: float, unit: str) -> float:
    base = METRICS[metric][1]
    if unit == base:
        return value
    if base == "mg" and unit == "g":
        return value * 1000
    if base == "g" and unit == "mg":
        return value / 1000
    if base == "kcal" and unit == "kJ":
        return value / 4.184
    raise ValueError(f"Không quy đổi được {unit} sang {base}")


def validate_inventory(p: dict, catalog) -> dict:
    errors = {}
    for it in p.get("inventory", []):
        if catalog.get_food(it["fdc_id"]) is None:
            errors["inventory"] = f"fdc_id {it['fdc_id']} không có trong USDA đang dùng"
            break
    return errors


def profile_to_form(p: dict) -> dict:
    """Ngược lại của parse_form – để điền sẵn checklist (kể cả bản nháp từ AI)."""
    def number(value):
        return "" if value is None else format(Decimal(str(value)), "f").removesuffix(".0").replace(".", ",")

    f: dict = {
        "days": "" if p.get("days") is None else str(p["days"]),
        "meals": list(p.get("meals") or []),
        "dishes_per_meal": "" if p.get("dishes_per_meal") is None else str(p["dishes_per_meal"]),
        "serving_levels": "; ".join(number(v) for v in p.get("serving_levels") or []),
        "rule_set": p.get("rule_set") or "",
        "rule_set_confirmed": "1" if p.get("rule_set_confirmed") else "",
        "avoid": ", ".join(p.get("avoid") or []),
        "likes": ", ".join(p.get("likes") or []),
        "dislikes": ", ".join(p.get("dislikes") or []),
        "prefer_variety": "1" if p.get("prefer_variety") else "",
        "max_prep_minutes": number(p.get("max_prep_minutes")),
        "prep_hard": "1" if p.get("prep_hard") else "",
        "inventory": "\n".join(f"{i['fdc_id']}: {number(i['grams'])}" for i in (p.get("inventory") or [])),
    }
    body = p.get("body") or {}
    for key in ("height_cm", "weight_kg", "age_years"):
        f["body_" + key] = number(body.get(key))
    for key in ("sex", "activity"):
        f["body_" + key] = body.get(key, "")
    f["body_scope_confirmed"] = "1" if body.get("scope_confirmed") else ""
    f["body_pregnancy_or_breastfeeding"] = (
        "no" if body.get("scope_confirmed") and body.get("scope_source") != "simulation" else ""
    )
    for i, lim in enumerate((p.get("nutrient_limits") or [])[:LIMIT_ROWS]):
        f[f"limit_metric_{i}"] = lim.get("metric", "")
        f[f"limit_op_{i}"] = lim.get("op", "<=")
        f[f"limit_value_{i}"] = number(lim.get("value"))
        f[f"limit_unit_{i}"] = lim.get("unit", "")
        f[f"limit_scope_{i}"] = lim.get("scope", "day")
        f[f"limit_category_{i}"] = lim.get("category", "health_limit")
        f[f"limit_source_{i}"] = lim.get("source", "")
        f[f"limit_confirmed_{i}"] = "1" if lim.get("confirmed") else ""
    b = p.get("budget")
    if b:
        f.update({"budget_amount": number(b.get("amount")),
                  "budget_scope": b.get("scope", "period"), "budget_mode": b.get("mode", ""),
                  "budget_hard": "1" if b.get("hard") else "",
                  "budget_drop_cost_if_missing": "1" if b.get("drop_cost_if_missing") else ""})
    return f
