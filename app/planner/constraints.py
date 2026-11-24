"""Thiết lập ràng buộc và ưu tiên (FR-02, FR-03).

Điều kiện BẮT BUỘC giới hạn tập phương án: loại trừ nguyên liệu, giới hạn dinh dưỡng đã xác nhận,
ngân sách (nếu đặt là bắt buộc), thời gian nấu (nếu đặt là bắt buộc).
Tiêu chí ƯU TIÊN chỉ xếp hạng phương án hợp lệ: khẩu vị, phản hồi, ít lặp món, nấu nhanh,
tận dụng tồn kho, chi phí (khi so sánh được).

Mỗi điều kiện lưu chỉ tiêu, phép so sánh, giá trị, đơn vị, kỳ áp dụng, nguồn và phiên bản.
`editable` cho biết người dùng có quyền nới điều kiện này hay không (FR-09 chỉ gợi ý điều kiện
editable). Giới hạn sức khỏe và danh sách tránh không bao giờ được gợi ý nới.
"""
from __future__ import annotations

import math

from app.catalog.lookup import METRICS
from app.planner.profile import SCOPES, normalize_value
from app.planner.energy import energy_constraints, enabled as personal_energy_enabled

OP_TEXT = {"<=": "≤", "<": "<", ">=": "≥", ">": ">", "==": "="}


def fmt(v: float) -> str:
    if v is None:
        return "—"
    s = f"{v:,.2f}".rstrip("0").rstrip(".")
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def nutrient_label(metric, op, value, unit, scope) -> str:
    return f"{METRICS[metric][2]} {OP_TEXT[op]} {fmt(value)} {unit} {SCOPES[scope]}"


def _rule_constraint(r, data_mode):
    is_demo = r["label"] == "demo"
    if is_demo and data_mode != "demo":
        raise ValueError("Bộ quy tắc giả lập chỉ dùng trong chế độ demo")
    if not is_demo and r["review_status"] != "reviewed":
        raise ValueError("Bộ quy tắc chưa được nhóm xác nhận")
    metric, op, scope = r["metric"], r["op"], r["scope"]
    if metric not in METRICS or op not in OP_TEXT or scope not in SCOPES:
        raise ValueError("Bộ quy tắc có chỉ tiêu, phép so sánh hoặc kỳ áp dụng không hợp lệ")
    value = normalize_value(metric, r["value"], r["unit"])
    if not math.isfinite(value) or value < 0:
        raise ValueError("Bộ quy tắc có giá trị giới hạn không hợp lệ")
    unit = METRICS[metric][1]
    tag = " [GIẢ LẬP]" if is_demo else ""
    rs = r["rule_set"]
    return {"cid": f"rule:{rs}:{metric}:{op}:{scope}", "kind": "nutrient", "hard": True,
            "metric": metric, "op": op, "value": value, "unit": unit, "scope": scope,
            "category": "health_limit", "editable": False, "demo_rule": is_demo,
            "source": f"{rs} v{r['version']} – {r['source_ref']}{tag}", "source_version": r["version"],
            "label": nutrient_label(metric, op, value, unit, scope) + tag}


def build_constraints(profile: dict, catalog, data_mode: str, *, checklist_rules=(), health_constraints=(),
                      health_preferences=()) -> tuple[list[dict], list[dict], dict]:
    cons: list[dict] = list(health_constraints)
    prefs: list[dict] = list(health_preferences)
    errors: dict[str, str] = {}
    from app.planner import disease
    if disease.requested(profile):
        try:
            generated, summary = disease.build(profile,catalog,data_mode)
            cons = [c for c in cons if not c.get('cid','').startswith('disease:')] + generated
            profile['disease_result'] = summary
        except ValueError as exc:
            errors['health_scenario'] = str(exc)
    try:
        if personal_energy_enabled(profile) and data_mode != "demo":
            raise ValueError("Bộ quy tắc năng lượng cá nhân hiện chỉ áp dụng trong chế độ thử nghiệm.")
        personal_cons, result = energy_constraints(profile)
        cons.extend(personal_cons)
        if result:
            profile["body_result"] = result
    except ValueError as exc:
        personal_cons = []
        errors["body"] = str(exc)

    if profile.get("avoid"):
        cons.append({"cid": "exclude", "kind": "exclude", "hard": True, "terms": list(profile["avoid"]),
                     "label": "Không dùng nguyên liệu: " + ", ".join(profile["avoid"]),
                     "source": "Hồ sơ người dùng đã xác nhận", "editable": False, "scope": "period"})

    checklist = profile.get("checklist") or {}
    known_ids = {row["fdc_id"] for row in catalog.reviewed_ingredients()}
    for choice in checklist.get("limited_ingredients", []):
        members = set(choice["fdc_ids"])
        prefs.append({"cid": "food_limit:" + choice["id"], "kind": "food_group", "name": choice["name"],
                      "members": sorted(members), "nonmembers": sorted(known_ids - members), "weight": 3.0,
                      "classification_source": "Mã nguyên liệu tương ứng lựa chọn trong checklist",
                      "source": "Nhu cầu người dùng", "label": "Ưu tiên hạn chế " + choice["name"]})
    for key, label in (("allergies", "Dị ứng"), ("ingredients", "Nguyên liệu cần tránh"),
                       ("gout_ingredients", "Gout · Nguyên liệu bạn muốn tránh")):
        choices = checklist.get(key)
        if choices:
            cons.append({"cid": key, "kind": "exclude", "hard": True, "ingredients": choices,
                         "label": label + " · " + ", ".join(choice["name"] for choice in choices),
                         "source": "Câu trả lời trong checklist", "editable": False, "scope": "period"})
    for key, label in (("dishes", "Món cần tránh"), ("gout_dishes", "Gout · Món bạn muốn tránh")):
        if dishes := checklist.get(key):
            cons.append({"cid": f"exclude_{key}", "kind": "exclude", "hard": True, "dishes": dishes,
                         "label": label + " · " + ", ".join(dishes.values()),
                         "source": "Câu trả lời trong checklist", "editable": False, "scope": "period"})

    rs = profile.get("rule_set")
    if rs:
        rules = catalog.get_rules(rs)
        if not rules:
            errors["rule_set"] = f"Không tìm thấy bộ quy tắc {rs}"
        elif not profile.get("rule_set_confirmed"):
            errors["rule_set"] = "Cần xác nhận áp dụng bộ quy tắc cho hồ sơ này"
        else:
            try:
                cons.extend(_rule_constraint(r, data_mode) for r in rules
                            if not (personal_energy_enabled(profile) and r["label"] == "demo" and r["metric"] == "energy_kcal"))
            except ValueError as exc:
                errors["rule_set"] = str(exc)

    try:
        known = {c["cid"] for c in cons}
        cons.extend(c for r in checklist_rules
                    if not (personal_energy_enabled(profile) and r["label"] == "demo" and r["metric"] == "energy_kcal")
                    if (c := _rule_constraint(r, data_mode))["cid"] not in known)
    except ValueError as exc:
        errors["goals"] = str(exc)

    for lim in profile.get("nutrient_limits") or []:
        if not lim.get("confirmed") or not lim.get("source"):
            continue  # đã báo lỗi ở bước đọc checklist; không áp dụng giới hạn chưa xác nhận
        value = normalize_value(lim["metric"], lim["value"], lim["unit"])
        unit = METRICS[lim["metric"]][1]
        cat = lim.get("category", "health_limit")
        cons.append({"cid": f"user:{lim['metric']}:{lim['op']}:{lim['scope']}:{lim.get('row', 0)}", "kind": "nutrient",
                     "hard": True, "metric": lim["metric"], "op": lim["op"], "value": value, "unit": unit,
                     "scope": lim["scope"], "category": cat, "editable": cat == "personal_goal",
                     "original": f"{OP_TEXT[lim['op']]} {fmt(lim['value'])} {lim['unit']}",
                     "source": f"Người dùng xác nhận – {lim['source']}", "source_version": None,
                     "label": nutrient_label(lim["metric"], lim["op"], value, unit, lim["scope"])})

    if checklist_rules or health_constraints or personal_cons:
        nutrition = [c for c in cons if c["kind"] == "nutrient"]
        lowers = [c for c in nutrition if c["op"] in (">", ">=", "==")]
        uppers = [c for c in nutrition if c["op"] in ("<", "<=", "==")]
        for lower in lowers:
            for upper in uppers:
                if (lower["metric"], lower["scope"]) != (upper["metric"], upper["scope"]):
                    continue
                if lower.get("meal_type") and upper.get("meal_type") and lower["meal_type"] != upper["meal_type"]:
                    continue
                same_boundary = lower["value"] == upper["value"]
                strict = lower["op"] == ">" or upper["op"] == "<"
                if lower["value"] > upper["value"] or (same_boundary and strict):
                    field = "health_conditions" if lower.get("condition") or upper.get("condition") else "goals"
                    errors[field] = f"Hai giới hạn mâu thuẫn: {lower['label']} và {upper['label']}."

    b = profile.get("budget")
    if b and b.get("hard"):
        mode_txt = "tiền mua cả gói" if b["mode"] == "purchase" else "chi phí theo lượng sử dụng"
        cons.append({"cid": "budget", "kind": "budget", "hard": True, "op": "<=", "value": float(b["amount"]),
                     "unit": "VND", "scope": b["scope"], "mode": b["mode"], "editable": True,
                     "source": "Hồ sơ người dùng", "label": f"Ngân sách ≤ {fmt(b['amount'])} VND {SCOPES[b['scope']]} ({mode_txt})"})
    elif b:
        prefs.append({"kind": "cost", "weight": 1.0, "mode": b["mode"], "amount": b["amount"], "scope": b["scope"],
                      "drop_if_missing": bool(b.get("drop_cost_if_missing")),
                      "label": f"Ưu tiên chi phí thấp (mục tiêu {fmt(b['amount'])} VND {SCOPES[b['scope']]})"})

    if profile.get("max_prep_minutes"):
        if profile.get("prep_hard"):
            cons.append({"cid": "prep_time", "kind": "prep_time", "hard": True, "op": "<=",
                         "value": float(profile["max_prep_minutes"]), "unit": "phút", "scope": "meal", "editable": True,
                         "source": "Hồ sơ người dùng", "label": f"Thời gian nấu mỗi món ≤ {fmt(profile['max_prep_minutes'])} phút"})
        else:
            prefs.append({"kind": "prep_time", "weight": 1.0, "max_minutes": float(profile["max_prep_minutes"]),
                          "label": f"Ưu tiên món nấu trong {fmt(profile['max_prep_minutes'])} phút"})
    else:
        prefs.append({"kind": "prep_time", "weight": 0.3, "max_minutes": None, "label": "Ưu tiên nhẹ món nấu nhanh"})

    if profile.get("likes"):
        prefs.append({"kind": "like", "weight": 3.0, "terms": profile["likes"], "label": "Thích: " + ", ".join(profile["likes"])})
    if profile.get("dislikes"):
        prefs.append({"kind": "dislike", "weight": 3.0, "terms": profile["dislikes"],
                      "label": "Không thích (chỉ giảm ưu tiên): " + ", ".join(profile["dislikes"])})
    if profile.get("prefer_variety"):
        prefs.append({"kind": "variety", "weight": 4.0, "label": "Ít lặp món trong kỳ"})
    if profile.get("inventory"):
        prefs.append({"kind": "inventory", "weight": 1.0, "label": "Ưu tiên dùng nguyên liệu đang có"})
    prefs.append({"kind": "feedback", "weight": 3.0, "label": "Phản hồi thích/không thích và thời gian nấu thực tế (bản mới nhất mỗi món)"})

    seen = set()
    for c in cons:  # mã điều kiện duy nhất
        while c["cid"] in seen:
            c["cid"] += "'"
        seen.add(c["cid"])
    return cons, prefs, errors


def adjustable_parameters(profile: dict) -> list[str]:
    """Các thông số người dùng được phép đổi (dùng cho gợi ý FR-09)."""
    out = ["Thêm mức khẩu phần (hiện: " + "; ".join(f"{v:g}" for v in profile.get("serving_levels", [])) + ")",
           f"Số món mỗi bữa (hiện: {profile.get('dishes_per_meal')})",
           "Danh sách bữa hoặc số ngày"]
    if profile.get("dislikes") or profile.get("likes"):
        out.append("Sở thích chỉ ảnh hưởng thứ tự, không làm bài toán không khả thi")
    return out
