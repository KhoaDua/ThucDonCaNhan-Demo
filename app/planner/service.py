"""Điều phối nghiệp vụ: lập thực đơn, đổi món, kiểm tra phương án, phản hồi (FR-06…FR-13).

Không phụ thuộc giao diện web: mọi hàm nhận kết nối SQLite + Settings và trả dict.
Trạng thái kết quả: validated | missing_data | constraint_violation | infeasible | search_timeout | stale_plan.
"""
from __future__ import annotations

from app.catalog.lookup import MEAL_LABELS, METRICS, Catalog
from app.config import Settings
from app.planner import solver as solver_mod
from app.planner.checker import verify_plan
from app.planner.constraints import adjustable_parameters, fmt
from app.planner.health import generation_blockers
from app.planner.messages import swap_failure_reasons
from app.planner.filtering import MISSING_REASONS, REASON_TEXT, build_options, screen, strip_cost_scores
from app.storage import repo

STATUS_TEXT = {
    "validated": "Đã kiểm tra – đạt mọi điều kiện bắt buộc",
    "missing_data": "Thiếu dữ liệu để đánh giá",
    "constraint_violation": "Phương án vi phạm điều kiện bắt buộc",
    "infeasible": "Không có phương án thỏa điều kiện trong tập ứng viên đã xét",
    "search_timeout": "Hết thời gian tìm kiếm khi chưa có phương án đạt kiểm tra",
    "stale_plan": "Bạn đang sửa một phiên bản cũ – hãy tải lại bản mới nhất",
}


def _slots(profile):
    return [{"day": d, "meal": m, "pos": p} for d in range(1, profile["days"] + 1) for m in profile["meals"]
            for p in range(profile["dishes_per_meal"])]


def _linear(constraints):
    out = []
    for c in constraints:
        if c["kind"] == "nutrient":
            out.append(solver_mod.LinearCon(c["cid"], c["metric"], c["op"], c["value"], c["scope"], c.get("meal_type")))
        elif c['kind'] == 'nutrient_ratio':
            out.append(solver_mod.LinearCon(c['cid'],c['cid'],c['op'],c['value'],c['scope'],c.get('meal_type')))
        elif c["kind"] == "food_group":
            out.append(solver_mod.LinearCon(c["cid"], c["cid"], "<=", c["value"], c["scope"]))
        elif c["kind"] == "budget" and c["mode"] == "usage":
            out.append(solver_mod.LinearCon(c["cid"], "cost_usage", "<=", c["value"], c["scope"]))
    return out


def _items(slots, assignment):
    return [{"day": s["day"], "meal": s["meal"], "pos": s["pos"], "recipe_id": o["recipe_id"], "version": o["version"],
             "servings": o["servings"]} for s, o in zip(slots, assignment)]


def _attempt(conn, settings: Settings, profile, constraints, prefs, *, timeout_s=None, max_nodes=None, solver=None):
    """Một lần: sàng lọc → tạo phương án → giải → kiểm tra độc lập. Không ghi CSDL."""
    catalog = Catalog(conn, settings.dataset)
    out = {"catalog": catalog}
    if not catalog.release:
        out.update(status="missing_data", reasons=["Chưa nhập dữ liệu dinh dưỡng USDA (chạy lệnh import-usda)"])
        return out
    sres = screen(catalog, profile, constraints)
    out["screen"] = sres
    if not sres.eligible and not sres.excluded:
        out.update(status="missing_data", reasons=[f"Chưa có công thức đã duyệt cho bộ dữ liệu '{settings.dataset}' và các bữa đã chọn"])
        return out
    fb = repo.latest_feedback(conn)
    cinfo = build_options(catalog, profile, prefs, sres, fb)
    notes = []
    cost_pref = next((p for p in prefs if p["kind"] == "cost"), None)
    if cost_pref and cinfo["cost_missing"]:
        if not cost_pref.get("drop_if_missing"):
            out.update(status="missing_data", needs_confirmation="drop_cost",
                       reasons=[f"Ngân sách là ưu tiên mềm nhưng thiếu giá cho {len(cinfo['cost_missing'])} món "
                                f"({', '.join(cinfo['cost_missing'][:8])}). Hãy bổ sung giá hoặc xác nhận bỏ tiêu chí chi phí "
                                f"khỏi xếp hạng trong checklist."])
            return out
        strip_cost_scores(sres)
        notes.append("Đã bỏ tiêu chí chi phí khỏi xếp hạng theo xác nhận của người dùng (thiếu giá).")

    slots = _slots(profile)
    options = []
    reasons = []
    for s in slots:
        opts = [o for c in sres.eligible for o in c["options"] if s["meal"] in o["meal_types"]]
        options.append(opts)
    missing_ex = sres.missing_data_exclusions()
    for meal in profile["meals"]:
        n_ok = len({o["recipe_id"] for c in sres.eligible for o in c["options"] if meal in o["meal_types"]})
        if n_ok < profile["dishes_per_meal"]:
            miss_meal = [e for e in missing_ex if meal in (catalog.get_recipe(e["recipe_id"], e["version"]) or {}).get("meal_types", [])]
            reasons.append(f"{MEAL_LABELS[meal]}: chỉ còn {n_ok} món đủ điều kiện, cần {profile['dishes_per_meal']}"
                           + (f"; {len(miss_meal)} món bị loại vì thiếu dữ liệu" if miss_meal else ""))
            if miss_meal:
                out["missing_slot"] = True
    if reasons:
        out.update(status="missing_data" if out.get("missing_slot") else "infeasible", reasons=reasons, notes=notes)
        return out

    reports = {}

    def accept(assignment):
        items = _items(slots, assignment)
        rep = verify_plan(conn, items, profile, constraints, settings.dataset, catalog.release)
        key = tuple((i["recipe_id"], i["servings"]) for i in items)
        reports[key] = rep
        return rep["status"] == "validated"

    variety = next((p["weight"] for p in prefs if p["kind"] == "variety"), 0.0)
    timeout_s = settings.search_timeout_s if timeout_s is None else timeout_s
    max_nodes = settings.search_max_nodes if max_nodes is None else max_nodes
    which = solver or settings.solver
    lin = _linear(constraints)
    res = None
    purchase = None
    b = next((c for c in constraints if c["kind"] == "budget" and c["mode"] == "purchase"), None)
    if b:
        prices = {}
        for c in sres.eligible:
            for f in c["options"][0]["grams"]:
                p = catalog.get_price(f)
                prices[f] = (float(p["package_g"]), float(p["price"]))
        purchase = {"bound": b["value"], "prices": prices,
                    "inventory": {i["fdc_id"]: i["grams"] for i in profile.get("inventory") or []}}
    if which == "milp" and solver_mod.milp_available():
        res = solver_mod.solve_milp(slots, options, lin, variety_weight=variety, purchase=purchase, accept=accept,
                                    timeout_s=timeout_s)
        if res.status == "rejected":
            notes.append("Nghiệm MILP bị bộ kiểm tra từ chối – chuyển sang bộ giải builtin.")
            res = None
    elif which == "milp":
        notes.append("SciPy chưa cài hoặc không nạp được (ví dụ bị Windows chặn) – dùng bộ giải builtin.")
    if res is None:
        res = solver_mod.solve_builtin(slots, options, lin, variety_weight=variety, accept=accept,
                                       timeout_s=timeout_s, max_nodes=max_nodes, purchase=purchase)
    out["solve"] = res
    out["notes"] = notes + ([res.note] if res.note else [])
    if res.status == "found":
        items = _items(slots, res.assignment)
        key = tuple((i["recipe_id"], i["servings"]) for i in items)
        rep = reports.get(key) or verify_plan(conn, items, profile, constraints, settings.dataset, catalog.release)
        out.update(items=items, report=rep, status=rep["status"])
        if rep["status"] != "validated":  # không xảy ra nếu accept hoạt động đúng; vẫn chặn
            out["reasons"] = ["Bộ kiểm tra độc lập không công nhận phương án"]
    elif res.status == "timeout":
        out.update(status="search_timeout", reasons=[res.note])
    else:
        r = [res.note]
        if missing_ex:
            r.append(f"Giới hạn của kết luận: {len(missing_ex)} món bị loại vì thiếu dữ liệu, có thể làm tập ứng viên hẹp hơn thực tế.")
        out.update(status="infeasible", reasons=r)
    return out


def _screen_summary(sres):
    if not sres:
        return {}
    return {"eligible": [c["recipe"]["recipe_id"] for c in sres.eligible],
            "excluded": [{"recipe_id": e["recipe_id"], "name": e["name"],
                          "reasons": [{"code": r["code"], "text": REASON_TEXT.get(r["code"], r["code"]), "detail": r["detail"]}
                                      for r in e["reasons"]],
                          "missing_data": any(r["code"] in MISSING_REASONS for r in e["reasons"])} for e in sres.excluded]}


def _suggestions(conn, settings, profile, constraints, prefs, status):
    """FR-09: chỉ gợi ý thay đổi điều kiện/tiêu chí người dùng được quyền sửa."""
    sug, locked = [], []
    if status == "infeasible":
        for c in constraints:
            if not c.get("editable"):
                locked.append(c["label"])
                continue
            trial = [x for x in constraints if x is not c]
            r = _attempt(conn, settings, profile, trial, prefs, timeout_s=min(3.0, settings.search_timeout_s), max_nodes=200_000)
            if r.get("status") == "validated":
                sug.append(f"Nới hoặc bỏ điều kiện “{c['label']}” sẽ có phương án đạt kiểm tra.")
        sug += ["Có thể thay đổi: " + p for p in adjustable_parameters(profile)]
    elif status == "search_timeout":
        sug += ["Thử lại với thời gian tìm kiếm dài hơn (PLANNER_TIMEOUT_S) hoặc bộ giải milp.",
                "Giảm số ngày hoặc bớt một mức khẩu phần để thu hẹp không gian tìm kiếm.",
                "Bỏ bớt một ưu tiên mềm (ví dụ ít lặp món) không làm thay đổi điều kiện bắt buộc."]
        locked = [c["label"] for c in constraints if not c.get("editable")]
    elif status == "missing_data":
        sug.append("Bổ sung dữ liệu còn thiếu trong trang Quản trị dữ liệu, hoặc xác nhận cách xử lý được yêu cầu.")
        locked = [c["label"] for c in constraints if not c.get("editable")]
    return sug, locked


def has_demo_rules(rules):
    return any(c.get("demo_rule") or (c.get("cid", "").startswith("rule:") and "[GIẢ LẬP]" in (c.get("source") or ""))
               for c in rules)


def generate_plan(conn, settings: Settings, request_token: str | None = None, *, timeout_s=None, max_nodes=None,
                  solver=None, profile_version=None) -> dict:
    prev = repo.find_run_by_token(conn, request_token)
    if prev:
        prev["duplicate"] = True
        return prev
    pv = repo.get_profile_version(conn, profile_version)
    if not pv or pv["status"] != "confirmed":
        return {"status": "missing_data", "detail": {"reasons": ["Chưa có hồ sơ đã xác nhận. Hãy điền và xác nhận checklist."]}}
    blockers = generation_blockers(conn, pv, data_mode=settings.data_mode)
    if blockers:
        return {"status": "missing_data", "detail": {"reasons": blockers}}
    profile, constraints, prefs = pv["data"], pv["constraints"], pv["preferences"]
    if settings.data_mode != "demo" and has_demo_rules(constraints + prefs):
        return {"status": "missing_data", "detail": {"reasons": ["Hồ sơ còn dùng quy tắc thử nghiệm. "
                                                                "Hãy cập nhật mục tiêu trong checklist."]}}
    r = _attempt(conn, settings, profile, constraints, prefs, timeout_s=timeout_s, max_nodes=max_nodes, solver=solver)
    solve = r.get("solve")
    detail = {"status_text": STATUS_TEXT.get(r["status"]), "reasons": r.get("reasons", []), "notes": r.get("notes", []),
              "screen": _screen_summary(r.get("screen")), "needs_confirmation": r.get("needs_confirmation"),
              "solver": None if not solve else {"name": solve.solver, "status": solve.status, "nodes": solve.nodes,
                                               "elapsed_s": round(solve.elapsed_s, 3), "proven_optimal": solve.proven_optimal,
                                               "objective": solve.objective, "rejected_by_checker": solve.rejected_by_checker,
                                               "binding": solve.extra.get("binding")},
              "profile_version": pv["version"], "config": settings.public_dict()}
    if solve and "energy_bound" in solve.extra:
        detail["energy_bound"] = solve.extra["energy_bound"]
    if r["status"] == "validated":
        report = r["report"]
        context = _context(r["catalog"], pv, constraints, prefs, report, settings, detail)
        plan_id, run_id = repo.create_plan(conn, pv["version"], settings.dataset, r["items"], report, context, request_token, detail)
        return {"status": "validated", "plan_id": plan_id, "run_id": run_id, "detail": detail}
    if r["status"] == "constraint_violation" and r.get("report"):
        detail["violations"] = [x for x in r["report"]["results"] if x["status"] == "fail"]
    detail["suggestions"], detail["locked"] = _suggestions(conn, settings, profile, constraints, prefs, r["status"])
    run_id = repo.save_run(conn, pv["version"], request_token, r["status"], detail)
    return {"status": r["status"], "run_id": run_id, "detail": detail}


def _context(catalog, pv, constraints, prefs, report, settings, detail):
    return {"profile_version": pv["version"], "profile": pv["data"], "constraints": constraints, "preferences": prefs,
            "dataset": settings.dataset, "data_mode": settings.data_mode, "usda_release": report["release_id"],
            "usda_label": (catalog.release_info() or {}).get("label"),
            "recipe_versions": report["sources"]["recipes"], "price_versions": report["sources"]["price_versions"],
            "rule_sources": sorted({c["source"] for c in constraints + prefs if c.get("source")}),
            "config": settings.public_dict(), "solver": detail.get("solver"), "screen": detail.get("screen"),
            "notes": detail.get("notes")}


def verify_submitted_plan(conn, settings: Settings, items: list[dict], profile_version: int | None = None) -> dict:
    """FR-08: kiểm tra một thực đơn được gửi vào (kể cả khi kèm tổng 'đã đạt' tự khai)."""
    pv = repo.get_profile_version(conn, profile_version, confirmed_only=True)
    if not pv:
        return {"status": "missing_data", "detail": {"reasons": ["Chưa có hồ sơ đã xác nhận."]}}
    blockers = generation_blockers(conn, pv, data_mode=settings.data_mode)
    if blockers:
        return {"status": "missing_data", "detail": {"reasons": blockers}}
    clean = [{k: it[k] for k in ("day", "meal", "pos", "recipe_id", "version", "servings") if k in it} for it in items]
    return verify_plan(conn, clean, pv["data"], pv["constraints"], settings.dataset)


def swap_dish(conn, settings: Settings, plan_id: int, base_version: int, day: int, meal: str, pos: int,
              new_recipe_id: str | None, new_servings: float | None, request_token: str | None) -> dict:
    """FR-11: đổi món hoặc khẩu phần. Đạt → lưu phiên bản mới (thực đơn, báo cáo, mua sắm) trong một giao dịch."""
    prev = repo.find_change_by_token(conn, request_token)
    if prev:
        return {"status": prev["status"], "duplicate": True, "version": prev["result_version"], "detail": prev["detail"]}
    plan = repo.get_plan(conn, plan_id)
    if not plan:
        return {"status": "missing_data", "detail": {"reasons": ["Không tìm thấy thực đơn"]}}
    if int(base_version) != plan["current_version"]:
        d = {"reasons": [f"Bạn đang sửa phiên bản {base_version}, bản mới nhất là {plan['current_version']}."]}
        repo.record_change(conn, plan_id, base_version, request_token, "stale_plan", None, d)
        return {"status": "stale_plan", "detail": d, "current_version": plan["current_version"]}
    pv = repo.get_profile_version(conn, plan["profile_version"])
    blockers = generation_blockers(conn, pv, data_mode=settings.data_mode)
    if blockers:
        detail = {"reasons": blockers}
        repo.record_change(conn, plan_id, base_version, request_token, "missing_data", None, detail)
        return {"status": "missing_data", "detail": detail}
    profile, constraints = pv["data"], pv["constraints"]
    catalog = Catalog(conn, plan["dataset"])
    items = [dict(i) for i in plan["items"]]
    target = next((i for i in items if i["day"] == int(day) and i["meal"] == meal and i["pos"] == int(pos)), None)
    if not target:
        return {"status": "missing_data", "detail": {"reasons": ["Không tìm thấy vị trí món cần đổi"]}}
    before = dict(target)
    if new_recipe_id and new_recipe_id != target["recipe_id"]:
        rec = catalog.get_recipe(new_recipe_id)
        if not rec:
            d = {"reasons": [f"Món {new_recipe_id} chưa có công thức đã duyệt trong bộ dữ liệu {plan['dataset']}"]}
            repo.record_change(conn, plan_id, base_version, request_token, "missing_data", None, d)
            return {"status": "missing_data", "detail": d}
        target["recipe_id"], target["version"] = rec["recipe_id"], rec["version"]
    if new_servings is not None:
        target["servings"] = float(new_servings)
    report = verify_plan(conn, items, profile, constraints, plan["dataset"])
    change = {"slot": {"day": int(day), "meal": meal, "pos": int(pos)}, "before": before, "after": dict(target)}
    if report["status"] != "validated":
        violations = [x for x in report['results'] if x['status'] != 'pass']
        d = {"reasons": swap_failure_reasons(violations, report['structural']),
             "structural": report['structural'], "violations": violations, "change": change}
        repo.record_change(conn, plan_id, base_version, request_token, report["status"], None, d)
        return {"status": report["status"], "detail": d, "current_version": plan["current_version"]}
    ctx = dict(plan["context"])
    ctx.update(recipe_versions=report["sources"]["recipes"], price_versions=report["sources"]["price_versions"],
               usda_release=report["release_id"])
    name_before = (catalog.get_recipe(before["recipe_id"], before["version"]) or {}).get("name_vi", before["recipe_id"])
    name_after = (catalog.get_recipe(target["recipe_id"], target["version"]) or {}).get("name_vi", target["recipe_id"])
    note = (f"Ngày {day} {MEAL_LABELS.get(meal)}: {name_before} ×{fmt(before['servings'])} → {name_after} ×{fmt(target['servings'])}")
    v = repo.save_plan_version(conn, plan_id, int(base_version), items, report, ctx, note, request_token, {"change": change})
    if v is None:
        cur = repo.get_plan(conn, plan_id)
        d = {"reasons": ["Thực đơn vừa được cập nhật ở nơi khác. Hãy tải lại bản mới nhất."]}
        return {"status": "stale_plan", "detail": d, "current_version": cur["current_version"]}
    return {"status": "validated", "version": v, "detail": {"change": change, "note": note}}


def _checked_swap_choices(conn, plan, meal, day, pos):
    """Read-only: test each pair in the whole plan, using the same checker as save."""
    pv = repo.get_profile_version(conn, plan['profile_version'])
    if not pv or plan['version'] != plan['current_version'] or generation_blockers(conn, pv, data_mode=plan['dataset']):
        return [], None
    target = next((i for i in plan['items'] if i['day']==day and i['meal']==meal and i['pos']==pos), None)
    if target is None:
        return [], None
    cat = Catalog(conn, plan['dataset'])
    eligible = screen(cat, pv['data'], pv['constraints']).eligible
    levels = pv['data']['serving_levels']

    def valid_levels(rec, choices):
        allowed = []
        for level in choices:
            items = [dict(i) for i in plan['items']]
            item = next(i for i in items if i['day']==day and i['meal']==meal and i['pos']==pos)
            item.update(recipe_id=rec['recipe_id'], version=rec['version'], servings=level)
            report = verify_plan(conn, items, pv['data'], pv['constraints'], plan['dataset'])
            if report['status']=='validated':
                allowed.append(level)
        return allowed

    alternatives = []
    for candidate in eligible:
        rec = candidate['recipe']
        if meal not in rec['meal_types'] or rec['recipe_id']==target['recipe_id']:
            continue
        allowed = valid_levels(rec, levels)
        if allowed:
            alternatives.append({'recipe_id':rec['recipe_id'], 'name':rec['name_vi'], 'servings':allowed})
    current = cat.get_recipe(target['recipe_id'], target['version'])
    allowed = valid_levels(current, [l for l in levels if abs(l-target['servings'])>1e-9]) if current else []
    portion_change = {'recipe_id':current['recipe_id'], 'name':current['name_vi'], 'servings':allowed} if allowed else None
    return alternatives, portion_change


def swap_alternatives(conn, plan: dict, meal: str, day: int = 1, pos: int = 0) -> list[dict]:
    return _checked_swap_choices(conn, plan, meal, day, pos)[0]


def swap_choices(conn, settings, plan_id, base_version, day, meal, pos):
    plan = repo.get_plan(conn, plan_id)
    if not plan:
        return {'status':'missing_data', 'reasons':['Không tìm thấy thực đơn.']}
    if base_version != plan['current_version']:
        return {'status':'stale_plan', 'reasons':['Thực đơn đã thay đổi. Hãy tải lại để xem món thay thế phù hợp.']}
    pv = repo.get_profile_version(conn, plan['profile_version'])
    blockers = generation_blockers(conn, pv, data_mode=settings.data_mode)
    if blockers:
        return {'status':'missing_data', 'reasons':blockers}
    if not any(i['day']==day and i['meal']==meal and i['pos']==pos for i in plan['items']):
        return {'status':'missing_data', 'reasons':['Không tìm thấy vị trí món cần đổi.']}
    alternatives, portion_change = _checked_swap_choices(conn, plan, meal, day, pos)
    latest = repo.get_plan(conn, plan_id)
    if latest['version'] != base_version:
        return {'status':'stale_plan', 'reasons':['Thực đơn vừa thay đổi. Hãy tải lại trang.']}
    return {'status':'ok', 'base_version':base_version, 'alternatives':alternatives, 'portion_change':portion_change}


def explain(plan: dict) -> list[str]:
    """FR-10: giải thích bằng mẫu cố định, mọi số liệu lấy trực tiếp từ báo cáo kiểm tra."""
    rep = plan["report"]
    lines = []
    n_pass = sum(1 for r in rep["results"] if r["status"] == "pass")
    lines.append(f"Bộ kiểm tra độc lập đã tính lại {len(rep['dishes'])} món từ công thức, khẩu phần và dữ liệu USDA "
                 f"{rep['release_id']}; {n_pass}/{len(rep['results'])} điều kiện bắt buộc đạt.")
    for r in rep["results"]:
        lines.append(f"{r['scope_label']}: {r['detail']}.")
    ctx = plan.get("context") or {}
    for p in ctx.get("preferences", []):
        lines.append(f"Ưu tiên đã dùng để xếp hạng: {p['label']}.")
    sol = ctx.get("solver") or {}
    if sol:
        lines.append(f"Bộ giải {sol.get('name')} duyệt {sol.get('nodes')} nút trong {sol.get('elapsed_s')} giây"
                     + ("; đã chứng minh tối ưu theo điểm ưu tiên." if sol.get("proven_optimal") else
                        "; dừng theo giới hạn tìm kiếm – nghiệm đạt kiểm tra nhưng chưa chứng minh là tốt nhất."))
    return lines


def record_feedback(conn, recipe_id: str, liked: int | None, cook_minutes: float | None, note: str = "") -> dict:
    errors = {}
    if liked not in (None, 0, 1, -1):
        errors["liked"] = "Giá trị thích/không thích không hợp lệ"
    if cook_minutes is not None and not (0 < cook_minutes <= 600):
        errors["cook_minutes"] = "Thời gian nấu phải trong khoảng 1–600 phút"
    if not recipe_id:
        errors["recipe_id"] = "Thiếu mã món"
    if liked in (None, 0) and cook_minutes is None:
        errors["liked"] = "Chọn thích/không thích hoặc nhập thời gian nấu"
    if errors:
        return {"ok": False, "errors": errors}
    fid = repo.add_feedback(conn, recipe_id, liked if liked in (1, -1) else None, cook_minutes, note)
    return {"ok": True, "feedback_id": fid}


def metric_label(m):
    return METRICS[m][2]
