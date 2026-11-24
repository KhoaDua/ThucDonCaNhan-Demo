"""Bộ lập thực đơn (FR-06): chọn (ngày, bữa, công thức, mức khẩu phần) thỏa điều kiện bắt buộc.

Hai bộ giải dùng chung một mô hình:
- `builtin` (mặc định): nhánh và cận (branch & bound) thuần Python, có cắt tỉa theo giới hạn
  dinh dưỡng/chi phí từng bữa, từng ngày và cả kỳ. Khi duyệt hết cây mà không có nghiệm, kết luận
  KHÔNG KHẢ THI trên tập ứng viên đã xét. Khi hết thời gian/số nút, trả `timeout`.
- `milp`: quy hoạch tuyến tính nguyên hỗn hợp bằng `scipy.optimize.milp` (nếu cài SciPy).

Ràng buộc mô hình:
- Mỗi vị trí món (ngày, bữa, thứ tự) chọn đúng một phương án.
- Trong cùng một bữa không lặp công thức (thứ tự mã công thức tăng dần để phá đối xứng).
- Tổng chỉ tiêu theo phạm vi (bữa/ngày/kỳ) thỏa phép so sánh; ngân sách theo lượng dùng là tuyến tính.
- Ngân sách theo tiền mua gói (phi tuyến do làm tròn gói) được kiểm tra ở lá bằng `accept`
  (bộ kiểm tra độc lập) trong `builtin`, và bằng biến nguyên số gói trong `milp`.

Mục tiêu (nhỏ hơn là tốt hơn): tổng điểm ưu tiên + phạt lặp món. Hòa điểm: thứ tự ổn định theo
(điểm, mã công thức, khẩu phần) – kết quả lặp lại được với cùng dữ liệu và cấu hình.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable

from app.planner.nutrition import tolerance


@dataclass
class LinearCon:
    cid: str
    attr: str  # tên chỉ tiêu trong option["totals"] hoặc "cost_usage"
    op: str
    bound: float
    scope: str  # meal | day | period
    meal_type: str | None = None


@dataclass
class SolveResult:
    status: str  # found | infeasible | timeout
    assignment: list | None = None  # list option dict theo slot
    objective: float | None = None
    proven_optimal: bool = False
    nodes: int = 0
    elapsed_s: float = 0.0
    rejected_by_checker: int = 0
    solver: str = "builtin"
    note: str = ""
    extra: dict = field(default_factory=dict)


def _group_key(slot, scope):
    if scope == "meal":
        return (slot["day"], slot["meal"])
    if scope == "day":
        return slot["day"]
    return 0


def _val(opt, attr):
    if attr == "cost_usage":
        return opt["cost_usage"]
    return opt["totals"][attr]


def _applies(con, slot):
    return con.meal_type is None or con.meal_type == slot["meal"]


def solve_builtin(slots: list[dict], options: list[list[dict]], cons: list[LinearCon], *, variety_weight: float = 0.0,
                  accept: Callable[[list[dict]], bool] | None = None, timeout_s: float = 10.0,
                  max_nodes: int = 2_000_000, purchase: dict | None = None) -> SolveResult:
    """`purchase` = {"bound", "prices": {fdc: (package_g, price)}, "inventory": {fdc: g}}: tiền mua gói của phần
    đã gán là cận dưới của tiền mua cuối cùng (lượng cần chỉ tăng, làm tròn gói đơn điệu) nên dùng để cắt tỉa."""
    t0 = time.monotonic()
    n = len(slots)
    res = SolveResult(status="infeasible")
    if any(len(o) == 0 for o in options):
        res.elapsed_s = time.monotonic() - t0
        res.note = "Có vị trí món không có ứng viên"
        return res
    opts = [sorted(o, key=lambda x: (x["score"], x["recipe_id"], x["servings"])) for o in options]

    # nhóm theo ràng buộc
    groups = []  # per con: dict key -> list slot idx
    for c in cons:
        g: dict = {}
        for i, s in enumerate(slots):
            if not _applies(c, s):
                continue
            g.setdefault(_group_key(s, c.scope), []).append(i)
        groups.append(g)
    vmax = [[max(_val(o, c.attr) for o in opts[i]) for i in range(n)] for c in cons]
    vmin = [[min(_val(o, c.attr) for o in opts[i]) for i in range(n)] for c in cons]
    # phần còn lại (max/min) của nhóm sau vị trí i
    rem_max = [[0.0] * n for _ in cons]
    rem_min = [[0.0] * n for _ in cons]
    for ci, c in enumerate(cons):
        for key, idxs in groups[ci].items():
            for i in idxs:
                later = [j for j in idxs if j > i]
                rem_max[ci][i] = math.fsum(vmax[ci][j] for j in later)
                rem_min[ci][i] = math.fsum(vmin[ci][j] for j in later)
    slot_groups = [[_group_key(slots[i], c.scope) for c in cons] for i in range(n)]
    # trước khi gán bất cứ gì: kiểm tra nhanh khả thi theo cận
    for ci, c in enumerate(cons):
        for key, idxs in groups[ci].items():
            lo = math.fsum(vmin[ci][j] for j in idxs)
            hi = math.fsum(vmax[ci][j] for j in idxs)
            t = tolerance(c.bound)
            if (c.op in ("<=", "<") and lo > c.bound + t) or (c.op in (">=", ">") and hi < c.bound - t) \
                    or (c.op == "<" and lo >= c.bound - t) or (c.op == ">" and hi <= c.bound + t):
                res.elapsed_s = time.monotonic() - t0
                res.note = f"Cận cho thấy điều kiện {c.cid} không thể đạt ở nhóm {key}"
                res.extra["binding"] = [c.cid]
                return res

    min_score_suffix = [0.0] * (n + 1)
    for i in range(n - 1, -1, -1):
        min_score_suffix[i] = min_score_suffix[i + 1] + opts[i][0]["score"]

    sums = [dict.fromkeys(groups[ci], 0.0) for ci in range(len(cons))]
    used: dict[str, int] = {}
    assign: list = [None] * n
    best = {"obj": math.inf, "assign": None}
    state = {"nodes": 0, "stopped": False}
    prev_same_meal = [None] * n  # chỉ số slot trước trong cùng bữa
    last = {}
    for i, s in enumerate(slots):
        k = (s["day"], s["meal"])
        prev_same_meal[i] = last.get(k)
        last[k] = i

    def feasible_after(i):
        for ci, c in enumerate(cons):
            if not _applies(c, slots[i]):
                continue
            key = slot_groups[i][ci]
            cur = sums[ci][key]
            t = tolerance(c.bound)
            if c.op == "<=" and cur + rem_min[ci][i] > c.bound + t:
                return False
            if c.op == "<" and cur + rem_min[ci][i] >= c.bound - t:
                return False
            if c.op == ">=" and cur + rem_max[ci][i] < c.bound - t:
                return False
            if c.op == ">" and cur + rem_max[ci][i] <= c.bound + t:
                return False
        return True

    need: dict = {}
    pstate = {"cost": 0.0}

    def pk(f, g):
        pkg, _price = purchase["prices"][f]
        b = g - purchase["inventory"].get(f, 0.0)
        if b <= 1e-9:
            return 0
        q = b / pkg
        r = round(q)
        return int(r) if abs(q - r) <= 1e-9 * max(1.0, q) else int(math.ceil(q))

    def add_purchase(o, sign):
        delta = 0.0
        for f, g in o["grams"].items():
            old = need.get(f, 0.0)
            new = old + sign * g
            delta += purchase["prices"][f][1] * (pk(f, new) - pk(f, old))
            need[f] = new
        pstate["cost"] += delta

    mode = {"dynamic": False, "first_only": False, "deadline": t0 + timeout_s, "exhausted": True}

    def dfs(i, obj):
        if state["stopped"]:
            return
        if i == n:
            if obj < best["obj"] - 1e-12:
                chosen = list(assign)
                if accept is None or accept(chosen):
                    best["obj"], best["assign"] = obj, chosen
                    if mode["first_only"]:
                        state["stopped"] = True
                else:
                    res.rejected_by_checker += 1
            return
        if obj + min_score_suffix[i] >= best["obj"] - 1e-12:
            return
        p = prev_same_meal[i]
        # thứ tự động (pha 3): điểm + phạt lặp món hiện tại
        ordered = opts[i] if not (variety_weight and mode["dynamic"]) else sorted(
            opts[i], key=lambda x: (x["score"] + variety_weight * used.get(x["recipe_id"], 0),
                                    x["recipe_id"], x["servings"]))
        for o in ordered:
            state["nodes"] += 1
            if state["nodes"] % 512 == 0 and time.monotonic() > mode["deadline"]:
                state["stopped"] = True
                mode["exhausted"] = False
                return
            if state["nodes"] > max_nodes:
                state["stopped"] = True
                mode["exhausted"] = False
                return
            if p is not None and o["recipe_id"] <= assign[p]["recipe_id"]:
                continue
            pen = variety_weight * used.get(o["recipe_id"], 0)  # lần lặp thứ k bị phạt k × trọng số
            new_obj = obj + o["score"] + pen
            if new_obj + min_score_suffix[i + 1] >= best["obj"] - 1e-12:
                continue
            for ci, c in enumerate(cons):
                if _applies(c, slots[i]):
                    sums[ci][slot_groups[i][ci]] += _val(o, c.attr)
            ok_p = True
            if purchase:
                add_purchase(o, +1)
                ok_p = pstate["cost"] <= purchase["bound"] + tolerance(purchase["bound"])
            if ok_p and feasible_after(i):
                assign[i] = o
                used[o["recipe_id"]] = used.get(o["recipe_id"], 0) + 1
                dfs(i + 1, new_obj)
                used[o["recipe_id"]] -= 1
                assign[i] = None
            for ci, c in enumerate(cons):
                if _applies(c, slots[i]):
                    sums[ci][slot_groups[i][ci]] -= _val(o, c.attr)
            if purchase:
                add_purchase(o, -1)
            if state["stopped"]:
                return

    def evaluate(a):
        """Tính lại từ đầu: (mục tiêu, khả thi) cho một phương án đầy đủ."""
        cnt: dict = {}
        obj = 0.0
        for o in a:
            obj += o["score"] + variety_weight * cnt.get(o["recipe_id"], 0)
            cnt[o["recipe_id"]] = cnt.get(o["recipe_id"], 0) + 1
        for i in range(n):
            q = prev_same_meal[i]
            if q is not None and a[i]["recipe_id"] == a[q]["recipe_id"]:
                return obj, False
        meal_ids: dict = {}
        for i in range(n):
            k = (slots[i]["day"], slots[i]["meal"])
            if a[i]["recipe_id"] in meal_ids.setdefault(k, set()):
                return obj, False
            meal_ids[k].add(a[i]["recipe_id"])
        for ci, c in enumerate(cons):
            tot: dict = {}
            for i in range(n):
                if not _applies(c, slots[i]):
                    continue
                tot[slot_groups[i][ci]] = tot.get(slot_groups[i][ci], 0.0) + _val(a[i], c.attr)
            for v in tot.values():
                if not _cmp(v, c.op, c.bound):
                    return obj, False
        if purchase:
            g: dict = {}
            for o in a:
                for f, x in o["grams"].items():
                    g[f] = g.get(f, 0.0) + x
            cost = math.fsum(purchase["prices"][f][1] * pk(f, x) for f, x in g.items())
            if cost > purchase["bound"] + tolerance(purchase["bound"]):
                return obj, False
        return obj, True

    def local_improve(deadline):
        """Pha 2: leo đồi – thay từng vị trí món nếu giảm mục tiêu và vẫn khả thi."""
        improved = True
        while improved and time.monotonic() < deadline:
            improved = False
            for i in range(n):
                for o in opts[i]:
                    if time.monotonic() > deadline:
                        return
                    cur = best["assign"]
                    if o is cur[i]:
                        continue
                    cand = list(cur)
                    cand[i] = o
                    # giữ thứ tự mã tăng dần trong cùng bữa (chuẩn hóa)
                    k = (slots[i]["day"], slots[i]["meal"])
                    idx = [j for j in range(n) if (slots[j]["day"], slots[j]["meal"]) == k]
                    vals = sorted((cand[j] for j in idx), key=lambda x: x["recipe_id"])
                    for j, v in zip(idx, vals):
                        cand[j] = v
                    obj, ok = evaluate(cand)
                    if ok and obj < best["obj"] - 1e-9 and (accept is None or accept(cand)):
                        best["obj"], best["assign"] = obj, cand
                        improved = True

    # Pha 1: tìm nghiệm khả thi đầu tiên theo thứ tự điểm tĩnh
    mode.update(dynamic=False, first_only=True, deadline=t0 + timeout_s * 0.5)
    dfs(0, 0.0)
    proven = False
    infeasible_proven = False
    if best["assign"] is None:
        if mode["exhausted"]:
            infeasible_proven = True
        else:  # chưa tìm thấy: dùng nốt thời gian còn lại cho pha 1
            state["stopped"] = False
            mode.update(exhausted=True, deadline=t0 + timeout_s)
            dfs(0, 0.0)
            infeasible_proven = best["assign"] is None and mode["exhausted"]
    if best["assign"] is not None:
        # Pha 2: cải thiện cục bộ; Pha 3: nhánh và cận với nghiệm hiện có để chứng minh/tìm tốt hơn
        local_improve(t0 + timeout_s * 0.75)
        state["stopped"] = False
        mode.update(dynamic=True, first_only=False, deadline=t0 + timeout_s, exhausted=True)
        dfs(0, 0.0)
        proven = mode["exhausted"]
    res.nodes = state["nodes"]
    res.elapsed_s = time.monotonic() - t0
    if best["assign"] is not None:
        res.status = "found"
        res.assignment = best["assign"]
        res.objective = best["obj"]
        res.proven_optimal = proven
        if not proven:
            res.note = "Dừng do giới hạn thời gian/số nút: nghiệm đã kiểm tra đạt nhưng chưa chứng minh là tốt nhất"
    elif infeasible_proven:
        res.status = "infeasible"
        res.note = "Đã duyệt hết tập ứng viên, không có phương án thỏa mọi điều kiện bắt buộc"
    else:
        res.status = "timeout"
        res.note = f"Hết thời gian ({timeout_s:g} giây) hoặc vượt {max_nodes} nút khi chưa có phương án đạt kiểm tra"
    return res


def _cmp(v, op, bound):
    from app.planner.nutrition import compare
    return compare(v, op, bound)


def milp_available() -> bool:
    """False nếu SciPy chưa cài hoặc DLL bị chặn (ImportError/OSError…)."""
    try:
        from scipy.optimize import milp  # noqa: F401
        return True
    except Exception:
        return False


def solve_milp(slots: list[dict], options: list[list[dict]], cons: list[LinearCon], *, variety_weight: float = 0.0,
               purchase: dict | None = None, accept: Callable[[list[dict]], bool] | None = None,
               timeout_s: float = 10.0) -> SolveResult:
    """MILP bằng SciPy. `purchase` = {"bound", "prices": {fdc: (package_g, price)}, "inventory": {fdc: g}}."""
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import lil_matrix

    t0 = time.monotonic()
    res = SolveResult(status="infeasible", solver="milp")
    if any(len(o) == 0 for o in options):
        res.note = "Có vị trí món không có ứng viên"
        return res
    var = []  # (slot, option)
    for i, opts in enumerate(options):
        for o in sorted(opts, key=lambda x: (x["score"], x["recipe_id"], x["servings"])):
            var.append((i, o))
    nx = len(var)
    recipes = sorted({o["recipe_id"] for _, o in var})
    r_index = {r: k for k, r in enumerate(recipes)}
    kmax = max(1, len(slots) - 1)
    zkeys = [(r, k) for r in recipes for k in range(1, kmax + 1)] if variety_weight > 0 else []
    nz = len(zkeys)
    fdcs = sorted({f for _, o in var for f in o["grams"]}) if purchase else []
    f_index = {f: k for k, f in enumerate(fdcs)}
    npk = len(fdcs)
    nv = nx + nz + npk

    c = np.zeros(nv)
    for k, (_, o) in enumerate(var):
        c[k] = o["score"] + 1e-7 * k  # phá hòa ổn định theo thứ tự sắp xếp
    for k in range(nz):
        c[nx + k] = variety_weight
    rows, lb, ub = [], [], []

    def add(coefs: dict, lo, hi):
        rows.append(coefs)
        lb.append(lo)
        ub.append(hi)

    for i in range(len(slots)):
        add({k: 1.0 for k, (s, _) in enumerate(var) if s == i}, 1, 1)
    meal_groups: dict = {}
    for k, (i, o) in enumerate(var):
        meal_groups.setdefault((slots[i]["day"], slots[i]["meal"], o["recipe_id"]), []).append(k)
    for ks in meal_groups.values():
        if len(ks) > 1:
            add({k: 1.0 for k in ks}, -np.inf, 1)
    for con in cons:
        gs: dict = {}
        for k, (i, o) in enumerate(var):
            if not _applies(con, slots[i]):
                continue
            gs.setdefault(_group_key(slots[i], con.scope), {})[k] = _val(o, con.attr)
        t = max(tolerance(con.bound), 1e-6 * max(1.0, abs(con.bound)))
        for coefs in gs.values():
            if con.op == "<=":
                add(coefs, -np.inf, con.bound)
            elif con.op == "<":
                add(coefs, -np.inf, con.bound - t)
            elif con.op == ">=":
                add(coefs, con.bound, np.inf)
            elif con.op == ">":
                add(coefs, con.bound + t, np.inf)
    if nz:
        # phạt lặp tăng dần: Σ_k max(0, n_r − k) = n_r(n_r−1)/2, giống bộ giải builtin
        for zi, (r, kk) in enumerate(zkeys):  # z_{r,k} ≥ Σx_r − k
            coefs = {k: 1.0 for k, (_, o) in enumerate(var) if o["recipe_id"] == r}
            coefs[nx + zi] = -1.0
            add(coefs, -np.inf, kk)
    if purchase:
        for f, fk in f_index.items():  # pkg·p_f − Σ g·x ≥ −inv
            pkg, _price = purchase["prices"][f]
            coefs = {k: -o["grams"][f] for k, (_, o) in enumerate(var) if f in o["grams"]}
            coefs[nx + nz + fk] = float(pkg)
            add(coefs, -float(purchase["inventory"].get(f, 0.0)), np.inf)
        add({nx + nz + fk: float(purchase["prices"][f][1]) for f, fk in f_index.items()}, -np.inf, purchase["bound"])

    A = lil_matrix((len(rows), nv))
    for r, coefs in enumerate(rows):
        for k, v in coefs.items():
            A[r, k] = v
    integrality = np.zeros(nv)
    integrality[:nx] = 1
    integrality[nx + nz:] = 1
    lo_b = np.zeros(nv)
    hi_b = np.full(nv, np.inf)
    hi_b[:nx] = 1
    out = milp(c, integrality=integrality, bounds=Bounds(lo_b, hi_b),
               constraints=LinearConstraint(A.tocsr(), np.array(lb), np.array(ub)),
               options={"time_limit": float(timeout_s), "disp": False})
    res.elapsed_s = time.monotonic() - t0
    if out.x is None:
        res.status = "infeasible" if out.status == 2 else "timeout"
        res.note = f"SciPy milp: {out.message}"
        return res
    chosen: list = [None] * len(slots)
    for k, (i, o) in enumerate(var):
        if out.x[k] > 0.5:
            chosen[i] = o
    if any(ch is None for ch in chosen):
        res.status = "timeout"
        res.note = "Nghiệm MILP không đầy đủ"
        return res
    if accept is not None and not accept(chosen):
        res.status = "rejected"
        res.rejected_by_checker = 1
        res.note = "Bộ kiểm tra độc lập không công nhận nghiệm MILP"
        return res
    res.status = "found"
    res.assignment = chosen
    res.objective = float(out.fun)
    res.proven_optimal = out.status == 0
    res.note = f"SciPy milp: {out.message}"
    return res
