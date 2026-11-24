"""Dịch các câu trả lời ngắn thành lựa chọn trong danh mục và nguồn quy tắc."""
from __future__ import annotations

import re
import unicodedata

from app.catalog.demo import DEMO_RULE_SET
from app.planner.profile import _all, _first


GOALS = {
    "variety": "Ăn đa dạng, ít lặp món",
    "energy": "Kiểm soát năng lượng",
    "less_salty": "Ăn ít mặn",
}


def name_key(text: str) -> str:
    text = unicodedata.normalize("NFD", text.casefold().replace("đ", "d"))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def ingredient_terms(info: dict) -> list[str]:
    return [term.strip() for term in (
        [info.get("name_vi") or ""] + (info.get("aliases") or "").split(";")
        + (info.get("avoid_tags") or "").split(";")
    ) if term.strip()]


def _entries(text: str) -> list[str]:
    return list(dict.fromkeys(entry.strip() for entry in re.split(r"[,;\n]+", text) if entry.strip()))


def _ingredients(entries: list[str], rows: list[dict], field: str, errors: dict) -> list[dict]:
    terms_by_key = {}
    for info in rows:
        for term in ingredient_terms(info):
            terms_by_key.setdefault(name_key(term), {})[info["fdc_id"]] = (term, info["name_vi"])
    choices = {}
    for entry in entries:
        key = name_key(entry)
        matches = terms_by_key.get(key)
        if not matches:
            errors[field] = f"Chưa nhận diện được “{entry}”. Hãy chọn tên hoặc nhóm trong danh mục gợi ý."
            continue
        ids = tuple(sorted(matches))
        name, canonical = next(iter(matches.values()))
        if canonical and set(terms_by_key[name_key(canonical)]) == set(ids):
            name = canonical
        choices.setdefault(ids, {"name": name, "key": key, "fdc_ids": list(ids)})
    return list(choices.values())


def dish_choice(recipe: dict) -> str:
    return f"{recipe['name_vi']} [{recipe['recipe_id']}]"


def _dishes(text, recipes, field, errors):
    dishes = {}
    for entry in dict.fromkeys(line.strip() for line in text.splitlines() if line.strip()):
        key = name_key(entry)
        stored_id = re.search(r"\[([^\[\]]+)\]$", entry)
        matches = [r for r in recipes if key in (name_key(r["name_vi"]), name_key(r["recipe_id"]))
                   or (stored_id and name_key(stored_id.group(1)) == name_key(r["recipe_id"]))]
        if len(matches) != 1:
            errors[field] = (f"Có nhiều món tên “{entry}”. Hãy chọn món kèm mã trong danh mục."
                             if matches else f"Chưa tìm thấy món “{entry}”. Hãy chọn tên trong danh mục.")
            continue
        recipe = matches[0]
        dishes[recipe["recipe_id"]] = recipe["name_vi"]
    return dishes


FOOD_GROUPS = (
    ("chicken", "Thịt gà", "gà"), ("pork", "Thịt heo", "heo"),
    ("fish", "Cá và sản phẩm từ cá", "cá"), ("shrimp", "Tôm", "tôm"),
    ("molluscs", "Bạch tuộc / nhuyễn thể", "nhuyễn thể"), ("eggs", "Trứng", "trứng"),
    ("milk", "Sữa và chế phẩm từ sữa", "sữa"), ("soy", "Đậu nành và sản phẩm từ đậu nành", "đậu nành"),
    ("peanut", "Đậu phộng (lạc)", "đậu phộng"), ("wheat", "Lúa mì", "lúa mì"),
    ("gluten", "Thực phẩm có gluten", "gluten"), ("mushroom", "Nấm", "nấm"),
)
ALLERGEN_IDS = {"fish", "shrimp", "molluscs", "eggs", "milk", "soy", "peanut", "wheat", "gluten"}


def food_options(catalog):
    """Danh mục lựa chọn tạo từ dữ liệu đã rà soát; không suy diễn phân loại bệnh."""
    rows = catalog.reviewed_ingredients()
    options, covered = [], set()
    for key, label, term in FOOD_GROUPS:
        choices = _ingredients([term], rows, "", {})
        if choices:
            choice = choices[0]
            ids = sorted({f for c in choices for f in c["fdc_ids"]})
            options.append({"id": key, "name": label, "key": term, "fdc_ids": ids})
            covered.update(ids)
    allergens = [o for o in options if o["id"] in ALLERGEN_IDS]
    for row in rows:
        if row["fdc_id"] not in covered:
            options.append({"id": "food_" + str(row["fdc_id"]), "name": row["name_vi"],
                            "key": name_key(row["name_vi"]), "fdc_ids": [row["fdc_id"]]})
    return options, allergens


def _selected_options(form, field, options, errors):
    by_id = {o["id"]: o for o in options}
    ids = list(dict.fromkeys(_all(form, field)))
    if any(key not in by_id for key in ids):
        errors[field] = "Có lựa chọn không còn trong danh mục. Hãy chọn lại các thực phẩm có sẵn."
    return [dict(by_id[key]) for key in ids if key in by_id]


def parse_checklist(form: dict, catalog) -> tuple[dict, dict]:
    goals = list(dict.fromkeys(_all(form, "goals")))
    errors = {}
    if _first(form, "food_checklist_version") != "1":
        errors["food_checklist"] = "Checklist đã thay đổi. Hãy tải lại trang và chọn danh sách thực phẩm mới."
    if any(goal not in GOALS for goal in goals):
        errors["goals"] = "Hãy chọn mục tiêu trong checklist."
    options, allergens = food_options(catalog)
    allergy_ids = list(dict.fromkeys(_all(form, "allergen_choices")))
    if not allergy_ids:
        errors["allergen_choices"] = "Chọn thực phẩm dị ứng hoặc xác nhận Không có."
    if "none" in allergy_ids and len(allergy_ids) > 1:
        errors["allergen_choices"] = "Không thể chọn Không có cùng với thực phẩm dị ứng."
    allergy_form = {"allergen_choices": [k for k in allergy_ids if k != "none"]}
    allergies = _selected_options(allergy_form, "allergen_choices", allergens, errors)
    answer = _first(form, "food_restrictions")
    if answer not in ("yes", "no"):
        errors["food_restrictions"] = "Chọn Có hoặc Không về thực phẩm cần tránh/hạn chế."
    ingredients = _selected_options(form, "avoid_food_choices", options, errors)
    limited = _selected_options(form, "limit_food_choices", options, errors)
    recipes = {r["recipe_id"]: r["name_vi"] for r in catalog.list_active_recipes()}
    dish_ids = list(dict.fromkeys(_all(form, "avoid_dish_choices")))
    if any(rid not in recipes for rid in dish_ids):
        errors["avoid_dish_choices"] = "Món đã chọn không còn trong danh mục. Hãy chọn lại."
    dishes = {rid: recipes[rid] for rid in dish_ids if rid in recipes}
    if answer == "yes" and not (ingredients or limited or dishes):
        errors["food_restrictions"] = "Hãy chọn ít nhất một thực phẩm cần tránh/hạn chế hoặc món cần tránh."
    if answer == "no" and (ingredients or limited or dishes):
        errors["food_restrictions"] = "Bạn đã chọn thực phẩm/món. Hãy chọn Có hoặc bỏ các lựa chọn đó."
    avoid_ids = {f for choice in ingredients + allergies for f in choice["fdc_ids"]}
    if any(avoid_ids.intersection(choice["fdc_ids"]) for choice in limited):
        errors["limit_food_choices"] = "Một thực phẩm đang vừa cần loại trừ vừa cần hạn chế. Hãy giữ ở danh sách loại trừ."
    return {"food_checklist_version": 1, "goals": goals, "allergies": allergies,
            "allergen_choices": allergy_ids,
            "food_restrictions": answer, "ingredients": ingredients, "limited_ingredients": limited,
            "dishes": dishes}, errors


def goal_rules(goals: list[str], catalog, settings, *, personal_energy=False):
    sources = {
        "energy": ("energy_kcal", settings.checklist_energy_rule_set),
        "less_salty": ("sodium_mg", settings.checklist_sodium_rule_set),
    }
    selected, errors = [], {}
    for goal, (metric, configured_set) in sources.items():
        if goal not in goals or (goal == "energy" and personal_energy):
            continue
        rule_set = configured_set or (DEMO_RULE_SET if settings.data_mode == "demo" else "")
        rules = [r for r in catalog.get_rules(rule_set) if r["metric"] == metric] if rule_set else []
        if goal == "less_salty":
            rules = [r for r in rules if r["op"] in ("<", "<=")]
        if not rules or any(
            not r["source_ref"] or (r["label"] == "demo" and settings.data_mode != "demo")
            or (r["label"] != "demo" and r["review_status"] != "reviewed") for r in rules
        ):
            errors["goals"] = (f"Chưa có quy tắc đã duyệt cho mục tiêu “{GOALS[goal]}”. "
                               "Bạn có thể chọn mục tiêu khác hoặc chờ nhóm quản trị bổ sung.")
            continue
        selected.extend(rules)
    return selected, errors
