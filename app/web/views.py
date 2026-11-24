"""Các trang web dưới dạng hàm thuần (conn, settings, dữ liệu form) → Page | Redirect.

Tách khỏi FastAPI để kiểm thử được mà không cần mở trình duyệt hay máy chủ.
`form` là dict tên trường → danh sách giá trị (giống FormData.getlist).
"""
from __future__ import annotations

import uuid
import re
from dataclasses import dataclass, field

from app.ai.interpret import interpret_free_text
from app.catalog import manage
from app.catalog.health import CONDITIONS
from app.catalog.lookup import MEAL_ORDER, METRICS, Catalog
from app.planner import service, energy, disease
from app.planner.checklist import GOALS, dish_choice, goal_rules, ingredient_terms, parse_checklist, food_options
from app.planner.constraints import build_constraints
from app.planner.health import (REVIEW_LABELS, assess_health,
                                generation_blockers, parse_health)
from app.planner.profile import LIMIT_ROWS, parse_form, profile_to_form, validate_inventory
from app.planner.shopping import public_view
from app.planner.messages import swap_failure_reasons
from app.storage import repo
from app.storage.db import get_meta


@dataclass
class Page:
    template: str
    ctx: dict = field(default_factory=dict)
    status_code: int = 200


@dataclass
class Redirect:
    url: str


def _first(form, key, default=""):
    v = form.get(key)
    if isinstance(v, list):
        return v[0] if v else default
    return v if v is not None else default


def base_ctx(conn, settings) -> dict:
    cat = Catalog(conn, settings.dataset)
    return {"settings": settings.public_dict(), "demo": settings.data_mode == "demo", "usda": cat.release_info(),
            "ai_provider": settings.ai_provider}


# ---------------------------------------------------------------- trang chủ

def home(conn, settings) -> Page:
    cat = Catalog(conn, settings.dataset)
    counts = {r["dataset"]: r["n"] for r in conn.execute(
        "SELECT a.dataset, COUNT(*) AS n FROM recipe_active a GROUP BY a.dataset")}
    pv = repo.get_profile_version(conn)
    return Page("home.html", {**base_ctx(conn, settings), "recipe_counts": counts, "active_recipes": len(cat.list_active_recipes()),
                              "profile": pv, "can_generate": bool(pv and pv["status"] == "confirmed" and not generation_blockers(conn, pv, data_mode=settings.data_mode)),
                              "plans": repo.list_plans(conn)[:10], "token": uuid.uuid4().hex,
                              "rule_sets": cat.rule_sets(include_demo=settings.data_mode == "demo")})


# ---------------------------------------------------------------- hồ sơ / checklist

def _profile_ctx(conn, settings, form, errors=None, message=None, ai=None):
    cat = Catalog(conn, settings.dataset)
    return {**base_ctx(conn, settings), "form": form, "errors": errors or {}, "message": message, "ai": ai,
            "limit_rows": range(LIMIT_ROWS), "meal_order": MEAL_ORDER, "avoid_vocab": cat.avoid_tag_vocabulary(),
            "rule_sets": cat.rule_sets(include_demo=settings.data_mode == "demo")}


def _checklist_ctx(conn, settings, form, errors=None):
    cat = Catalog(conn, settings.dataset)
    foods, allergens = food_options(cat)
    ingredients = {term for info in cat.reviewed_ingredients() for term in ingredient_terms(info)}
    health, health_errors = parse_health({"health_simulation_version": "1", **form})
    scope_scenarios = disease.body_scope_scenarios(settings.data_mode)
    simulated_scope = not health_errors and disease.uses_body_scope_assumption(health, scope_scenarios)
    body, _ = energy.parse_body(form)
    needs_scope_answer = not simulated_scope and (
        "energy" in (form.get("goals") or []) or bool(health["conditions"]) or energy.has_energy_inputs(body)
    )
    return {**base_ctx(conn, settings), "form": form, "errors": errors or {}, "meal_order": MEAL_ORDER,
            "goals": GOALS, "ingredient_options": sorted(ingredients, key=str.casefold),
            "dish_options": [dish_choice(r) for r in cat.list_active_recipes()],
            "dish_choices": [{"id": r["recipe_id"], "name": r["name_vi"]} for r in cat.list_active_recipes()],
            "food_options": foods, "allergen_options": allergens, "health_definitions": CONDITIONS,
            "body_policy": energy.POLICY, "disease_policy": disease.POLICY,
            "body_scope_scenarios": scope_scenarios, "needs_scope_answer": needs_scope_answer}


def profile_form(conn, settings, version=None, advanced=False) -> Page:
    pv = repo.get_profile_version(conn, version)
    if version is not None and not pv:
        return _profile_missing(conn, settings)
    form = profile_to_form(pv["data"]) if pv else profile_to_form(
        {"days": 2, "meals": ["breakfast", "lunch", "dinner"], "dishes_per_meal": 1, "serving_levels": [1.0, 1.5],
         "prefer_variety": True})
    checklist = pv["data"].get("checklist", {}) if pv else {}
    health = checklist.get("health", {})
    foods, _ = food_options(Catalog(conn, settings.dataset))
    previous_avoid = checklist.get("ingredients", [])
    avoid_ids = [o["id"] for o in foods if any(set(o["fdc_ids"]) == set(c["fdc_ids"]) for c in previous_avoid)]
    form.update(base_version=str(pv["version"]) if pv else "", food_checklist_version="1",
                goals=checklist.get("goals", ["variety"] if form.get("prefer_variety") else []),
                health_conditions=health.get("conditions") or (["none"] if health.get("declared_none") else []),
                health_scenario=health.get('simulation_scenario',''),
                allergen_choices=checklist.get("allergen_choices", []),
                food_restrictions=checklist.get("food_restrictions", "yes" if avoid_ids or checklist.get("dishes") else ""),
                avoid_food_choices=avoid_ids,
                limit_food_choices=[o["id"] for o in checklist.get("limited_ingredients", [])],
                avoid_dish_choices=list(checklist.get("dishes", {})))
    if advanced:
        return Page("profile_advanced.html", _profile_ctx(conn, settings, form))
    return Page("profile.html", _checklist_ctx(conn, settings, form))


def profile_submit(conn, settings, form: dict) -> Page | Redirect:
    guided = _first(form, "action") == "checklist" or _first(form, "profile_mode") == "checklist"
    base_version = _first(form, "base_version")
    base = None
    if base_version:
        if not base_version.isdecimal() or len(base_version) > 18:
            return _profile_missing(conn, settings)
        base = repo.get_profile_version(conn, int(base_version))
        if not base:
            return _profile_missing(conn, settings)
    profile, errors = parse_form(form)
    cat = Catalog(conn, settings.dataset)
    errors.update(validate_inventory(profile, cat))
    if guided:
        if base:
            for key in ("avoid", "rule_set", "rule_set_confirmed", "nutrient_limits"):
                profile[key] = base["data"].get(key)
        else:
            profile.update(avoid=[], rule_set="", rule_set_confirmed=False, nutrient_limits=[])
        checklist, checklist_errors = parse_checklist(form, cat)
        errors.update(checklist_errors)
        health, health_errors = parse_health(form)
        errors.update(health_errors)
        if health:
            checklist["health"] = health
        profile["checklist"] = checklist
        profile["prefer_variety"] = "variety" in checklist["goals"]
    elif base and base["data"].get("checklist"):
        profile["checklist"] = base["data"]["checklist"]
        profile["prefer_variety"] = "variety" in profile["checklist"]["goals"]
    checklist = profile.get("checklist", {})
    if not guided and base and not any(k.startswith("body_") for k in form):
        body, body_errors = energy.validate_body(base["data"].get("body"), required=energy.enabled(profile))
    else:
        simulated_scope = guided and not health_errors and disease.uses_body_scope_assumption(
            checklist.get("health", {}), disease.body_scope_scenarios(settings.data_mode)
        )
        body, body_errors = energy.parse_body(
            form, required=energy.enabled(profile), simulated_scope=simulated_scope
        )
    errors.update(body_errors)
    profile["body"] = body
    if not body_errors:
        profile["body_result"] = energy.calculate(body)
    rules, rule_errors = goal_rules(checklist.get("goals", []), cat, settings, personal_energy=energy.enabled(profile))
    errors.update(rule_errors)
    review, health_cons, health_prefs = assess_health(conn, checklist.get("health"), profile=profile, data_mode=settings.data_mode)
    if checklist.get("allergen_selection_required"):
        review["state"] = "needs_information"
        review["reasons"].append({"state": "needs_information", "field": "allergen_choices",
                                  "message": "Danh sách dị ứng đã cập nhật. Hãy chọn lại các thực phẩm dị ứng hoặc xác nhận Không có."})
    profile["health_review"] = review
    if not errors:
        cons, prefs, errors = build_constraints(profile, cat, settings.data_mode, checklist_rules=rules,
                                               health_constraints=health_cons, health_preferences=health_prefs)
        if "health_conditions" in errors:
            review["state"] = "pending_review"
            review["reasons"].append({"state": "pending_review", "field": "health_conditions",
                                      "message": errors.pop("health_conditions")})
    if errors:
        if guided:
            if base:
                manual = profile_to_form(base["data"])
                form = {**form, **{k: manual.get(k, "") for k in ("rule_set", "avoid", "limit_metric_0")}}
            return Page("profile.html", _checklist_ctx(conn, settings, form, errors), 422)
        flat = {k: (v[0] if isinstance(v, list) and len(v) == 1 and k not in ("meals", "avoid_tag") else v) for k, v in form.items()}
        return Page("profile_advanced.html", _profile_ctx(conn, settings, flat, errors,
                                                 "Có trường chưa hợp lệ – xem thông báo tại từng ô."), 422)
    method = "guided_checklist" if guided else ("ai_draft" if _first(form, "from_ai") else "checklist")
    v = repo.save_profile_draft(conn, profile, cons, prefs, method,
                                status="draft" if review["state"] == "ready" else review["state"])
    return Redirect(f"/profile/confirm?v={v}")


def profile_interpret(conn, settings, form: dict) -> Page:
    text = _first(form, "free_text").strip()
    flat = {k: (v[0] if isinstance(v, list) and len(v) == 1 and k not in ("meals", "avoid_tag") else v) for k, v in form.items()}
    if not text:
        return Page("profile_advanced.html", _profile_ctx(conn, settings, flat, {"free_text": "Nhập một câu mô tả nhu cầu"}), 422)
    out = interpret_free_text(conn, settings, text, flat)
    out["form"]["base_version"] = _first(form, "base_version")
    return Page("profile_advanced.html", _profile_ctx(conn, settings, out["form"], out["errors"], out["message"], ai=out))


def _profile_missing(conn, settings):
    return Page("message.html", {**base_ctx(conn, settings), "title": "Không tìm thấy hồ sơ", "text": "",
                                 "back": "/profile"}, 404)


def profile_confirm_page(conn, settings, version: int, confirmed_msg: str | None = None) -> Page:
    pv = repo.get_profile_version(conn, version)
    if not pv:
        return _profile_missing(conn, settings)
    hard = [c for c in pv["constraints"] if c.get("hard")]
    review = pv["data"].get("health_review", {"state": "ready", "reasons": []})
    blockers = generation_blockers(conn, pv, data_mode=settings.data_mode)
    if review["state"] == "ready" and blockers:
        state = "needs_information" if energy.integrity_errors(pv["data"], pv["constraints"]) else "pending_review"
        review = {"state": state, "reasons": [
            {"message": message, "field": "profile", "state": state} for message in blockers]}
    return Page("profile_confirm.html", {**base_ctx(conn, settings), "pv": pv, "hard": hard, "prefs": pv["preferences"],
                                         "goal_labels": GOALS, "health_definitions": CONDITIONS,
                                         "token": uuid.uuid4().hex, "message": confirmed_msg,
                                         "health_review": review,
                                         "review_labels": REVIEW_LABELS})


def profile_confirm_submit(conn, settings, version: int, request_token: str = "") -> Redirect | Page:
    pv = repo.get_profile_version(conn, version)
    if not pv:
        return _profile_missing(conn, settings)
    blockers = generation_blockers(conn, pv, data_mode=settings.data_mode)
    if blockers or pv["status"] not in ("draft", "confirmed"):
        # Lưu một phiên bản mới để người dùng xem lại toàn bộ điều kiện sau khi bổ sung/duyệt.
        page = profile_form(conn, settings, version)
        form = {**page.ctx["form"], "profile_mode": "checklist"}
        return profile_submit(conn, settings, form)
    if settings.data_mode != "demo" and service.has_demo_rules(pv["constraints"] + pv["preferences"]):
        return Page("message.html", {**base_ctx(conn, settings), "title": "Cần cập nhật hồ sơ",
                                    "text": "Hồ sơ đang dùng quy tắc thử nghiệm. Hãy cập nhật mục tiêu trong checklist.",
                                    "back": f"/profile?v={version}"}, 422)
    if pv["status"] != "confirmed" and not repo.confirm_profile(conn, version):
        return _profile_missing(conn, settings)
    if request_token:
        return plan_generate(conn, settings, {"request_token": request_token, "profile_version": str(version)})
    return Redirect(f"/profile/confirm?v={version}&ok=1")


# ---------------------------------------------------------------- lập thực đơn

def plan_generate(conn, settings, form: dict) -> Redirect | Page:
    version = _first(form, "profile_version")
    if version and (not version.isdecimal() or len(version) > 18):
        return _profile_missing(conn, settings)
    out = service.generate_plan(conn, settings, _first(form, "request_token") or None,
                                profile_version=int(version) if version else None)
    if out.get("plan_id"):
        return Redirect(f"/plans/{out['plan_id']}")
    if out.get("run_id"):
        return Redirect(f"/runs/{out['run_id']}")
    return Page("run.html", {**base_ctx(conn, settings), "run": {"status": out["status"], "detail": out["detail"], "run_id": None}}, 409)


def run_page(conn, settings, run_id: int) -> Page:
    run = repo.get_run(conn, run_id)
    if not run:
        return Page("message.html", {**base_ctx(conn, settings), "title": "Không tìm thấy lần chạy", "text": ""}, 404)
    if run["plan_id"]:
        run["detail"]["plan_id"] = run["plan_id"]
    return Page("run.html", {**base_ctx(conn, settings), "run": run, "token": uuid.uuid4().hex})


def plans_page(conn, settings) -> Page:
    runs = [dict(r) for r in conn.execute("SELECT run_id, status, plan_id, profile_version, created_at FROM plan_run ORDER BY run_id DESC LIMIT 30")]
    return Page("plans.html", {**base_ctx(conn, settings), "plans": repo.list_plans(conn), "runs": runs})


def plan_page(conn, settings, plan_id: int, day: int | None = None, version: int | None = None,
              change_token: str | None = None) -> Page:
    plan = repo.get_plan(conn, plan_id, version)
    if not plan:
        return Page("message.html", {**base_ctx(conn, settings), "title": "Không tìm thấy thực đơn", "text": ""}, 404)
    days = sorted({i["day"] for i in plan["items"]})
    day = day if day in days else days[0]
    rep = plan["report"]
    dishes = [d for d in rep["dishes"] if d["day"] == day]
    meals = [m for m in MEAL_ORDER if any(d["meal"] == m for d in dishes)]
    meal_totals = {m: rep["totals"]["meals"].get(f"{day}|{m}") for m in meals}
    change = repo.find_change_by_token(conn, change_token) if change_token else None
    if change and change['detail'].get('violations'):
        detail = change['detail']
        raw = {f"{r['scope_label']}: {r['detail']}" for r in detail['violations']}
        formatted = swap_failure_reasons(detail['violations'])
        extras = detail.get('structural', [s for s in detail.get('reasons', []) if s not in raw and s not in formatted])
        detail['reasons'] = swap_failure_reasons(detail['violations'], extras)
    results_day = [r for r in rep["results"] if r.get("scope") in ("period",) or r.get("scope_key") in (day, [day])
                   or (r.get("scope") == "meal" and isinstance(r.get("scope_key"), list) and r["scope_key"][0] == day)
                   or r["kind"] in ("exclude", "prep_time", "budget")]
    fb = repo.latest_feedback(conn)
    return Page("plan.html", {**base_ctx(conn, settings), "plan": plan, "day": day, "days": days, "dishes": dishes, "meals": meals,
                              "meal_totals": meal_totals, "day_total": rep["totals"]["days"].get(str(day)),
                              "results": rep["results"], "results_day": results_day,
                              "levels": plan["context"]["profile"]["serving_levels"], "token": uuid.uuid4().hex,
                              "change": change, "explain": service.explain(plan), "cost": rep["cost"],
                              "is_current": plan["version"] == plan["current_version"], "feedback": fb,
                              "budget": plan["context"]["profile"].get("budget")})


def plan_trace(conn, settings, plan_id: int, day: int, metric: str, version: int | None = None) -> Page:
    plan = repo.get_plan(conn, plan_id, version)
    if not plan or metric not in METRICS:
        return Page("message.html", {**base_ctx(conn, settings), "title": "Không có dữ liệu truy xuất", "text": ""}, 404)
    rows = plan["report"]["traces"].get(str(day), {}).get(metric, [])
    total = plan["report"]["totals"]["days"].get(str(day), {}).get(metric)
    results = [r for r in plan["report"]["results"] if r.get("metric") == metric and r.get("scope_key") in (day, [day])]
    return Page("trace.html", {**base_ctx(conn, settings), "plan": plan, "day": day, "metric": metric, "rows": rows,
                               "total": total, "results": results})


def plan_shopping(conn, settings, plan_id: int, version: int | None = None) -> Page:
    plan = repo.get_plan(conn, plan_id, version)
    if not plan:
        return Page("message.html", {**base_ctx(conn, settings), "title": "Không tìm thấy thực đơn", "text": ""}, 404)
    return Page("shopping.html", {**base_ctx(conn, settings), "plan": plan, "shop": public_view(plan["shopping"]),
                                  "budget": plan["context"]["profile"].get("budget")})


def plan_swap_options(conn, settings, plan_id, base_version, day, meal, pos):
    return service.swap_choices(conn, settings, plan_id, base_version, day, meal, pos)


def plan_swap(conn, settings, plan_id: int, form: dict) -> Redirect:
    token = _first(form, "request_token") or uuid.uuid4().hex
    servings_s = _first(form, "servings").replace(",", ".")
    out = service.swap_dish(conn, settings, plan_id, int(_first(form, "base_version", "0")), int(_first(form, "day", "1")),
                            _first(form, "meal"), int(_first(form, "pos", "0")), _first(form, "recipe_id") or None,
                            float(servings_s) if servings_s else None, token)
    if not repo.find_change_by_token(conn, token):  # ví dụ: món không tồn tại trước khi ghi
        repo.record_change(conn, plan_id, int(_first(form, "base_version", "0")), token, out["status"], None, out.get("detail", {}))
    return Redirect(f"/plans/{plan_id}?day={_first(form, 'day', '1')}&change={token}")


def feedback_submit(conn, settings, form: dict) -> Redirect | Page:
    liked_s = _first(form, "liked")
    liked = {"like": 1, "dislike": -1}.get(liked_s)
    cm = _first(form, "cook_minutes").replace(",", ".").strip()
    try:
        cook = float(cm) if cm else None
    except ValueError:
        cook = -1.0
    out = service.record_feedback(conn, _first(form, "recipe_id"), liked, cook, _first(form, "note"))
    back = _first(form, "back")
    if not isinstance(back, str) or not re.fullmatch(r"/plans/[1-9][0-9]*(?:\?day=[1-9][0-9]*)?", back):
        back = "/"
    if not out["ok"]:
        return Page("message.html", {**base_ctx(conn, settings), "title": "Phản hồi chưa hợp lệ",
                                     "text": "; ".join(out["errors"].values()), "back": back}, 422)
    sep = "&" if "?" in back else "?"
    return Redirect(f"{back}{sep}fb=1")


# ---------------------------------------------------------------- quản trị dữ liệu (tách khỏi luồng người dùng)

def admin_home(conn, settings, message=None, report=None) -> Page:
    recipes = [dict(r) for r in conn.execute(
        "SELECT r.recipe_id, r.version, r.dataset, r.name_vi, r.status, r.batch_id, r.imported_at, "
        "(SELECT version FROM recipe_active a WHERE a.recipe_id=r.recipe_id AND a.dataset=r.dataset) AS active_version "
        "FROM recipe r ORDER BY r.dataset, r.recipe_id, r.version")]
    logs = [dict(r) for r in conn.execute("SELECT id, kind, dataset, created_at, status FROM import_log ORDER BY id DESC LIMIT 20")]
    return Page("admin/index.html", {**base_ctx(conn, settings), "recipes": recipes, "logs": logs, "message": message,
                                     "report": report, "active_release": get_meta(conn, "active_usda_release")})


def admin_log(conn, settings, log_id: int) -> Page:
    import json
    r = conn.execute("SELECT * FROM import_log WHERE id=?", (log_id,)).fetchone()
    rep = json.loads(r["report_json"]) if r else None
    return admin_home(conn, settings, message=f"Báo cáo nhập #{log_id}", report=rep)


def admin_upload_recipes(conn, settings, recipes_bytes: bytes, lines_bytes: bytes, dataset: str) -> Page:
    rep = manage.import_recipes(conn, recipes_bytes.decode("utf-8-sig"), lines_bytes.decode("utf-8-sig"), dataset)
    return admin_home(conn, settings, f"Nhập công thức: {rep.status}", rep.to_dict())


def admin_upload_prices(conn, settings, data: bytes, dataset: str) -> Page:
    rep = manage.import_prices(conn, data.decode("utf-8-sig"), dataset)
    return admin_home(conn, settings, f"Nhập giá/quy cách: {rep.status}", rep.to_dict())


def admin_upload_ingredients(conn, settings, data: bytes, dataset: str) -> Page:
    rep = manage.import_ingredient_info(conn, data.decode("utf-8-sig"), dataset)
    return admin_home(conn, settings, f"Nhập danh mục nguyên liệu: {rep.status}", rep.to_dict())


def admin_approve(conn, settings, form: dict) -> Page:
    action = _first(form, "action", "approve")
    rid, ver = _first(form, "recipe_id"), int(_first(form, "version", "0"))
    ok, msg = (manage.approve_recipe if action == "approve" else manage.reject_recipe)(conn, rid, ver)
    return admin_home(conn, settings, msg)


def admin_load_demo(conn, settings) -> Page:
    out = manage.load_demo(conn)
    return admin_home(conn, settings, "Đã nạp dữ liệu DEMO (nhãn demo)", out["recipes"])
