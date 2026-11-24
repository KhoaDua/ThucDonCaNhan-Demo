"""BMI và năng lượng duy trì DEMO; bổ sung vào ràng buộc nutrient hiện có."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

POLICY = json.loads(Path(__file__).with_name("energy_policy.json").read_text(encoding="utf-8"))
BODY_FIELDS = ("height_cm", "weight_kg", "age_years", "sex", "activity", "scope_confirmed")
GROUP_LABELS = {"underweight": "Thiếu cân", "normal": "Bình thường", "overweight": "Thừa cân"}
MEAL_LABELS = {"breakfast": "Bữa sáng", "lunch": "Bữa trưa", "dinner": "Bữa tối"}


def has_energy_inputs(body):
    """Kiểm tra bộ đầu vào đã được chuẩn hóa có đủ để tính năng lượng."""
    return bool(body and all(body.get(key) for key in BODY_FIELDS if key != "scope_confirmed"))


def enabled(profile):
    from app.planner.disease import requested
    body = profile.get("body")
    return (
        "energy" in (profile.get("checklist") or {}).get("goals", [])
        or requested(profile)
        or (has_energy_inputs(body) and body.get("scope_confirmed") is True)
    )


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("giá trị không phải số")
    text = str(value).strip().replace(",", ".")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
        raise ValueError("cần nhập số dương, dùng dấu phẩy hoặc dấu chấm cho phần thập phân")
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("giá trị không hữu hạn")
    return number


def validate_body(data, *, required=False):
    """Chuẩn hóa chỉ các trường đầu vào; không tin BMI/calo do phía trình duyệt gửi."""
    if not data and not required:
        return None, {}
    if data is not None and not isinstance(data, dict):
        return None, {"body": "Thông tin cơ thể không hợp lệ."}
    raw = data or {}
    if not required and not any(raw.get(k) not in (None, "", False) for k in BODY_FIELDS):
        return None, {}
    result, errors = {}, {}
    labels = {"height_cm": "Chiều cao", "weight_kg": "Cân nặng", "age_years": "Tuổi"}
    ranges = {"height_cm": POLICY["height_range_cm"], "weight_kg": POLICY["weight_range_kg"],
              "age_years": [POLICY["age_min"], POLICY["age_max_input"]]}
    for key in labels:
        value = raw.get(key)
        if value in (None, ""):
            result[key] = None
            if required:
                errors["body_" + key] = "Nhập " + labels[key].lower() + " để tính năng lượng cá nhân."
            continue
        try:
            number = _decimal(value)
            if key == "age_years" and not number.is_integer():
                raise ValueError("cần là số nguyên")
            lo, hi = ranges[key]
            if not lo <= number <= hi:
                raise ValueError(f"cần từ {lo} đến {hi}; hãy kiểm tra lại giá trị và đơn vị")
            result[key] = int(number) if key == "age_years" else number
        except ValueError as exc:
            errors["body_" + key] = labels[key] + " " + str(exc) + "."
    for key, values, label in (("sex", POLICY["sex_labels"], "giới tính dùng trong công thức"),
                               ("activity", POLICY["activity"], "mức vận động")):
        value = raw.get(key) or ""
        result[key] = value
        if (required or value) and (not isinstance(value, str) or value not in values):
            errors["body_" + key] = "Chọn " + label + "."
    result["scope_confirmed"] = raw.get("scope_confirmed") in (True, "1")
    if result["scope_confirmed"] and raw.get("scope_source") in ("answer", "simulation"):
        result["scope_source"] = raw["scope_source"]
    if required and not result["scope_confirmed"]:
        errors["body_scope_confirmed"] = "Xác nhận thuộc nhóm từ 18 tuổi, không mang thai hoặc cho con bú để áp dụng module."
    return result, errors


def parse_body(form, *, required=False, simulated_scope=False):
    def first(key):
        value = form.get("body_" + key, "")
        return (value[0] if value else "") if isinstance(value, list) else value
    raw = {key: first(key) for key in BODY_FIELDS}
    answer_field = "body_pregnancy_or_breastfeeding"
    # Hồ sơ/API cũ vẫn đọc được. Checklist mới gửi phiên bản để tránh lấy xác nhận cũ.
    if "body_scope_confirmed" in form and "body_scope_version" not in form and answer_field not in form:
        return validate_body(raw, required=required)

    answer = first("pregnancy_or_breastfeeding")
    from_simulation = required and simulated_scope and answer == ""
    raw["scope_confirmed"] = from_simulation or answer == "no"
    raw["scope_source"] = "simulation" if from_simulation else "answer"
    body, errors = validate_body(raw, required=required)
    needs_answer = not required and not errors and has_energy_inputs(body) and answer not in ("yes", "no")
    if errors.pop("body_scope_confirmed", None) or needs_answer:
        errors[answer_field] = (
            "Mục tiêu calo cá nhân chưa áp dụng khi đang mang thai hoặc cho con bú."
            if answer == "yes" else "Chọn Có hoặc Không để xác định phạm vi tính calo."
        )
    return body, errors


def calculate(body):
    body, errors = validate_body(body)
    if errors:
        raise ValueError(" ".join(errors.values()))
    if not body or body.get("height_cm") is None or body.get("weight_kg") is None:
        return None
    bmi = body["weight_kg"] / (body["height_cm"] / 100.0) ** 2
    adult = body.get("age_years") is not None and body["age_years"] >= 18 and body["scope_confirmed"]
    group = ("underweight" if bmi < POLICY["bmi"]["underweight_below"] else "normal" if bmi < POLICY["bmi"]["overweight_from"] else "overweight") if adult else None
    out = {"policy_id": POLICY["policy_id"], "policy_version": POLICY["version"], "bmi": bmi,
           "group": group, "group_label": GROUP_LABELS.get(group), "obesity_subgroup": adult and bmi >= POLICY["bmi"]["obesity_from"],
           "bmr": None, "day_target": None, "educational": True}
    if not adult or body.get("sex") not in POLICY["sex_labels"] or body.get("activity") not in POLICY["activity"]:
        return out
    band = next(b for b in POLICY["age_bands"] if body["age_years"] >= b["min_age"] and
                (b["max_age_exclusive"] is None or body["age_years"] < b["max_age_exclusive"]))
    a, b = band[body["sex"]]
    bmr = a * body["weight_kg"] + b
    pal = POLICY["activity"][body["activity"]]["factor"]
    out.update(bmr=bmr, pal=pal, day_target=bmr * pal)
    return out


def energy_constraints(profile):
    """Giới hạn các bữa được chọn; không chuẩn hóa lại tỷ lệ khi chỉ chọn một bữa."""
    if not enabled(profile):
        return [], None
    body, errors = validate_body(profile.get("body"), required=True)
    if errors:
        raise ValueError(" ".join(errors.values()))
    result = calculate(body)
    from app.planner.disease import requested, resolve_energy
    if requested(profile):
        result = resolve_energy(profile, result)
    selected = profile.get("meals", [])
    if not selected or any(m not in POLICY["meal_ratios"] for m in selected):
        raise ValueError("Chọn các bữa hợp lệ để phân bổ năng lượng.")
    day = result["day_target"]
    share = math.fsum(POLICY["meal_ratios"][m] for m in selected)
    planned = day * share
    result.update(selected_share=share, planned_target=planned, meals={})
    cons = []
    def add(target, scope, meal=None):
        for op, ratio in ((">=", 1 - POLICY["tolerance"]), ("<=", 1 + POLICY["tolerance"])):
            label = (MEAL_LABELS[meal] if meal else "Tổng các bữa đã chọn mỗi ngày" if share < 1 else "Tổng mỗi ngày")
            value = target * ratio
            cons.append({"cid": "body_energy:" + (meal or "day") + ":" + op, "kind": "nutrient", "hard": True,
                         "metric": "energy_kcal", "op": op, "value": value, "unit": "kcal", "scope": scope,
                         **({"meal_type": meal} if meal else {}), "category": "personal_goal", "editable": False,
                         "demo_rule": True, "source": POLICY["policy_id"] + " · Bộ quy tắc thử nghiệm đã xác nhận",
                         "source_version": POLICY["version"], "label": label + " · Năng lượng " +
                         ("≥" if op == ">=" else "≤") + f" {value:.1f} kcal (cá nhân)"})
    add(planned, "day")
    for meal in selected:
        target = day * POLICY["meal_ratios"][meal]
        result["meals"][meal] = {"target": target, "lower": target * (1 - POLICY["tolerance"]), "upper": target * (1 + POLICY["tolerance"]),
                                "ratio": POLICY["meal_ratios"][meal]}
        add(target, "meal", meal)
    result["planned_lower"], result["planned_upper"] = planned * (1 - POLICY["tolerance"]), planned * (1 + POLICY["tolerance"])
    if result.get("disease_policy_id"):
        for c in cons:
            c.update(category="health_limit", condition="+".join(sorted(profile["checklist"]["health"]["conditions"])),
                     source=result["disease_policy_id"] + " · Mục tiêu năng lượng theo bệnh DEMO",
                     source_version=result["disease_policy_version"])
    return cons, result


def integrity_errors(profile, constraints):
    """Tính lại từ đầu vào khi kiểm tra/lập/đổi món; không tin kết quả hoặc giới hạn khai sẵn."""
    try:
        expected, result = energy_constraints(profile)
    except ValueError as exc:
        return [str(exc)]
    actual = [c for c in constraints if str(c.get("cid", "")).startswith("body_energy:")]
    if actual != expected:
        return ["Giới hạn năng lượng cá nhân không khớp đầu vào. Hãy xem lại và xác nhận hồ sơ."]
    if result and profile.get("body_result") != result:
        return ["Kết quả BMI/năng lượng cá nhân không khớp đầu vào hoặc phiên bản quy tắc. Hãy cập nhật hồ sơ."]
    return []
