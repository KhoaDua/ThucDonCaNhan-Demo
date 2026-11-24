"""Danh sách mua sắm (FR-12) và chi phí mua theo gói.

    lượng cần mua = max(0, tổng lượng cần − lượng đang có)   (cùng mã fdc_id, cùng cơ sở gam phần ăn được)
    số gói        = ceil(lượng cần mua ÷ gam mỗi gói)         (chỉ khi có quy cách cùng cơ sở khối lượng)
    tổng tiền     = Σ số gói × giá gói                        (chỉ khi MỌI mặt hàng cần mua đều có giá)

Kết quả có giá chi tiết để tính tổng và đối chiếu ngân sách, nhưng giao diện chỉ hiển thị TỔNG tiền.
Không gộp hai mã fdc_id khác nhau (kể cả sống/chín).
"""
from __future__ import annotations

import math
from typing import Callable


def packages_needed(buy_g: float, package_g: float) -> int:
    if buy_g <= 0:
        return 0
    q = buy_g / package_g
    r = round(q)
    if abs(q - r) <= 1e-9 * max(1.0, q):  # tránh 2.0000000001 → 3
        return int(r)
    return int(math.ceil(q))


def build_shopping(needed: dict[int, float], inventory: dict[int, float], price_of: Callable[[int], dict | None],
                   name_of: Callable[[int], str]) -> dict:
    items = []
    purchase_parts, usage_parts = [], []
    missing_price, missing_package = [], []
    price_versions = {}
    for fdc in sorted(needed, key=lambda f: name_of(f)):
        need = needed[fdc]
        have = float(inventory.get(fdc, 0.0))
        buy = max(0.0, need - have)
        p = price_of(fdc)
        pkg_ok = bool(p and p.get("package_g") and (p.get("mass_basis") or "edible") == "edible")
        price_ok = bool(p and p.get("price") is not None)
        if p:
            price_versions[str(fdc)] = p.get("version")
        n_pkg = packages_needed(buy, float(p["package_g"])) if pkg_ok else None
        item = {"fdc_id": fdc, "name": name_of(fdc), "need_g": need, "have_g": min(have, need) if have else 0.0,
                "inventory_g": have, "buy_g": buy, "package_g": float(p["package_g"]) if pkg_ok else None,
                "packages": n_pkg,
                "package_status": "ok" if pkg_ok else ("không cần mua" if buy <= 0 else "chưa có quy cách cùng cơ sở khối lượng"),
                # dữ liệu xử lý nội bộ – KHÔNG hiển thị trên giao diện danh sách mua sắm
                "_price_per_package": float(p["price"]) if price_ok else None}
        items.append(item)
        if buy > 0:
            if not pkg_ok:
                missing_package.append(item["name"])
            elif not price_ok:
                missing_price.append(item["name"])
            else:
                purchase_parts.append(n_pkg * float(p["price"]))
        if price_ok and pkg_ok:
            usage_parts.append(need * float(p["price"]) / float(p["package_g"]))
    usage_complete = all(
        (price_of(f) or {}).get("price") is not None and (price_of(f) or {}).get("package_g")
        and ((price_of(f) or {}).get("mass_basis") or "edible") == "edible" for f in needed)
    purchase_complete = not missing_price and not missing_package
    return {
        "items": items,
        "purchase_total": math.fsum(purchase_parts) if purchase_complete else None,
        "purchase_status": "ok" if purchase_complete else "chưa tính được",
        "missing_price": missing_price,
        "missing_package": missing_package,
        "usage_total": math.fsum(usage_parts) if usage_complete else None,
        "currency": "VND",
        "price_versions": price_versions,
    }


def public_view(shopping: dict) -> dict:
    """Bản dành cho giao diện: bỏ mọi giá theo từng nguyên liệu (FR-12)."""
    return {k: ([{kk: vv for kk, vv in it.items() if not kk.startswith("_")} for it in v] if k == "items" else v)
            for k, v in shopping.items() if k != "price_versions"}
