"""Xóa dữ liệu câu hỏi đã bỏ theo yêu cầu người dùng, không lưu bản sao nội dung cũ."""
from __future__ import annotations

import json

MARKER = "food_checklist_v1_cleanup"
MESSAGE = "Checklist thực phẩm đã thay đổi. Hãy chọn lại danh sách trước khi lập thực đơn."
OLD_KEYS = {"insulin", "carb_plan", "kidney_disease", "diet_guidance", "other_diet", "gout_restrictions",
            "guidance", "guidance_match", "guidance_fingerprint", "restrictions_provided",
            "diet_guidance_details", "diet_guidance_source", "diet_guidance_date",
            "allergy", "allergy_names", "gout_ingredient_names", "gout_ingredients", "gout_dishes"}


def _legacy(obj):
    if isinstance(obj, dict):
        return bool(set(obj) & OLD_KEYS) or any(_legacy(v) for v in obj.values())
    return isinstance(obj, list) and any(_legacy(v) for v in obj)


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if k not in OLD_KEYS and k != "answers"}
    if isinstance(obj, list):
        return [_clean(v) for v in obj]
    return obj


def _old_constraint(c):
    cid = str(c.get("cid", ""))
    return bool(c.get("condition")) or cid == "allergies" or cid.startswith(("health:", "gout_", "exclude_gout"))


def _profile(data):
    data = _clean(data)
    checklist = data.get("checklist", {})
    checklist.pop("allergies", None)
    checklist.pop("allergen_choices", None)
    checklist.pop("allergen_not_listed", None)
    checklist.pop("food_checklist_version", None)
    health = checklist.get("health", {})
    checklist["health"] = {"intake_version": 0, "conditions": health.get("conditions", []),
                           "declared_none": health.get("declared_none", False), "migration_required": True}
    data["checklist"] = checklist
    data["health_review"] = {"state": "needs_information", "reasons": [
        {"state": "needs_information", "field": "food_checklist", "message": MESSAGE}]}
    return data


def purge_legacy_intake(conn, *, force=False):
    if not force and conn.execute("SELECT 1 FROM meta WHERE key=?", (MARKER,)).fetchone():
        return {"profiles": 0, "plan_versions": 0, "policies": 0}
    counts = {"profiles": 0, "plan_versions": 0, "policies": 0}
    affected_profiles, affected_plans = set(), set()
    dump = lambda value: json.dumps(value, ensure_ascii=False)
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in conn.execute("SELECT * FROM profile_version").fetchall():
            data = json.loads(row["data_json"])
            checklist = data.get("checklist")
            if not _legacy(data) and (checklist or {}).get("health", {}).get("migration_required"):
                continue
            if not _legacy(data) and not (checklist and checklist.get("food_checklist_version") != 1):
                continue
            cj = json.loads(row["constraints_json"] or "{}")
            for key in ("constraints", "preferences"):
                cj[key] = [_clean(c) for c in cj.get(key, []) if not _old_constraint(c)]
            conn.execute("UPDATE profile_version SET data_json=?,constraints_json=?,status='needs_information',confirmed_at=NULL WHERE profile_id=? AND version=?",
                         (dump(_profile(data)), dump(cj), row["profile_id"], row["version"]))
            affected_profiles.add(row["version"])
            counts["profiles"] += 1
        for row in conn.execute("SELECT v.*,p.profile_version FROM plan_version v JOIN plan p ON p.plan_id=v.plan_id").fetchall():
            context = json.loads(row["context_json"])
            if row["profile_version"] not in affected_profiles and not _legacy(context):
                continue
            context = _clean(context)
            if context.get("profile"):
                context["profile"] = _profile(context["profile"])
            for key in ("constraints", "preferences"):
                context[key] = [c for c in context.get(key, []) if not _old_constraint(c)]
            context["rule_sources"] = sorted({c["source"] for c in context.get("constraints", []) + context.get("preferences", []) if c.get("source")})
            context["notes"] = [MESSAGE]
            context["migration_required"] = True
            report = json.loads(row["report_json"])
            report["results"] = [_clean(r) for r in report.get("results", []) if not _old_constraint(r)]
            report["status"] = "stale_plan"
            conn.execute("UPDATE plan_version SET context_json=?,report_json=?,status='stale_plan' WHERE plan_id=? AND version=?",
                         (dump(context),dump(report),row["plan_id"],row["version"]))
            affected_plans.add(row["plan_id"])
            counts["plan_versions"] += 1
        for row in conn.execute("SELECT run_id,profile_version,plan_id,detail_json FROM plan_run").fetchall():
            if row["profile_version"] in affected_profiles or row["plan_id"] in affected_plans:
                old = json.loads(row["detail_json"] or "{}")
                detail = {"reasons": [MESSAGE], "profile_version": row["profile_version"],
                          **{k: old[k] for k in ("config", "solver") if k in old}}
                conn.execute("UPDATE plan_run SET status=?,detail_json=? WHERE run_id=?",
                             ("stale_plan" if row["plan_id"] else "missing_data",dump(detail),row["run_id"]))
        for row in conn.execute("SELECT change_id,plan_id,detail_json FROM plan_change").fetchall():
            if row["plan_id"] in affected_plans:
                old = json.loads(row["detail_json"] or "{}")
                detail = {"reasons": [MESSAGE], **({"change": old["change"]} if "change" in old else {})}
                conn.execute("UPDATE plan_change SET status='stale_plan',detail_json=? WHERE change_id=?", (dump(detail),row["change_id"]))
        for row in conn.execute("SELECT policy_id,version,mode,data_json FROM health_policy").fetchall():
            data = json.loads(row["data_json"])
            if _legacy(data) or data.get("intake_version") != 3:
                conn.execute("DELETE FROM health_policy WHERE policy_id=? AND version=? AND mode=?",(row["policy_id"],row["version"],row["mode"]))
                counts["policies"] += 1
        for row in conn.execute("SELECT id,detail_json FROM ai_log").fetchall():
            data = json.loads(row["detail_json"] or "{}")
            if _legacy(data):
                conn.execute("UPDATE ai_log SET detail_json=? WHERE id=?", (dump({"message": MESSAGE}),row["id"]))
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(MARKER,dump(counts)))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def remove_unlisted_allergen(conn):
    """Bỏ lựa chọn đã ngừng hỗ trợ; hồ sơ từng chọn nó phải xác nhận lại dị ứng."""
    marker = 'remove_unlisted_allergen_v1'
    if conn.execute('SELECT 1 FROM meta WHERE key=?', (marker,)).fetchone():
        return
    dump = lambda obj: json.dumps(obj, ensure_ascii=False)
    message = 'Danh sách dị ứng đã cập nhật. Hãy chọn lại thực phẩm dị ứng hoặc xác nhận Không có.'
    def clean(data):
        checklist = data.get('checklist') or {}
        if not checklist.get('allergen_not_listed') and 'not_listed' not in checklist.get('allergen_choices', []):
            return False
        checklist.pop('allergen_not_listed', None)
        checklist['allergen_choices'] = [v for v in checklist.get('allergen_choices', []) if v != 'not_listed']
        checklist['allergen_selection_required'] = True
        data['health_review'] = {'state':'needs_information', 'reasons':[
            {'state':'needs_information', 'field':'allergen_choices', 'message':message}]}
        return True
    conn.execute('BEGIN IMMEDIATE')
    try:
        affected = set()
        for row in conn.execute('SELECT profile_id,version,data_json FROM profile_version').fetchall():
            data = json.loads(row['data_json'])
            if clean(data):
                affected.add(row['version'])
                conn.execute("UPDATE profile_version SET data_json=?,status='needs_information',confirmed_at=NULL WHERE profile_id=? AND version=?",
                             (dump(data),row['profile_id'],row['version']))
        for row in conn.execute('SELECT v.*,p.profile_version FROM plan_version v JOIN plan p ON v.plan_id=p.plan_id').fetchall():
            context = json.loads(row['context_json'])
            changed = clean(context.get('profile') or {})
            if changed or row['profile_version'] in affected:
                context['migration_required'] = True
                context['notes'] = [message]
                report = json.loads(row['report_json'])
                report['status'] = 'stale_plan'
                conn.execute("UPDATE plan_version SET context_json=?,report_json=?,status='stale_plan' WHERE plan_id=? AND version=?",
                             (dump(context),dump(report),row['plan_id'],row['version']))
        conn.execute('INSERT INTO meta(key,value) VALUES(?,?)', (marker,'done'))
        conn.execute('COMMIT')
    except BaseException:
        conn.execute('ROLLBACK')
        raise
