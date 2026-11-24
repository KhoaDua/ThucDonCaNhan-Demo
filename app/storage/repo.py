"""Lưu hồ sơ, thực đơn, kết quả kiểm tra và phản hồi."""
from __future__ import annotations

import json

from app.storage.db import now_iso, transaction

PROFILE_ID = 1  # ứng dụng cho duy nhất một cá nhân (FR-06)


def _j(x):
    return json.dumps(x, ensure_ascii=False, default=lambda o: list(o) if isinstance(o, (set, tuple)) else str(o))


# ---------------------------------------------------------------- hồ sơ

def save_profile_draft(conn, profile: dict, constraints: list, prefs: list, input_method: str = "checklist", *, status="draft") -> int:
    if status not in ("draft", "needs_information", "pending_review", "unsupported"):
        raise ValueError("Trạng thái lưu hồ sơ không hợp lệ.")
    with transaction(conn):
        v = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 AS v FROM profile_version WHERE profile_id=?", (PROFILE_ID,)).fetchone()["v"]
        conn.execute("INSERT INTO profile_version VALUES(?,?,?,?,?,?,?,?)",
                     (PROFILE_ID, v, status, _j(profile), _j({"constraints": constraints, "preferences": prefs}),
                      input_method, now_iso(), None))
    return v


def confirm_profile(conn, version: int) -> bool:
    pv = get_profile_version(conn, version)
    if not pv or (pv["data"].get("health_review") or {}).get("state", "ready") != "ready":
        return False
    with transaction(conn):
        cur = conn.execute("UPDATE profile_version SET status='confirmed', confirmed_at=? WHERE profile_id=? AND version=? AND status='draft'",
                           (now_iso(), PROFILE_ID, version))
    return cur.rowcount == 1


def get_profile_version(conn, version: int | None = None, confirmed_only: bool = False) -> dict | None:
    q = "SELECT * FROM profile_version WHERE profile_id=?"
    args: list = [PROFILE_ID]
    if version is not None:
        q += " AND version=?"
        args.append(version)
    if confirmed_only:
        q += " AND status='confirmed'"
    q += " ORDER BY version DESC LIMIT 1"
    r = conn.execute(q, args).fetchone()
    if not r:
        return None
    d = dict(r)
    d["data"] = json.loads(d.pop("data_json"))
    cj = json.loads(d.pop("constraints_json") or "{}")
    d["constraints"] = cj.get("constraints", [])
    d["preferences"] = cj.get("preferences", [])
    return d


# ---------------------------------------------------------------- thực đơn

def find_run_by_token(conn, token: str):
    if not token:
        return None
    r = conn.execute("SELECT * FROM plan_run WHERE request_token=?", (token,)).fetchone()
    return _run(r)


def _run(r):
    if not r:
        return None
    d = dict(r)
    d["detail"] = json.loads(d.pop("detail_json") or "{}")
    return d


def get_run(conn, run_id: int):
    return _run(conn.execute("SELECT * FROM plan_run WHERE run_id=?", (run_id,)).fetchone())


def save_run(conn, profile_version: int, token: str | None, status: str, detail: dict, plan_id: int | None = None) -> int:
    cur = conn.execute("INSERT INTO plan_run(profile_id, profile_version, request_token, status, detail_json, plan_id, created_at) "
                       "VALUES(?,?,?,?,?,?,?)", (PROFILE_ID, profile_version, token or None, status, _j(detail), plan_id, now_iso()))
    return cur.lastrowid


def create_plan(conn, profile_version: int, dataset: str, items: list, report: dict, context: dict,
                token: str | None, run_detail: dict) -> tuple[int, int]:
    """Lưu thực đơn đã kiểm tra + báo cáo + danh sách mua sắm trong MỘT giao dịch."""
    assert report["status"] == "validated", "Chỉ lưu thực đơn đã đạt kiểm tra"
    with transaction(conn):
        cur = conn.execute("INSERT INTO plan(profile_id, profile_version, dataset, current_version, created_at) VALUES(?,?,?,?,?)",
                           (PROFILE_ID, profile_version, dataset, 1, now_iso()))
        plan_id = cur.lastrowid
        conn.execute("INSERT INTO plan_version VALUES(?,?,?,?,?,?,?,?,?)",
                     (plan_id, 1, "validated", _j(items), _j(report), _j(report["shopping"]), _j(context), "Tạo mới", now_iso()))
        run_id = save_run(conn, profile_version, token, "validated", run_detail, plan_id)
    return plan_id, run_id


def get_plan(conn, plan_id: int, version: int | None = None) -> dict | None:
    p = conn.execute("SELECT * FROM plan WHERE plan_id=?", (plan_id,)).fetchone()
    if not p:
        return None
    v = version or p["current_version"]
    pv = conn.execute("SELECT * FROM plan_version WHERE plan_id=? AND version=?", (plan_id, v)).fetchone()
    if not pv:
        return None
    d = dict(p)
    d.update(version=v, status=pv["status"], items=json.loads(pv["items_json"]), report=json.loads(pv["report_json"]),
             shopping=json.loads(pv["shopping_json"]), context=json.loads(pv["context_json"]), change_note=pv["change_note"],
             version_created_at=pv["created_at"])
    d["versions"] = [dict(r) for r in conn.execute("SELECT version, change_note, created_at FROM plan_version WHERE plan_id=? ORDER BY version",
                                                   (plan_id,))]
    return d


def list_plans(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM plan ORDER BY plan_id DESC")]


def find_change_by_token(conn, token: str):
    if not token:
        return None
    r = conn.execute("SELECT * FROM plan_change WHERE request_token=?", (token,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["detail"] = json.loads(d.pop("detail_json") or "{}")
    return d


def record_change(conn, plan_id, base_version, token, status, result_version, detail):
    conn.execute("INSERT INTO plan_change(plan_id, base_version, request_token, status, result_version, detail_json, created_at) "
                 "VALUES(?,?,?,?,?,?,?)", (plan_id, base_version, token or None, status, result_version, _j(detail), now_iso()))


def save_plan_version(conn, plan_id: int, base_version: int, items: list, report: dict, context: dict, note: str,
                      token: str | None, detail: dict) -> int | None:
    """Ghi phiên bản mới nếu bản đang lưu vẫn là base_version (khóa lạc quan). Trả None nếu bản đã cũ."""
    with transaction(conn):
        cur = conn.execute("UPDATE plan SET current_version=? WHERE plan_id=? AND current_version=?",
                           (base_version + 1, plan_id, base_version))
        if cur.rowcount != 1:
            return None
        conn.execute("INSERT INTO plan_version VALUES(?,?,?,?,?,?,?,?,?)",
                     (plan_id, base_version + 1, "validated", _j(items), _j(report), _j(report["shopping"]), _j(context), note, now_iso()))
        record_change(conn, plan_id, base_version, token, "validated", base_version + 1, detail)
    return base_version + 1


# ---------------------------------------------------------------- phản hồi

def add_feedback(conn, recipe_id: str, liked: int | None, cook_minutes: float | None, note: str = "") -> int:
    with transaction(conn):
        cur = conn.execute("INSERT INTO feedback(profile_id, recipe_id, liked, cook_minutes, note, created_at) VALUES(?,?,?,?,?,?)",
                           (PROFILE_ID, recipe_id, liked, cook_minutes, note, now_iso()))
    return cur.lastrowid


def latest_feedback(conn) -> dict:
    """Quy tắc: mỗi trường (thích/không thích, thời gian nấu) lấy từ bản ghi MỚI NHẤT (id lớn nhất)
    của cùng món có cung cấp trường đó. Ghi rõ mã bản ghi được dùng."""
    out: dict = {}
    for r in conn.execute("SELECT * FROM feedback WHERE profile_id=? ORDER BY id", (PROFILE_ID,)):
        d = out.setdefault(r["recipe_id"], {"liked": None, "cook_minutes": None, "liked_feedback_id": None,
                                            "cook_feedback_id": None})
        if r["liked"] in (1, -1):
            d["liked"], d["liked_feedback_id"] = r["liked"], r["id"]
        if r["cook_minutes"] is not None:
            d["cook_minutes"], d["cook_feedback_id"] = r["cook_minutes"], r["id"]
    return out


def list_feedback(conn, limit: int = 50) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM feedback WHERE profile_id=? ORDER BY id DESC LIMIT ?", (PROFILE_ID, limit))]


def log_ai(conn, provider: str, action: str, ok: bool, detail: dict) -> None:
    conn.execute("INSERT INTO ai_log(provider, action, ok, detail_json, created_at) VALUES(?,?,?,?,?)",
                 (provider, action, 1 if ok else 0, _j(detail), now_iso()))
