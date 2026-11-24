"""Phép tính dinh dưỡng dùng chung (FR-07) – không giữ trạng thái, không đọc CSDL.

    gam thực tế  = gam trong công thức × khẩu phần chọn ÷ số khẩu phần gốc
    lượng chất   = gam thực tế ÷ 100 × hàm lượng trên 100 g

Giữ đủ độ chính xác khi cộng (math.fsum) và chỉ làm tròn khi hiển thị. Nếu một dòng thiếu giá
trị của chỉ tiêu, tổng chỉ tiêu đó là None (chưa đủ dữ liệu) – không bao giờ coi là 0.
"""
from __future__ import annotations

import math
from typing import Callable, Iterable

from app.catalog.lookup import METRICS

OPS = ("<=", "<", ">=", ">", "==")


def tolerance(limit: float) -> float:
    return 1e-9 * max(1.0, abs(limit))


def compare(value: float, op: str, limit: float) -> bool:
    """So sánh có dung sai số thực rất nhỏ: bằng ngưỡng đạt với `<=`, vi phạm với `<`."""
    t = tolerance(limit)
    if op == "<=":
        return value <= limit + t
    if op == "<":
        return value < limit - t
    if op == ">=":
        return value >= limit - t
    if op == ">":
        return value > limit + t
    if op == "==":
        return abs(value - limit) <= t
    raise ValueError(f"Phép so sánh không hỗ trợ: {op}")


def actual_grams(quantity_g: float, servings: float, base_servings: float) -> float:
    return quantity_g * servings / base_servings


def dish_nutrition(recipe: dict, servings: float, nutrients_of: Callable[[int], dict],
                   metrics: Iterable[str] = METRICS) -> dict:
    """Tính dinh dưỡng một món cho `servings` khẩu phần.

    `nutrients_of(fdc_id)` trả {metric: {amount_per_100g, food_nutrient_id, ...}}.
    Kết quả gồm từng dòng (để truy xuất) và tổng theo chỉ tiêu.
    """
    metrics = list(metrics)
    base = float(recipe["base_servings"])
    lines = []
    parts: dict[str, list[float]] = {m: [] for m in metrics}
    missing: dict[str, list[int]] = {}
    for ing in recipe["ingredients"]:
        grams = actual_grams(float(ing["quantity_g_edible"]), servings, base)
        nut = nutrients_of(int(ing["fdc_id"])) or {}
        line = {"line_no": ing["line_no"], "fdc_id": ing["fdc_id"], "quantity_g_recipe": ing["quantity_g_edible"],
                "grams": grams, "values": {}}
        for m in metrics:
            rec = nut.get(m)
            if rec is None or rec.get("amount_per_100g") is None:
                missing.setdefault(m, []).append(int(ing["fdc_id"]))
                line["values"][m] = None
                continue
            v = grams / 100.0 * float(rec["amount_per_100g"])
            parts[m].append(v)
            line["values"][m] = {"per_100g": rec["amount_per_100g"], "amount": v,
                                 "food_nutrient_id": rec.get("food_nutrient_id"), "release_id": rec.get("release_id")}
        lines.append(line)
    totals = {m: (None if m in missing else math.fsum(parts[m])) for m in metrics}
    return {"recipe_id": recipe["recipe_id"], "version": recipe.get("version"), "servings": servings,
            "base_servings": base, "factor": servings / base, "lines": lines, "totals": totals, "missing": missing}


def sum_totals(totals_list: list[dict], metrics: Iterable[str] = METRICS) -> dict:
    out = {}
    for m in metrics:
        vals = [t.get(m) for t in totals_list]
        out[m] = None if any(v is None for v in vals) else math.fsum(vals)
    return out


def grams_by_food(recipe: dict, servings: float) -> dict[int, float]:
    out: dict[int, float] = {}
    base = float(recipe["base_servings"])
    for ing in recipe["ingredients"]:
        f = int(ing["fdc_id"])
        out[f] = out.get(f, 0.0) + actual_grams(float(ing["quantity_g_edible"]), servings, base)
    return out


def usage_cost(grams: dict[int, float], price_of: Callable[[int], dict | None]) -> tuple[float | None, list[int]]:
    """Chi phí theo lượng sử dụng = Σ gam × giá gói ÷ gam mỗi gói. None nếu thiếu giá."""
    parts, missing = [], []
    for f, g in grams.items():
        p = price_of(f)
        if (not p or p.get("price") is None or not p.get("package_g")
                or (p.get("mass_basis") or "edible") != "edible"):
            missing.append(f)
            continue
        parts.append(g * float(p["price"]) / float(p["package_g"]))
    return (None if missing else math.fsum(parts)), missing
