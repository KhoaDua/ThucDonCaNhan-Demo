"""Luồng nhập tự do có AI (FR-02): câu tự do → BẢN NHÁP theo checklist → người dùng sửa/xác nhận.

Bản nháp không bao giờ được lưu ở trạng thái đã xác nhận. Nếu AI lỗi, giữ nguyên dữ liệu đã nhập để
người dùng hoàn tất bằng checklist. Trường mơ hồ/thiếu được đánh dấu tại ô nhập tương ứng.
"""
from __future__ import annotations

import time

from app.ai.providers import AIUnavailable, get_provider
from app.ai.tools import ToolRegistry
from app.catalog.lookup import MEAL_ORDER, METRICS, Catalog
from app.planner.profile import default_profile, parse_form, profile_to_form
from app.storage import repo


def _sanitize(draft: dict) -> tuple[dict, list]:
    out, bad = {}, []

    def take(key, fn):
        if key in draft:
            try:
                v = fn(draft[key])
                if v is not None:
                    out[key] = v
            except (TypeError, ValueError, KeyError, AttributeError):
                bad.append({"field": key, "question": f"AI trả giá trị không hợp lệ cho “{key}” – hãy nhập lại trong checklist."})

    take("days", lambda v: int(v))
    take("meals", lambda v: [m for m in MEAL_ORDER if m in list(v)] or None)
    take("dishes_per_meal", lambda v: int(v))
    take("serving_levels", lambda v: [float(x) for x in v])
    take("avoid", lambda v: [str(x).strip().lower() for x in v if str(x).strip()])
    take("likes", lambda v: [str(x).strip().lower() for x in v if str(x).strip()])
    take("dislikes", lambda v: [str(x).strip().lower() for x in v if str(x).strip()])
    take("prefer_variety", lambda v: bool(v))
    take("max_prep_minutes", lambda v: float(v))
    take("prep_hard", lambda v: bool(v))

    def lims(v):
        res = []
        for i, l in enumerate(v):
            if l.get("metric") not in METRICS:
                raise ValueError
            res.append({"metric": l["metric"], "op": l.get("op", "<="), "value": float(l["value"]),
                        "unit": l.get("unit") or METRICS[l["metric"]][1], "scope": l.get("scope", "day"),
                        "category": l.get("category", "health_limit"), "source": str(l.get("source") or ""),
                        "confirmed": False, "row": i})
        return res
    take("nutrient_limits", lims)

    def budget(b):
        return {"amount": float(b["amount"]), "currency": "VND", "scope": b.get("scope") or "period",
                "mode": b.get("mode") or "", "hard": bool(b.get("hard")), "drop_cost_if_missing": False}
    take("budget", budget)
    take("inventory", lambda v: [{"fdc_id": int(i["fdc_id"]), "grams": float(i["grams"])} for i in v])
    return out, bad


def interpret_free_text(conn, settings, text: str, current_form: dict) -> dict:
    base, _ = parse_form(current_form) if current_form.get("days") else (default_profile(), {})
    base = {**default_profile(), **{k: v for k, v in base.items() if v is not None}}
    keep_form = dict(current_form)
    keep_form["free_text"] = text
    provider = get_provider(settings)
    if provider is None:
        return {"ok": False, "form": keep_form, "errors": {}, "ambiguous": [], "ai_fields": [], "tool_calls": [],
                "message": "AI đang tắt (AI_PROVIDER=none). Hãy hoàn tất bằng checklist."}
    tools = ToolRegistry(Catalog(conn, settings.dataset), settings.ai_tool_max_results)
    t0 = time.time()
    try:
        res = provider.interpret(text, tools)
    except (AIUnavailable, Exception) as e:  # mọi lỗi AI đều dẫn về checklist
        repo.log_ai(conn, getattr(provider, "name", "?"), "interpret", False, {"error": str(e)[:300]})
        return {"ok": False, "form": keep_form, "errors": {}, "ambiguous": [], "ai_fields": [], "tool_calls": tools.calls,
                "message": f"AI không sẵn sàng hoặc trả dữ liệu lỗi ({e}). Thông tin đã nhập được giữ nguyên – "
                           f"hãy hoàn tất bằng checklist."}
    draft, bad = _sanitize(res.draft)
    merged = dict(base)
    merged.update(draft)
    form = profile_to_form(merged)
    form["free_text"] = text
    _, errors = parse_form(form)
    ambiguous = res.ambiguous + bad
    for a in ambiguous:
        errors[a["field"]] = a["question"]
    repo.log_ai(conn, res.provider, "interpret", True, {"fields": sorted(draft), "ambiguous": [a["field"] for a in ambiguous],
                                                         "tool_calls": res.tool_calls, "elapsed_s": round(time.time() - t0, 3)})
    return {"ok": True, "form": form, "errors": errors, "ambiguous": ambiguous, "ai_fields": sorted(draft),
            "tool_calls": res.tool_calls, "provider": res.provider, "note": res.raw_note,
            "message": "Đây là BẢN NHÁP do AI diễn giải. Hãy kiểm tra các ô được đánh dấu, sửa nếu cần rồi bấm Lưu để xem ràng buộc."}
