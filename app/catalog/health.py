"""Lưu bộ quy tắc theo bệnh và phân loại nguyên liệu có phiên bản."""
from __future__ import annotations

import json
import math
import re
from datetime import date

from app.catalog.lookup import METRICS, MEAL_ORDER
from app.storage.db import transaction


CONDITIONS = {
    "type2_diabetes": {"label": "Tiểu đường type 2", "policy_answers": ()},
    "gout": {"label": "Gout", "policy_answers": ()},
}

SCOPES = ("meal", "day", "period")
OPERATORS = ("<=", "<", ">=", ">")


def _validate_policy(data, mode):
    if not isinstance(data, dict):
        raise ValueError("Cần một bộ quy tắc dạng JSON.")
    policy_id, version, condition = (data.get(k) for k in ("policy_id", "version", "condition"))
    if not isinstance(policy_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", policy_id):
        raise ValueError("Mã bộ quy tắc không hợp lệ.")
    if type(version) is not int or not 1 <= version <= 2**63 - 1:
        raise ValueError("Phiên bản phải là số nguyên dương.")
    if not isinstance(condition, str) or condition not in CONDITIONS:
        raise ValueError("Mã tình trạng sức khỏe chưa được hỗ trợ.")
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        raise ValueError("Cần ghi nguồn của bộ quy tắc.")
    if mode == "operational" and (not isinstance(data.get("reviewed_by"), str) or not data["reviewed_by"].strip()):
        raise ValueError("Cần ghi người hoặc nhóm đã duyệt bộ quy tắc.")
    if mode == "operational":
        if not policy_is_reviewed(data):
            raise ValueError("Chỉ nạp quy tắc có review_status=reviewed, người duyệt và ngày duyệt hợp lệ (YYYY-MM-DD).")
    if data.get("intake_version") != 3 or "guidance_fingerprint" in data:
        raise ValueError("Bộ quy tắc cần được duyệt lại cho checklist thực phẩm mới (intake_version=3).")
    answers = data.get("answers")
    if (not isinstance(answers, dict) or set(answers) != set(CONDITIONS[condition]["policy_answers"])
            or any(answer not in ("yes", "no") for answer in answers.values())):
        raise ValueError("Cần khai báo đối tượng áp dụng bằng đầy đủ câu trả lời yes/no.")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError("Bộ quy tắc phải có ít nhất một điều kiện.")
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("Mỗi điều kiện phải là một đối tượng JSON.")
        kind = rule.get("kind")
        if rule.get("meal_type") is not None and (kind != "nutrient" or rule.get("scope") != "meal" or rule["meal_type"] not in MEAL_ORDER):
            raise ValueError("Giới hạn riêng cho bữa chỉ hỗ trợ điều kiện dinh dưỡng theo bữa sáng/trưa/tối.")
        if kind == "nutrient":
            metric, value = rule.get("metric"), rule.get("value")
            if (not isinstance(metric, str) or metric not in METRICS or rule.get("unit") != METRICS[metric][1]
                    or rule.get("scope") not in SCOPES or rule.get("op") not in OPERATORS
                    or not _valid_number(value) or value < 0):
                raise ValueError("Điều kiện dinh dưỡng cần chỉ tiêu, đơn vị chuẩn, phép so sánh, ngưỡng và kỳ hợp lệ.")
        elif kind == "food_group":
            _validate_group(rule)
        else:
            raise ValueError("Loại điều kiện chưa được hỗ trợ.")
    if condition == "type2_diabetes" and not any(r.get("metric") == "carb_g" and r["kind"] == "nutrient" for r in rules):
        raise ValueError("Bộ quy tắc tiểu đường type 2 cần điều kiện carbohydrate.")
    if condition == "gout" and not any(r["kind"] == "food_group" for r in rules):
        raise ValueError("Bộ quy tắc gout cần điều kiện theo nhóm thực phẩm.")


def _valid_number(value):
    return type(value) in (int, float) and -1e12 <= value <= 1e12 and math.isfinite(value)


def _validate_group(rule):
    members, nonmembers = rule.get("members"), rule.get("nonmembers")
    if (not isinstance(rule.get("name"), str) or not rule["name"].strip()
            or not isinstance(rule.get("classification_source"), str) or not rule["classification_source"].strip()):
        raise ValueError("Nhóm thực phẩm cần tên và nguồn phân loại.")
    if not all(isinstance(ids, list) and all(type(fid) is int and 0 < fid <= 2**63 - 1 for fid in ids)
               for ids in (members, nonmembers)) or set(members) & set(nonmembers):
        raise ValueError("Cần danh sách mã FDC đã phân loại và mỗi mã chỉ thuộc một danh sách.")
    cap, weight = rule.get("max_dishes"), rule.get("weight")
    if weight is None:
        if type(cap) is not int or not 0 <= cap <= 2**53 - 1 or rule.get("scope") not in SCOPES:
            raise ValueError("Điều kiện số món cần mức tối đa nguyên không âm và kỳ áp dụng.")
    elif cap is not None or not _valid_number(weight) or weight <= 0:
        raise ValueError("Tiêu chí nhóm thực phẩm cần trọng số dương hoặc mức tối đa, mỗi dòng dùng một cách.")


def store_policy(conn, data, mode="operational"):
    _validate_policy(data, mode)
    encoded = json.dumps(data, sort_keys=True, ensure_ascii=False)
    key = (data["policy_id"], data["version"], mode)
    with transaction(conn):
        existing = conn.execute("SELECT data_json FROM health_policy WHERE policy_id=? AND version=? AND mode=?", key).fetchone()
        if existing:
            if existing["data_json"] != encoded:
                raise ValueError("Phiên bản đã có nội dung khác. Hãy tăng phiên bản.")
            return False
        conn.execute("INSERT INTO health_policy VALUES(?,?,?,?,?)", (*key[:2], data["condition"], mode, encoded))
    return True


def find_policies(conn, condition, mode, answers):
    """Lấy phiên bản mới nhất của mỗi bộ, rồi chọn đúng đối tượng áp dụng."""
    rows = conn.execute(
        "SELECT p.data_json FROM health_policy p WHERE p.condition_id=? AND p.mode=? AND p.version=("
        "SELECT MAX(v.version) FROM health_policy v WHERE v.policy_id=p.policy_id AND v.mode=p.mode) "
        "ORDER BY p.policy_id", (condition, mode))
    return [policy for row in rows if (policy := json.loads(row["data_json"]))["answers"] == answers
            and policy.get("intake_version") == 3 and not policy.get("guidance_fingerprint")
            and (mode == "demo" or policy_is_reviewed(policy))]


def policy_is_reviewed(policy):
    """Không coi nội dung dự thảo hoặc chỉ có tên người nhập là đã được duyệt."""
    try:
        reviewed_at = date.fromisoformat(policy.get("reviewed_at", ""))
    except (TypeError, ValueError):
        return False
    return (policy.get("review_status") == "reviewed" and reviewed_at <= date.today()
            and reviewed_at.isoformat() == policy.get("reviewed_at")
            and bool(str(policy.get("reviewed_by") or "").strip())
            and bool(str(policy.get("source") or "").strip()))


def classify_ingredients(fdc_ids, group):
    """Một món thuộc nhóm nếu có ít nhất một nguyên liệu thuộc nhóm."""
    ids = set(fdc_ids)
    if ids.intersection(group["members"]):
        return 1, set()
    unknown = ids.difference(group["nonmembers"])
    return (None, unknown) if unknown else (0, set())
