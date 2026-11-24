"""Bộ dữ liệu DEMO có nhãn riêng (dataset = "demo").

- Công thức, định lượng, giá, quy cách và quy tắc dưới đây là DỮ LIỆU MẪU do nhóm phát triển
  soạn để chạy thử luồng. Chúng KHÔNG phải công thức/giá đã duyệt và KHÔNG phải khuyến nghị
  dinh dưỡng.
- Mã `fdc_id` là mã thật của USDA SR Legacy; mô tả USDA mong đợi được ghi kèm và bộ nạp
  kiểm tra khớp mô tả trước khi dùng. Hàm lượng dinh dưỡng luôn đọc từ bản USDA đã nhập.
- Hai món D18, D19 cố ý dùng nguyên liệu USDA không có dòng natri để minh họa FR-05.
- Món D17 cố ý nhiều natri để minh họa đổi món bị từ chối (FR-11).
"""
from __future__ import annotations

import csv
import io

DEMO_SOURCE = "DEMO – dữ liệu mẫu của nhóm phát triển, chưa duyệt"

# fdc_id: (tên tiếng Việt, bí danh, nhãn cần tránh, đầu mô tả USDA mong đợi)
DEMO_INGREDIENTS: dict[int, tuple[str, str, str, str]] = {
    170379: ("Bông cải xanh", "súp lơ xanh", "rau", "Broccoli, raw"),
    171287: ("Trứng gà", "trứng", "trứng", "Egg, whole, raw, fresh"),
    174531: ("Nước mắm", "", "cá;hải sản;nước chấm", "Sauce, fish, ready-to-serve"),
    168878: ("Cơm trắng", "cơm;gạo", "gạo", "Rice, white, long-grain, regular, enriched, cooked"),
    171477: ("Ức gà chín", "thịt gà", "gà;thịt gia cầm;thịt", "Chicken, broilers or fryers, breast, meat only, cooked, roasted"),
    171077: ("Ức gà sống", "thịt gà", "gà;thịt gia cầm;thịt", "Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw"),
    167902: ("Thịt heo xay", "thịt lợn;thịt heo", "heo;thịt lợn;thịt", "Pork, fresh, ground, raw"),
    172475: ("Đậu phụ", "đậu hũ;tàu hũ", "đậu nành", "Tofu, raw, firm, prepared with calcium sulfate"),
    168462: ("Cải bó xôi", "rau chân vịt;rau bina", "rau", "Spinach, raw"),
    169975: ("Bắp cải", "cải bắp", "rau", "Cabbage, raw"),
    170457: ("Cà chua", "", "rau", "Tomatoes, red, ripe, raw"),
    170393: ("Cà rốt", "", "rau", "Carrots, raw"),
    170000: ("Hành tây", "", "rau", "Onions, raw"),
    169230: ("Tỏi", "", "gia vị", "Garlic, raw"),
    175176: ("Cá rô phi", "cá diêu hồng", "cá;hải sản", "Fish, tilapia, raw"),
    175179: ("Tôm", "", "tôm;hải sản;giáp xác", "Crustaceans, shrimp, raw"),
    174277: ("Nước tương", "xì dầu", "đậu nành;lúa mì;gluten;nước chấm", "Soy sauce made from soy and wheat (shoyu)"),
    171411: ("Dầu đậu nành", "dầu ăn", "đậu nành;dầu", "Oil, soybean, salad or cooking"),
    173468: ("Muối ăn", "muối", "gia vị", "Salt, table"),
    169655: ("Đường cát", "đường", "đường", "Sugars, granulated"),
    168914: ("Bánh phở/bún chín", "bún;phở;bánh phở", "gạo", "Rice noodles, cooked"),
    174924: ("Bánh mì trắng", "bánh mì", "lúa mì;gluten", "Bread, white, commercially prepared"),
    173944: ("Chuối", "", "trái cây", "Bananas, raw"),
    169251: ("Nấm mỡ trắng", "nấm", "nấm", "Mushrooms, white, raw"),
    168409: ("Dưa leo", "dưa chuột", "rau", "Cucumber, with peel, raw"),
    169957: ("Giá đỗ", "giá", "đậu xanh;rau", "Mung beans, mature seeds, sprouted, raw"),
    170005: ("Hành lá", "", "rau", "Onions, spring or scallions"),
    170931: ("Tiêu đen", "tiêu", "gia vị", "Spices, pepper, black"),
    169997: ("Ngò rí", "rau mùi", "rau", "Coriander (cilantro) leaves, raw"),
    170390: ("Cải thìa", "cải chíp;bok choy", "rau", "Cabbage, chinese (pak-choi), raw"),
    168573: ("Sả", "", "gia vị", "Lemon grass (citronella), raw"),
    172430: ("Đậu phộng", "lạc", "đậu phộng;lạc;hạt", "Peanuts, all types, raw"),
    169705: ("Yến mạch", "", "yến mạch;gluten", "Oats"),
    171267: ("Sữa tươi ít béo", "sữa", "sữa;lactose", "Milk, reduced fat, fluid, 2% milkfat, with added vitamin A"),
    170886: ("Sữa chua không đường", "sữa chua", "sữa;lactose", "Yogurt, plain, low fat"),
    169231: ("Gừng", "", "gia vị", "Ginger root, raw"),
    168156: ("Nước cốt chanh", "chanh", "trái cây", "Lime juice, raw"),
    175165: ("Cá trê/cá da trơn", "cá basa", "cá;hải sản", "Fish, catfish, channel, farmed, raw"),
    170106: ("Ớt đỏ", "ớt", "gia vị;cay", "Peppers, hot chili, red, raw"),
    173043: ("Trái tầm bóp", "lồng đèn;physalis", "trái cây", "Groundcherries, (cape-gooseberries or poha), raw"),
    168019: ("Bạch tuộc", "", "hải sản;nhuyễn thể", "Octopus (Alaska Native)"),
}

# recipe_id, tên, khẩu phần gốc, bữa phù hợp, phút chuẩn bị, [(fdc_id, gam phần ăn được cho cả công thức)]
DEMO_RECIPES = [
    ("D01", "Trứng chiên cà chua", 2, "breakfast;dinner", 15,
     [(171287, 200), (170457, 200), (170000, 30), (171411, 10), (174531, 10), (170005, 10)]),
    ("D02", "Bánh mì trứng ốp la dưa leo", 2, "breakfast", 10,
     [(174924, 160), (171287, 100), (168409, 80), (174277, 8), (171411, 8)]),
    ("D03", "Cháo yến mạch chuối sữa", 2, "breakfast", 10,
     [(169705, 100), (171267, 400), (173944, 240)]),
    ("D04", "Phở gà", 2, "breakfast;lunch", 40,
     [(168914, 500), (171477, 200), (170000, 40), (170005, 20), (169231, 10), (174531, 20), (169997, 10), (169957, 100)]),
    ("D05", "Sữa chua chuối đậu phộng", 2, "breakfast", 5,
     [(170886, 300), (173944, 240), (172430, 40)]),
    ("D06", "Cơm gà xào bông cải", 2, "lunch;dinner", 25,
     [(168878, 450), (171077, 250), (170379, 300), (169230, 10), (171411, 15), (174277, 15)]),
    ("D07", "Cơm cá rô phi kho tiêu", 2, "lunch;dinner", 35,
     [(168878, 450), (175176, 300), (174531, 25), (169655, 10), (170931, 2), (170005, 10), (171411, 10)]),
    ("D08", "Cơm canh cải thìa đậu phụ", 2, "lunch;dinner", 20,
     [(168878, 450), (172475, 300), (170390, 300), (170005, 10), (173468, 3), (171411, 10)]),
    ("D09", "Cơm thịt heo xay xào nấm", 2, "lunch;dinner", 25,
     [(168878, 450), (167902, 250), (169251, 200), (170000, 50), (174277, 15), (171411, 10), (170931, 1)]),
    ("D10", "Bún tôm xào rau", 2, "lunch;dinner", 25,
     [(168914, 450), (175179, 250), (169975, 200), (170393, 100), (169230, 10), (174531, 15), (171411, 10)]),
    ("D11", "Cơm cá trê sốt cà", 2, "lunch;dinner", 30,
     [(168878, 450), (175165, 300), (170457, 250), (170000, 50), (174531, 15), (171411, 15), (169655, 5)]),
    ("D12", "Cơm gỏi gà bắp cải", 2, "lunch;dinner", 20,
     [(168878, 400), (171477, 200), (169975, 300), (170393, 100), (170000, 50), (168156, 20), (174531, 20),
      (169655, 10), (169997, 10), (172430, 20)]),
    ("D13", "Cơm cải bó xôi xào tỏi, trứng", 2, "lunch;dinner", 15,
     [(168878, 450), (168462, 300), (169230, 15), (171287, 100), (171411, 15), (173468, 2)]),
    ("D14", "Cơm đậu phụ sốt cà chua", 2, "lunch;dinner", 20,
     [(168878, 450), (172475, 300), (170457, 250), (170005, 10), (174277, 15), (171411, 15)]),
    ("D15", "Cơm canh chua cá rô phi", 2, "dinner", 30,
     [(168878, 400), (175176, 250), (170457, 150), (169957, 100), (168156, 20), (174531, 20), (169655, 10),
      (169997, 10), (170106, 5)]),
    ("D16", "Cơm gà kho gừng", 2, "lunch;dinner", 35,
     [(168878, 450), (171077, 300), (169231, 20), (174531, 20), (169655, 10), (171411, 10), (170005, 10)]),
    ("D17", "Cơm thịt kho mặn (mẫu nhiều natri)", 2, "lunch;dinner", 45,
     [(168878, 400), (167902, 250), (174531, 60), (173468, 5), (169655, 15)]),
    ("D18", "Sữa chua trái tầm bóp (mẫu thiếu natri)", 2, "breakfast", 5,
     [(170886, 300), (173043, 150), (173944, 120)]),
    ("D19", "Cơm bạch tuộc xào sả ớt (mẫu thiếu natri)", 2, "lunch;dinner", 25,
     [(168878, 400), (168019, 300), (168573, 20), (170106, 5), (174531, 15), (171411, 10)]),
]

# fdc_id: (gam mỗi gói theo phần ăn được, giá VND mỗi gói). Giá mẫu, không phải giá thị trường.
DEMO_PRICES: dict[int, tuple[float, float]] = {
    170379: (500, 30000), 171287: (600, 35000), 174531: (500, 40000), 168878: (1000, 20000),
    171477: (500, 90000), 171077: (500, 70000), 167902: (500, 65000), 172475: (300, 12000),
    168462: (300, 15000), 169975: (1000, 15000), 170457: (500, 15000), 170393: (500, 12000),
    170000: (500, 15000), 169230: (200, 10000), 175176: (500, 45000), 175179: (500, 120000),
    174277: (500, 25000), 171411: (1000, 55000), 173468: (500, 6000), 169655: (1000, 25000),
    168914: (500, 15000), 174924: (300, 15000), 173944: (1000, 25000), 169251: (300, 30000),
    168409: (500, 12000), 169957: (300, 8000), 170005: (100, 5000), 170931: (50, 15000),
    169997: (100, 5000), 170390: (500, 15000), 168573: (200, 6000), 172430: (500, 40000),
    169705: (500, 45000), 171267: (1000, 35000), 170886: (400, 30000), 169231: (200, 8000),
    168156: (100, 8000), 175165: (500, 50000), 170106: (50, 3000), 173043: (250, 30000),
    168019: (500, 150000),
}

# Quy tắc giả lập "Calories-dinh dưỡng" – chỉ để kiểm thử luồng, KHÔNG phải khuyến nghị.
DEMO_RULE_SET = "DEMO-CALO-1"
DEMO_RULES = [
    ("sodium_mg", "<=", 2300, "mg", "day"),
    ("energy_kcal", ">=", 1200, "kcal", "day"),
    ("energy_kcal", "<=", 2800, "kcal", "day"),
]


def demo_health_policies():
    source = "Bộ dữ liệu thử nghiệm chức năng, không dùng làm hướng dẫn dinh dưỡng"
    members = [175179, 168019]
    return [
        {"policy_id": "DEMO-TYPE2", "version": 2, "intake_version": 3, "condition": "type2_diabetes", "source": source,
         "answers": {},
         "rules": [{"kind": "nutrient", "metric": "carb_g", "op": "<=", "value": 75, "unit": "g", "scope": "meal"}]},
        {"policy_id": "DEMO-GOUT", "version": 2, "intake_version": 3, "condition": "gout", "source": source, "answers": {},
         "rules": [{"kind": "food_group", "name": "tôm và bạch tuộc (nhóm mẫu)", "members": members,
                    "nonmembers": sorted(set(DEMO_INGREDIENTS) - set(members)), "max_dishes": 1, "scope": "period",
                    "classification_source": "Phân loại mẫu theo mã FDC trong danh mục demo"}]},
    ]


def recipes_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["recipe_id", "name_vi", "base_servings", "meal_types", "prep_minutes", "source_ref", "review_status", "version"])
    for rid, name, base, meals, prep, _ in DEMO_RECIPES:
        w.writerow([rid, name, base, meals, prep, DEMO_SOURCE, "demo", 1])
    return out.getvalue()


def recipe_ingredients_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["recipe_id", "line_no", "fdc_id", "quantity_g_edible", "food_state", "conversion_source"])
    for rid, *_rest, lines in DEMO_RECIPES:
        for i, (fdc, grams) in enumerate(lines, start=1):
            w.writerow([rid, i, fdc, grams, DEMO_INGREDIENTS[fdc][3], "nhập trực tiếp bằng gam (demo)"])
    return out.getvalue()


def ingredient_info_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["fdc_id", "name_vi", "aliases", "avoid_tags", "review_status", "source_ref", "version"])
    for fdc, (name, aliases, tags, _desc) in DEMO_INGREDIENTS.items():
        w.writerow([fdc, name, aliases, tags, "reviewed", DEMO_SOURCE, 1])
    return out.getvalue()


def prices_csv() -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["fdc_id", "package_g", "price", "currency", "mass_basis", "source_ref", "effective_date", "version"])
    for fdc, (pkg, price) in DEMO_PRICES.items():
        w.writerow([fdc, pkg, price, "VND", "edible", DEMO_SOURCE, "2026-11-01", 1])
    return out.getvalue()
