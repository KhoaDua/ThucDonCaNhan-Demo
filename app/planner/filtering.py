"""Sàng lọc món ăn (FR-05) và tính sẵn phương án khẩu phần cho bộ lập thực đơn.

- Loại món chứa nguyên liệu cần tránh.
- Loại món thiếu dữ liệu cần để kiểm tra điều kiện bắt buộc (thiếu dòng dinh dưỡng, mã USDA
  không tồn tại, thiếu giá khi ngân sách bắt buộc, chưa rà soát nguyên liệu khi có danh sách tránh).
- Giá trị thiếu KHÔNG được tính là 0; dòng có giá trị 0 thật vẫn là dữ liệu đã có.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.catalog.health import classify_ingredients
from app.planner.checklist import ingredient_terms, name_key
from app.planner.nutrition import dish_nutrition, grams_by_food, usage_cost

MISSING_REASONS = {"missing_food", "missing_nutrient", "missing_price", "missing_package", "missing_prep", "needs_review"}
REASON_TEXT = {
    "avoid": "chứa nguyên liệu cần tránh",
    "prep_time": "vượt thời gian nấu bắt buộc",
    "missing_food": "mã nguyên liệu không có trong USDA",
    "missing_nutrient": "thiếu dữ liệu dinh dưỡng cho điều kiện bắt buộc",
    "missing_price": "thiếu giá cho ngân sách bắt buộc",
    "missing_package": "thiếu quy cách gói (cùng cơ sở khối lượng) cho ngân sách theo tiền mua",
    "missing_prep": "thiếu thời gian chuẩn bị cho điều kiện thời gian nấu",
    "needs_review": "nguyên liệu chưa được rà soát với danh sách cần tránh",
}


def _has_phrase(term: str, text: str) -> bool:
    if not term or not text:
        return False
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text.lower()) is not None


def match_avoid(term: str, info: dict | None, usda_desc: str | None) -> str:
    """'match' | 'no_match' | 'unknown' cho một nguyên liệu và một mục cần tránh."""
    t = term.strip().lower()
    if info:
        tags = [x.strip().lower() for x in (info.get("avoid_tags") or "").split(";") if x.strip()]
        names = [info.get("name_vi") or ""] + [x for x in (info.get("aliases") or "").split(";")]
        if t in tags or any(_has_phrase(t, n) for n in names):
            return "match"
    if usda_desc and _has_phrase(t, usda_desc):
        return "match"
    if info and info.get("review_status") == "reviewed":
        return "no_match"
    return "unknown"


def ingredient_exclusion(exclusion, fdc_id, info, description):
    """Trả các lựa chọn bị vi phạm và các lựa chọn chưa đủ dữ liệu để kiểm tra."""
    matches, unknown = [], []
    for term in exclusion.get("terms", ()):
        outcome = match_avoid(term, info, description)
        if outcome == "match":
            matches.append(term)
        elif outcome == "unknown":
            unknown.append(term)
    if exclusion.get("ingredients"):
        terms = {name_key(term) for term in ingredient_terms(info or {})}
        for choice in exclusion["ingredients"]:
            if fdc_id in choice["fdc_ids"] or choice["key"] in terms:
                matches.append(choice["name"])
            elif not info or info["review_status"] != "reviewed":
                unknown.append(choice["name"])
    return matches, unknown


def dish_exclusion(exclusion, recipe_id, name):
    return (recipe_id in exclusion.get("dishes", {})
            or any(_has_phrase(term, name) for term in exclusion.get("terms", ())))


def text_matches(terms: list[str], recipe: dict, catalog) -> bool:
    for t in terms:
        if _has_phrase(t, recipe["name_vi"]):
            return True
        for ing in recipe["ingredients"]:
            info = catalog.get_ingredient_info(ing["fdc_id"])
            if info and match_avoid(t, info, None) == "match":
                return True
    return False


@dataclass
class ScreenResult:
    eligible: list = field(default_factory=list)  # [{recipe, options}]
    excluded: list = field(default_factory=list)  # [{recipe_id, name, reasons:[{code, detail}]}]
    not_for_meals: list = field(default_factory=list)

    def missing_data_exclusions(self):
        return [e for e in self.excluded if any(r["code"] in MISSING_REASONS for r in e["reasons"])]

    def constraint_exclusions(self):
        return [e for e in self.excluded if not any(r["code"] in MISSING_REASONS for r in e["reasons"])]


def screen(catalog, profile: dict, constraints: list[dict]) -> ScreenResult:
    res = ScreenResult()
    hard_metrics = sorted({c["metric"] for c in constraints if c["kind"] == "nutrient"})
    hard_metrics = sorted(set(hard_metrics) | {m for c in constraints if c['kind']=='nutrient_ratio' for m in c['terms']})
    exclusions = [c for c in constraints if c["kind"] == "exclude"]
    ingredient_exclusions = [c for c in exclusions if c.get("terms") or c.get("ingredients")]
    groups = [c for c in constraints if c["kind"] == "food_group"]
    budget = next((c for c in constraints if c["kind"] == "budget"), None)
    prep = next((c for c in constraints if c["kind"] == "prep_time"), None)
    meals = set(profile["meals"])

    for rec in catalog.list_active_recipes():
        if not meals.intersection(rec["meal_types"]):
            res.not_for_meals.append(rec["recipe_id"])
            continue
        reasons = []
        if any(dish_exclusion(c, rec["recipe_id"], rec["name_vi"]) for c in exclusions):
            res.excluded.append({"recipe_id": rec["recipe_id"], "version": rec["version"], "name": rec["name_vi"],
                                 "reasons": [{"code": "avoid", "detail": f"{rec['name_vi']} thuộc danh sách cần tránh"}]})
            continue
        group_counts = {}
        for group in groups:
            count, unknown = classify_ingredients((i["fdc_id"] for i in rec["ingredients"]), group)
            group_counts[group["cid"]] = count
            if count is None:
                names = ", ".join(f"{catalog.ingredient_name(fdc)} (FDC {fdc})" for fdc in sorted(unknown))
                reasons.append({"code": "needs_review", "detail": f"{group['name']} chưa phân loại: {names}"})
            elif count > group["value"]:
                reasons.append({"code": "avoid", "detail": group["label"]})
        for ing in rec["ingredients"]:
            fdc = ing["fdc_id"]
            food = catalog.get_food(fdc)
            name = catalog.ingredient_name(fdc)
            if food is None:
                reasons.append({"code": "missing_food", "detail": f"dòng {ing['line_no']}: fdc_id {fdc}"})
                continue
            if ingredient_exclusions:
                info = catalog.get_ingredient_info(fdc)
                for exclusion in ingredient_exclusions:
                    matches, unknown = ingredient_exclusion(exclusion, fdc, info, food["description"])
                    reasons.extend({"code": "avoid", "detail": f"{name} (fdc {fdc}) thuộc '{term}'"} for term in matches)
                    reasons.extend({"code": "needs_review", "detail": f"{name} (fdc {fdc}) chưa rà soát với '{term}'"}
                                   for term in unknown)
            if hard_metrics:
                nut = catalog.get_food_nutrients(fdc)
                for m_ in hard_metrics:
                    if m_ not in nut:
                        reasons.append({"code": "missing_nutrient",
                                        "detail": f"{name} (fdc {fdc}) không có dòng {m_} trong USDA {catalog.release}"})
            if budget:
                p = catalog.get_price(fdc)
                if not p or p.get("price") is None:
                    reasons.append({"code": "missing_price", "detail": f"{name} (fdc {fdc}) chưa có giá"})
                elif not p.get("package_g") or (p.get("mass_basis") or "edible") != "edible":
                    reasons.append({"code": "missing_package",
                                    "detail": f"{name} (fdc {fdc}) chưa có quy cách theo khối lượng phần ăn được"})
        if prep:
            if rec.get("prep_minutes") is None:
                reasons.append({"code": "missing_prep", "detail": "công thức chưa có prep_minutes"})
            elif rec["prep_minutes"] > prep["value"]:
                reasons.append({"code": "prep_time", "detail": f"{rec['prep_minutes']:g} phút > {prep['value']:g} phút"})
        # gộp lý do trùng
        uniq, seen = [], set()
        for r in reasons:
            k = (r["code"], r["detail"])
            if k not in seen:
                seen.add(k)
                uniq.append(r)
        if uniq:
            res.excluded.append({"recipe_id": rec["recipe_id"], "version": rec["version"], "name": rec["name_vi"], "reasons": uniq})
            continue
        res.eligible.append({"recipe": rec, "options": [], "group_counts": group_counts})
    return res


def build_options(catalog, profile: dict, prefs: list[dict], screen_res: ScreenResult, feedback: dict) -> dict:
    """Tính dinh dưỡng, chi phí và điểm ưu tiên cho từng (món, mức khẩu phần). Trả thông tin chi phí."""
    pref = {p["kind"]: p for p in prefs}
    from app.planner.disease import requested, build
    ratio_cons = build(profile,catalog,'demo')[0] if requested(profile) else []
    ratio_cons = [c for c in ratio_cons if c['kind']=='nutrient_ratio']
    inv_ids = {i["fdc_id"] for i in profile.get("inventory") or []}
    cost_pref = pref.get("cost")
    group_prefs = [p for p in prefs if p["kind"] == "food_group"]
    cost_missing = []
    n_slots = max(1, (profile["days"] or 1) * len(profile["meals"]) * (profile["dishes_per_meal"] or 1))
    for cand in screen_res.eligible:
        rec = cand["recipe"]
        fb = feedback.get(rec["recipe_id"])
        detail_base = {}
        s = 0.0
        for group in group_prefs:
            count, _ = classify_ingredients((i["fdc_id"] for i in rec["ingredients"]), group)
            if count != 0:
                s += group["weight"]
                detail_base[group["label"]] = group["weight"]
        if "like" in pref and text_matches(pref["like"]["terms"], rec, catalog):
            s -= pref["like"]["weight"]
            detail_base["thích"] = -pref["like"]["weight"]
        if "dislike" in pref and text_matches(pref["dislike"]["terms"], rec, catalog):
            s += pref["dislike"]["weight"]
            detail_base["không thích"] = pref["dislike"]["weight"]
        if fb and fb.get("liked") in (1, -1):
            w = pref["feedback"]["weight"]
            s += -w if fb["liked"] == 1 else w
            detail_base["phản hồi"] = -w if fb["liked"] == 1 else w
        minutes = fb.get("cook_minutes") if fb and fb.get("cook_minutes") else rec.get("prep_minutes")
        pt = pref.get("prep_time")
        if pt:
            if minutes is None:
                pen = pt["weight"] * 0.5
            elif pt.get("max_minutes"):
                pen = pt["weight"] * max(0.0, minutes - pt["max_minutes"]) / 10.0
            else:
                pen = pt["weight"] * minutes / 60.0
            s += pen
            detail_base["thời gian nấu"] = round(pen, 3)
        if "inventory" in pref and inv_ids.intersection(i["fdc_id"] for i in rec["ingredients"]):
            s -= pref["inventory"]["weight"]
            detail_base["dùng tồn kho"] = -pref["inventory"]["weight"]
        for lvl in profile["serving_levels"]:
            dn = dish_nutrition(rec, lvl, catalog.get_food_nutrients)
            import math
            derived = {c['cid']:math.fsum(dn['totals'][m]*a for m,a in c['terms'].items()) for c in ratio_cons}
            grams = grams_by_food(rec, lvl)
            cost, miss = usage_cost(grams, catalog.get_price)
            opt_score = s + 0.01 * abs(lvl - 1.0)
            detail = dict(detail_base)
            if cost_pref:
                if cost is None:
                    cost_missing.append(rec["recipe_id"])
                else:
                    ref = cost_pref["amount"] / (n_slots if cost_pref["scope"] == "period" else
                                                 max(1, len(profile["meals"]) * profile["dishes_per_meal"]))
                    c_pen = cost_pref["weight"] * cost / ref if ref > 0 else 0.0
                    opt_score += c_pen
                    detail["chi phí"] = round(c_pen, 3)
            cand["options"].append({"recipe_id": rec["recipe_id"], "version": rec["version"], "servings": lvl,
                                    "totals": {**dn["totals"], **cand.get("group_counts", {}), **derived}, "cost_usage": cost, "grams": grams,
                                    "score": opt_score, "score_detail": detail, "meal_types": rec["meal_types"]})
    return {"cost_missing": sorted(set(cost_missing))}


def strip_cost_scores(screen_res: ScreenResult) -> None:
    """Bỏ tiêu chí chi phí khỏi xếp hạng (chỉ khi người dùng đã xác nhận)."""
    for cand in screen_res.eligible:
        for o in cand["options"]:
            c = o["score_detail"].pop("chi phí", None)
            if c:
                o["score"] -= c
