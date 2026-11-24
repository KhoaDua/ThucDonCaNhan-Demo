"""Lớp tra cứu chỉ đọc cho hai bộ dữ liệu (công thức và dinh dưỡng nguyên liệu).

Bộ lọc, bộ lập thực đơn và công cụ AI đều đọc qua lớp này. Mỗi bản ghi trả về kèm nguồn
(phiên bản USDA, mã bản ghi `food_nutrient.id`, phiên bản công thức) để truy xuất.
"""
from __future__ import annotations

from app.storage.db import get_meta

# metric key -> (nutrient.id USDA, đơn vị, nhãn tiếng Việt)
METRICS: dict[str, tuple[int, str, str]] = {
    "energy_kcal": (1008, "kcal", "Năng lượng"),
    "protein_g": (1003, "g", "Protein"),
    "fat_g": (1004, "g", "Chất béo tổng"),
    "carb_g": (1005, "g", "Carbohydrate"),
    "fiber_g": (1079, "g", "Chất xơ"),
    "sodium_mg": (1093, "mg", "Natri"),
    "sugar_g": (2000, "g", "Đường tổng"),
    "saturated_fat_g": (1258, "g", "Chất béo bão hòa"),
}
NUTRIENT_TO_METRIC = {v[0]: k for k, v in METRICS.items()}
MEAL_LABELS = {"breakfast": "Bữa sáng", "lunch": "Bữa trưa", "dinner": "Bữa tối"}
MEAL_ORDER = ["breakfast", "lunch", "dinner"]


class Catalog:
    def __init__(self, conn, dataset: str):
        self.conn = conn
        self.dataset = dataset
        self.release = get_meta(conn, "active_usda_release")
        self._food_cache: dict[int, dict | None] = {}
        self._nut_cache: dict[int, dict] = {}

    # ----- USDA
    def release_info(self) -> dict | None:
        if not self.release:
            return None
        r = self.conn.execute("SELECT release_id, label, food_count, nutrient_row_count, imported_at FROM usda_release WHERE release_id=?",
                              (self.release,)).fetchone()
        return dict(r) if r else None

    def get_food(self, fdc_id: int) -> dict | None:
        if fdc_id not in self._food_cache:
            r = self.conn.execute("SELECT fdc_id, description, data_type, food_category_id, publication_date, ndb_number "
                                  "FROM usda_food WHERE release_id=? AND fdc_id=?", (self.release, fdc_id)).fetchone()
            self._food_cache[fdc_id] = dict(r) if r else None
        return self._food_cache[fdc_id]

    def get_food_nutrients(self, fdc_id: int) -> dict[str, dict]:
        """Trả {metric: {amount_per_100g, unit, food_nutrient_id, nutrient_id, release_id}}.
        Chỉ tiêu không có dòng dữ liệu sẽ KHÔNG có trong kết quả (chưa biết ≠ 0)."""
        if fdc_id not in self._nut_cache:
            ids = tuple(v[0] for v in METRICS.values())
            rows = self.conn.execute(
                f"SELECT fn.id, fn.nutrient_id, fn.amount, n.unit_name FROM usda_food_nutrient fn "
                f"JOIN usda_nutrient n ON n.release_id=fn.release_id AND n.id=fn.nutrient_id "
                f"WHERE fn.release_id=? AND fn.fdc_id=? AND fn.nutrient_id IN ({','.join('?' * len(ids))})",
                (self.release, fdc_id, *ids)).fetchall()
            out = {}
            for r in rows:
                m = NUTRIENT_TO_METRIC[r["nutrient_id"]]
                out[m] = {"amount_per_100g": r["amount"], "unit": r["unit_name"], "food_nutrient_id": r["id"],
                          "nutrient_id": r["nutrient_id"], "release_id": self.release}
            self._nut_cache[fdc_id] = out
        return self._nut_cache[fdc_id]

    def search_foods(self, query: str, limit: int = 10) -> list[dict]:
        q = f"%{query.strip()}%"
        rows = self.conn.execute("SELECT fdc_id, description FROM usda_food WHERE release_id=? AND description LIKE ? "
                                 "ORDER BY length(description), fdc_id LIMIT ?", (self.release, q, limit)).fetchall()
        return [dict(r) for r in rows]

    # ----- công thức
    def _recipe_row(self, recipe_id: str, version: int | None):
        if version is None:
            return self.conn.execute(
                "SELECT r.* FROM recipe r JOIN recipe_active a ON a.recipe_id=r.recipe_id AND a.version=r.version "
                "AND a.dataset=r.dataset WHERE r.recipe_id=? AND r.dataset=? AND r.status='approved'",
                (recipe_id, self.dataset)).fetchone()
        return self.conn.execute("SELECT * FROM recipe WHERE recipe_id=? AND version=?", (recipe_id, version)).fetchone()

    def get_recipe(self, recipe_id: str, version: int | None = None) -> dict | None:
        """Công thức kèm dòng nguyên liệu. `version=None` → phiên bản đã duyệt đang hoạt động."""
        r = self._recipe_row(recipe_id, version)
        if not r:
            return None
        rec = dict(r)
        rec["meal_types"] = [m for m in (rec["meal_types"] or "").split(";") if m]
        rec["ingredients"] = [dict(x) for x in self.conn.execute(
            "SELECT line_no, fdc_id, quantity_g_edible, food_state, conversion_source FROM recipe_ingredient "
            "WHERE recipe_id=? AND version=? ORDER BY line_no", (r["recipe_id"], r["version"]))]
        return rec

    def list_active_recipes(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT r.recipe_id, r.version FROM recipe r JOIN recipe_active a ON a.recipe_id=r.recipe_id "
            "AND a.version=r.version AND a.dataset=r.dataset WHERE r.dataset=? AND r.status='approved' ORDER BY r.recipe_id",
            (self.dataset,)).fetchall()
        return [self.get_recipe(r["recipe_id"], r["version"]) for r in rows]

    def search_recipes(self, query: str, limit: int = 10) -> list[dict]:
        q = f"%{query.strip()}%"
        rows = self.conn.execute(
            "SELECT r.recipe_id, r.version, r.name_vi, r.meal_types FROM recipe r JOIN recipe_active a ON "
            "a.recipe_id=r.recipe_id AND a.version=r.version AND a.dataset=r.dataset WHERE r.dataset=? AND "
            "r.status='approved' AND (r.name_vi LIKE ? OR r.recipe_id LIKE ?) ORDER BY r.recipe_id LIMIT ?",
            (self.dataset, q, q, limit)).fetchall()
        return [dict(r) for r in rows]

    # ----- nguyên liệu, giá, quy tắc
    def get_ingredient_info(self, fdc_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM ingredient_info WHERE fdc_id=? AND dataset=? ORDER BY version DESC LIMIT 1",
                              (fdc_id, self.dataset)).fetchone()
        return dict(r) if r else None

    def ingredient_name(self, fdc_id: int) -> str:
        info = self.get_ingredient_info(fdc_id)
        if info and info.get("name_vi"):
            return info["name_vi"]
        food = self.get_food(fdc_id)
        return food["description"] if food else f"fdc {fdc_id}"

    def get_price(self, fdc_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM price_item WHERE fdc_id=? AND dataset=? ORDER BY version DESC LIMIT 1",
                              (fdc_id, self.dataset)).fetchone()
        return dict(r) if r else None

    def avoid_tag_vocabulary(self) -> list[str]:
        tags = set()
        for r in self.conn.execute("SELECT avoid_tags FROM ingredient_info WHERE dataset=? AND review_status='reviewed'", (self.dataset,)):
            for t in (r["avoid_tags"] or "").split(";"):
                if t.strip():
                    tags.add(t.strip().lower())
        return sorted(tags)

    def reviewed_ingredients(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT i.fdc_id, i.name_vi, i.aliases, i.avoid_tags FROM ingredient_info i "
            "WHERE i.dataset=? AND i.review_status='reviewed' AND i.version=("
            "SELECT MAX(v.version) FROM ingredient_info v WHERE v.dataset=i.dataset AND v.fdc_id=i.fdc_id) "
            "ORDER BY i.name_vi, i.fdc_id", (self.dataset,))
        return [dict(r) for r in rows]

    def rule_sets(self, include_demo: bool) -> list[dict]:
        q = "SELECT rule_set, version, label, review_status, source_ref, audience, COUNT(*) AS n FROM nutrition_rule"
        if not include_demo:
            q += " WHERE label != 'demo' AND review_status='reviewed'"
        q += " GROUP BY rule_set, version ORDER BY rule_set, version"
        return [dict(r) for r in self.conn.execute(q)]

    def get_rules(self, rule_set: str, version: int | None = None) -> list[dict]:
        if version is None:
            v = self.conn.execute("SELECT MAX(version) AS v FROM nutrition_rule WHERE rule_set=?", (rule_set,)).fetchone()
            version = v["v"] if v else None
        rows = self.conn.execute("SELECT * FROM nutrition_rule WHERE rule_set=? AND version=? ORDER BY metric, op",
                                 (rule_set, version)).fetchall()
        return [dict(r) for r in rows]
