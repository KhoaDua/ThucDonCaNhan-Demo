"""Công cụ tra cứu CHỈ ĐỌC mà AI được phép gọi.

Python kiểm tra tham số, giới hạn số kết quả, tìm trong dữ liệu cục bộ và trả bản ghi có nguồn.
Kết quả tra cứu của AI chỉ dùng để diễn giải/giải thích; bộ lọc và bộ lập thực đơn luôn lấy danh
mục đã duyệt trực tiếp từ lớp tra cứu Python.
"""
from __future__ import annotations

import re

from app.catalog.lookup import METRICS, Catalog

RECIPE_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "get_recipe", "description": "Đọc công thức đã duyệt theo mã món: nguyên liệu (fdc_id), số gam, số khẩu phần gốc.",
        "parameters": {"type": "object", "properties": {"recipe_id": {"type": "string"}}, "required": ["recipe_id"]}}},
    {"type": "function", "function": {
        "name": "get_food_nutrients", "description": "Đọc hàm lượng dinh dưỡng trên 100 g theo mã nguyên liệu USDA fdc_id.",
        "parameters": {"type": "object", "properties": {"fdc_id": {"type": "integer"}}, "required": ["fdc_id"]}}},
    {"type": "function", "function": {
        "name": "search_recipes", "description": "Tìm món đã duyệt theo tên (giới hạn số kết quả).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
                       "required": ["query"]}}},
]


class ToolError(ValueError):
    pass


class ToolRegistry:
    def __init__(self, catalog: Catalog, max_results: int = 10):
        self.catalog = catalog
        self.max_results = max(1, min(int(max_results), 50))
        self.calls: list[dict] = []

    def call(self, name: str, args: dict) -> dict:
        try:
            if name == "get_recipe":
                out = self.get_recipe(args.get("recipe_id"))
            elif name == "get_food_nutrients":
                out = self.get_food_nutrients(args.get("fdc_id"))
            elif name == "search_recipes":
                out = self.search_recipes(args.get("query"), args.get("limit"))
            else:
                raise ToolError(f"Công cụ không được phép: {name}")
            self.calls.append({"tool": name, "args": args, "ok": True})
            return out
        except ToolError as e:
            self.calls.append({"tool": name, "args": args, "ok": False, "error": str(e)})
            return {"error": str(e)}

    def get_recipe(self, recipe_id) -> dict:
        if not isinstance(recipe_id, str) or not RECIPE_ID_RE.match(recipe_id):
            raise ToolError("recipe_id không hợp lệ")
        rec = self.catalog.get_recipe(recipe_id)
        if not rec:
            raise ToolError(f"Không có công thức đã duyệt {recipe_id}")
        return {"recipe_id": rec["recipe_id"], "version": rec["version"], "dataset": rec["dataset"], "name_vi": rec["name_vi"],
                "base_servings": rec["base_servings"], "meal_types": rec["meal_types"], "prep_minutes": rec["prep_minutes"],
                "source": rec["source_ref"],
                "ingredients": [{"line_no": i["line_no"], "fdc_id": i["fdc_id"], "quantity_g_edible": i["quantity_g_edible"],
                                 "name": self.catalog.ingredient_name(i["fdc_id"])} for i in rec["ingredients"][: self.max_results * 5]]}

    def get_food_nutrients(self, fdc_id) -> dict:
        try:
            fdc = int(fdc_id)
        except (TypeError, ValueError):
            raise ToolError("fdc_id phải là số nguyên")
        if not (0 < fdc < 100_000_000):
            raise ToolError("fdc_id ngoài phạm vi")
        food = self.catalog.get_food(fdc)
        if not food:
            raise ToolError(f"fdc_id {fdc} không có trong USDA {self.catalog.release}")
        nut = self.catalog.get_food_nutrients(fdc)
        return {"fdc_id": fdc, "description": food["description"], "release_id": self.catalog.release, "basis": "per 100 g",
                "nutrients": {m: {"amount": v["amount_per_100g"], "unit": METRICS[m][1], "food_nutrient_id": v["food_nutrient_id"]}
                              for m, v in nut.items()},
                "missing": [m for m in METRICS if m not in nut]}

    def search_recipes(self, query, limit=None) -> dict:
        if not isinstance(query, str) or not query.strip() or len(query) > 80:
            raise ToolError("query không hợp lệ")
        lim = self.max_results if limit is None else max(1, min(int(limit), self.max_results))
        return {"results": self.catalog.search_recipes(query, lim), "limit": lim}
