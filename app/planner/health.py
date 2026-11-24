"""Đọc câu trả lời và tạo điều kiện thực đơn từ bộ quy tắc theo bệnh."""
from __future__ import annotations

from app.catalog.health import CONDITIONS, find_policies
from app.catalog.lookup import MEAL_LABELS, Catalog
from app.planner import disease
from app.planner.constraints import nutrient_label
from app.planner.energy import integrity_errors as energy_integrity_errors
from app.planner.profile import SCOPES, _all, _first


REVIEW_LABELS = {
    "ready": "Đủ điều kiện xác nhận",
    "needs_information": "Cần bổ sung thông tin",
    "pending_review": "Chờ bộ quy tắc được duyệt",
}


def parse_health(form):
    selected = list(dict.fromkeys(_all(form, "health_conditions")))
    errors = {}
    if any(c not in CONDITIONS and c != "none" for c in selected) or ("none" in selected and len(selected) > 1):
        errors["health_conditions"] = "Chọn tình trạng đã được chẩn đoán hoặc chọn Không có."
    health = {"intake_version": 3, "conditions": [c for c in selected if c in CONDITIONS],
              "declared_none": "none" in selected}
    if health['conditions'] and _first(form,'health_simulation_version'):
        scenario = _first(form, "health_scenario")
        if _first(form, "health_simulation_version") != "1":
            errors["health_scenario"] = "Kịch bản mô phỏng đã thay đổi. Hãy tải lại trang."
        elif scenario not in disease.POLICY["scenarios"]:
            errors["health_scenario"] = "Chọn kịch bản giữ cân hoặc giảm cân theo bệnh."
        # Lựa chọn hợp lệ xác định phạm vi giả định của kịch bản DEMO.
        health.update(simulation_version=1, simulation_scenario=scenario, simulation_scope_confirmed=not errors)
    return health, errors


def assess_health(conn, health, *, profile=None, data_mode=None):
    if not health:
        return {"state": "ready", "reasons": []}, [], []
    if health.get("intake_version") != 3 or health.get("migration_required"):
        return {"state": "needs_information", "reasons": [{"state": "needs_information", "field": "food_checklist",
                "message": "Checklist đã thay đổi. Hãy chọn lại danh sách thực phẩm trước khi lập thực đơn."}]}, [], []
    if health.get('simulation_version') == 1 and health.get('conditions'):
        if profile is None:
            return {'state':'needs_information','reasons':[{'field':'body','message':'Bổ sung thông tin cơ thể cho kịch bản mô phỏng.'}]},[],[]
        try:
            constraints,result = disease.build(profile,Catalog(conn,'demo'),data_mode or 'demo')
            profile['disease_result'] = result
            return {'state':'ready','reasons':[]},constraints,[]
        except ValueError as exc:
            return {'state':'needs_information','reasons':[{'state':'needs_information','field':'health_scenario','message':str(exc)}]},[],[]
    reasons, constraints, preferences = [], [], []
    if not health.get("conditions") and not health.get("declared_none"):
        reasons.append({"state": "needs_information", "field": "health_conditions",
                        "message": "Chọn tình trạng đã được chẩn đoán hoặc xác nhận Không có."})
    for condition in health.get("conditions", []):
        if condition not in CONDITIONS:
            reasons.append({"state": "needs_information", "field": "health_conditions", "message": "Tình trạng đã chọn không được hỗ trợ."})
            continue
        cs, ps, es = health_rules(conn, "operational", {**health, "conditions": [condition]})
        constraints.extend(cs)
        preferences.extend(ps)
        for field, message in es.items():
            reasons.append({"state": "pending_review", "field": field, "message": message})
    state = "needs_information" if any(r["state"] == "needs_information" for r in reasons) else "pending_review" if reasons else "ready"
    return {"state": state, "reasons": reasons}, constraints, preferences


def generation_blockers(conn, profile_version, *, data_mode=None):
    """Tính lại điều kiện mở lập thực đơn, không tin trạng thái do trình duyệt gửi lên."""
    profile = profile_version["data"]
    checklist = profile.get("checklist") or {}
    if checklist and checklist.get("food_checklist_version") != 1:
        return ["Checklist đã thay đổi. Hãy chọn lại danh sách thực phẩm trước khi lập thực đơn."]
    if checklist.get("allergen_selection_required") or checklist.get("allergen_not_listed"):
        return ["Danh sách dị ứng đã cập nhật. Hãy chọn lại thực phẩm dị ứng trước khi lập thực đơn."]
    energy_errors = energy_integrity_errors(profile, profile_version["constraints"])
    if energy_errors:
        return energy_errors
    if disease.requested(profile):
        if data_mode is not None and data_mode != "demo":
            return ["Bộ quy tắc bệnh mô phỏng chỉ hoạt động trong chế độ DEMO."]
        return disease.integrity_errors(profile,profile_version['constraints'],Catalog(conn,'demo'),'demo')
    review, constraints, preferences = assess_health(conn, checklist.get("health"))
    if review["state"] != "ready":
        return [r["message"] for r in review["reasons"]]
    old = [c for c in profile_version["constraints"] if c.get("condition")]
    old_prefs = [p for p in profile_version["preferences"] if p.get("condition")]
    if old != constraints or old_prefs != preferences:
        return ["Bộ quy tắc sức khỏe đã thay đổi hoặc hồ sơ cũ chưa đủ thông tin. Hãy cập nhật và xác nhận lại checklist."]
    return []


def health_rules(conn, mode, health):
    constraints, preferences, errors = [], [], {}
    for condition in health.get("conditions", ()):
        definition = CONDITIONS[condition]
        policies = find_policies(conn, condition, mode, {}) if health.get("intake_version") == 3 else []
        label = definition["label"]
        if len(policies) != 1:
            errors["health_conditions"] = (
                f"Chưa có bộ quy tắc đã duyệt cho {label} theo checklist thực phẩm mới. Bạn đã khai báo xong; hãy chọn kịch bản mô phỏng trên trang hồ sơ."
                if not policies else f"Có nhiều bộ quy tắc {label} cùng áp dụng. Cần xác định một bộ trước khi lập thực đơn.")
            continue
        policy = policies[0]
        source = f"{policy['policy_id']} v{policy['version']} · {policy['source']}"
        if mode == "operational":
            source += f" · Duyệt bởi {policy['reviewed_by']}"
        else:
            source += " [GIẢ LẬP]"
        for index, rule in enumerate(policy["rules"], start=1):
            common = {"cid": f"health:{policy['policy_id']}:{index}", "condition": condition,
                      "source": source, "source_version": policy["version"], "demo_rule": mode == "demo",
                      "editable": False}
            tag = " [GIẢ LẬP]" if mode == "demo" else ""
            if rule["kind"] == "nutrient":
                name = nutrient_label(*(rule[key] for key in ("metric", "op", "value", "unit", "scope")))
                if rule.get("meal_type"):
                    name += " · " + MEAL_LABELS[rule["meal_type"]]
                constraints.append({**rule, **common, "label": f"{label} · {name}{tag}",
                                    "hard": True, "category": "health_limit"})
            elif "weight" in rule and rule["weight"] is not None:
                preferences.append({**rule, **common,
                                    "label": f"{label} · Ưu tiên món đã xác định nằm ngoài nhóm {rule['name']}{tag}"})
            else:
                cap, scope = rule["max_dishes"], rule["scope"]
                name = (f"Loại món thuộc nhóm {rule['name']}" if cap == 0 else
                        f"Tối đa {cap} món thuộc nhóm {rule['name']} {SCOPES[scope]}")
                constraints.append({**rule, **common, "label": f"{label} · {name}{tag}",
                                    "hard": True, "op": "<=", "value": cap, "unit": "món"})
    return constraints, preferences, errors
