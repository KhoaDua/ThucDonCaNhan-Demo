"""Giao diện kết nối AI chung + các nhà cung cấp.

Hai chức năng: (1) diễn giải câu nhập tự do thành bản nháp có cấu trúc theo checklist,
(2) tra cứu danh mục qua công cụ Python được phép gọi (xem tools.py).

- `mock`: bộ diễn giải theo luật, dùng cho kiểm thử/demo – KHÔNG phải AI thật.
- `failing`: luôn lỗi, dùng để kiểm thử luồng dự phòng bằng checklist.
- `openai_compatible`: gọi API dạng /chat/completions có tool calling (OpenAI, Azure OpenAI tương thích,
  OpenRouter, LM Studio, Ollama…). Khóa đọc từ biến môi trường AI_API_KEY, không ghi vào log.
  CHƯA được kiểm chứng với nhà cung cấp thật – chỉ ghi "đã tích hợp AI thật" sau khi nhóm thử đủ luồng.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from app.ai.tools import TOOL_SPECS, ToolRegistry


class AIUnavailable(Exception):
    pass


@dataclass
class InterpretResult:
    draft: dict = field(default_factory=dict)  # các trường hồ sơ AI đề xuất
    ambiguous: list = field(default_factory=list)  # [{field, question}]
    tool_calls: list = field(default_factory=list)
    provider: str = ""
    raw_note: str = ""


ALLOWED_FIELDS = {"days", "meals", "dishes_per_meal", "serving_levels", "nutrient_limits", "avoid", "likes", "dislikes",
                  "prefer_variety", "max_prep_minutes", "prep_hard", "budget", "inventory"}


def _num(s: str) -> float:
    from app.planner.profile import parse_number
    return parse_number(s)


class MockAI:
    name = "mock"

    def interpret(self, text: str, tools: ToolRegistry) -> InterpretResult:
        t = " " + (text or "").lower().strip() + " "
        d: dict = {}
        amb: list = []
        if not t.strip():
            raise AIUnavailable("Câu nhập trống")
        m = re.search(r"(\d+)\s*ngày", t)
        if m:
            d["days"] = int(m.group(1))
        elif re.search(r"(một|1)\s*tuần", t):
            d["days"] = 7
        meals = [k for k, w in (("breakfast", "sáng"), ("lunch", "trưa"), ("dinner", "tối")) if re.search(rf"(bữa|buổi)?\s*{w}", t)]
        mm = re.search(r"(\d)\s*bữa", t)
        if meals:
            d["meals"] = meals
        elif mm and mm.group(1) == "3":
            d["meals"] = ["breakfast", "lunch", "dinner"]
        elif mm:
            amb.append({"field": "meals", "question": f"Bạn muốn {mm.group(1)} bữa nào (sáng, trưa, tối)?"})
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:khẩu phần|suất|phần ăn)", t)
        if m:
            d["serving_levels"] = [_num(m.group(1))]
        m = re.search(r"(\d)\s*món\s*(?:mỗi|một)\s*bữa", t)
        if m:
            d["dishes_per_meal"] = int(m.group(1))
        lims = []
        m = re.search(r"natri\s*(dưới|tối đa|không quá|<=|≤|<|ít hơn)\s*([\d.,]+)\s*(mg|g)?", t)
        if m:
            op = "<" if m.group(1) in ("<", "ít hơn") else "<="
            lims.append({"metric": "sodium_mg", "op": op, "value": _num(m.group(2)), "unit": m.group(3) or "mg", "scope": "day",
                         "category": "health_limit", "source": "", "confirmed": False})
            amb.append({"field": "limit_0", "question": "Xác nhận giới hạn natri theo ngày và ghi nguồn (ví dụ: bác sĩ, quy tắc đã duyệt)."})
        elif re.search(r"ít muối|ăn nhạt|giảm muối|ít mặn", t):
            amb.append({"field": "limit_0", "question": "“Ít muối” là tối đa bao nhiêu mg natri mỗi ngày? Hãy nhập giá trị và nguồn."})
        m = re.search(r"(dưới|tối đa|không quá|trên|ít nhất|tối thiểu|khoảng)\s*([\d.,]+)\s*(kcal|calo|calories)", t)
        if m:
            word = m.group(1)
            if word == "khoảng":
                amb.append({"field": f"limit_{len(lims)}", "question": "“Khoảng” bao nhiêu kcal: là mức tối đa hay tối thiểu mỗi ngày?"})
                lims.append({"metric": "energy_kcal", "op": "<=", "value": _num(m.group(2)), "unit": "kcal", "scope": "day",
                             "category": "personal_goal", "source": "", "confirmed": False})
            else:
                op = ">=" if word in ("trên", "ít nhất", "tối thiểu") else "<="
                lims.append({"metric": "energy_kcal", "op": op, "value": _num(m.group(2)), "unit": "kcal", "scope": "day",
                             "category": "personal_goal", "source": "", "confirmed": False})
                amb.append({"field": f"limit_{len(lims) - 1}", "question": "Xác nhận mục tiêu năng lượng và ghi nguồn."})
        if lims:
            for i, l in enumerate(lims):
                l["row"] = i
            d["nutrient_limits"] = lims
        avoid = []
        for m in re.finditer(r"(?:không ăn được|không ăn|tránh|dị ứng(?: với)?|kiêng)\s+([^,.;!\n]+)", t):
            for part in re.split(r"\s+và\s+|,|/", m.group(1)):
                part = part.strip()
                if part and len(part) <= 30:
                    avoid.append(part)
        if avoid:
            d["avoid"] = list(dict.fromkeys(avoid))
        likes = []
        for m in re.finditer(r"(?<!không )thích\s+(?:ăn\s+)?([^,.;!\n]+)", t):
            likes += [x.strip() for x in re.split(r"\s+và\s+|,", m.group(1)) if x.strip()]
        if likes:
            d["likes"] = likes
        m = re.search(r"ngân sách\s*(khoảng|tối đa|dưới|không quá)?\s*([\d.,]+\s*(?:k|nghìn|ngàn|triệu|tr)?)", t)
        if m:
            amount = _num(m.group(2).replace(" ", ""))
            scope = "day" if re.search(r"(mỗi|/|một)\s*ngày", t[m.end():m.end() + 15]) else "period"
            mode = "purchase" if "tiền mua" in t or "mua gói" in t else ("usage" if "lượng dùng" in t else "")
            d["budget"] = {"amount": amount, "currency": "VND", "scope": scope, "mode": mode,
                           "hard": m.group(1) in ("tối đa", "dưới", "không quá"), "drop_cost_if_missing": False}
            if not mode:
                amb.append({"field": "budget_mode", "question": "Ngân sách tính theo lượng sử dụng hay theo tiền mua cả gói?"})
            if not m.group(1):
                amb.append({"field": "budget_amount", "question": "Ngân sách là giới hạn bắt buộc hay chỉ là ưu tiên?"})
        m = re.search(r"(\d+)\s*phút", t)
        if m:
            d["max_prep_minutes"] = float(m.group(1))
        elif re.search(r"nấu nhanh|nhanh gọn|ít thời gian", t):
            amb.append({"field": "max_prep_minutes", "question": "“Nấu nhanh” là tối đa bao nhiêu phút mỗi món?"})
        if re.search(r"đa dạng|ít lặp|không lặp", t):
            d["prefer_variety"] = True
        # minh họa tra cứu: nếu câu nhắc mã món (D01, R12…) hoặc "món <tên>", gọi công cụ đọc công thức
        for code in re.findall(r"\b([a-z]\d{2,4})\b", t):
            tools.call("get_recipe", {"recipe_id": code.upper()})
        for fdc in re.findall(r"fdc\s*(\d{4,8})", t):
            tools.call("get_food_nutrients", {"fdc_id": int(fdc)})
        if not d and not amb:
            amb.append({"field": "free_text", "question": "Chưa nhận ra thông tin nào – hãy dùng checklist bên dưới."})
        return InterpretResult(draft=d, ambiguous=amb, tool_calls=list(tools.calls), provider=self.name,
                               raw_note="Bộ diễn giải theo luật (mock) – không phải AI thật")


class FailingAI:
    name = "failing"

    def interpret(self, text, tools):
        raise AIUnavailable("Dịch vụ AI giả lập lỗi (AI_PROVIDER=failing)")


SYSTEM_PROMPT = """Bạn chuyển câu mô tả nhu cầu lập thực đơn (tiếng Việt) thành JSON theo checklist.
Chỉ trả JSON: {"draft": {...}, "ambiguous": [{"field": "...", "question": "..."}]}.
Trường hợp lệ của draft: days (int), meals (list trong breakfast/lunch/dinner), dishes_per_meal (int),
serving_levels (list số), nutrient_limits (list {metric trong energy_kcal/protein_g/fat_g/carb_g/fiber_g/sodium_mg/sugar_g,
op trong <=,<,>=,>, value, unit, scope trong meal/day/period, category health_limit|personal_goal}),
avoid (list chuỗi), likes, dislikes, prefer_variety (bool), max_prep_minutes (số), prep_hard (bool),
budget ({amount, scope period|day, mode usage|purchase, hard bool}), inventory (list {fdc_id, grams}).
KHÔNG tự đặt giới hạn dinh dưỡng điều trị; chỉ ghi lại giá trị người dùng nói, để confirmed=false.
Điều gì mơ hồ (ví dụ "nấu nhanh" không có số phút) thì đưa vào ambiguous thay vì đoán.
Có thể gọi công cụ get_recipe, get_food_nutrients, search_recipes để tra cứu danh mục."""


class OpenAICompatible:
    name = "openai_compatible"

    def __init__(self, base_url: str, model: str, api_key: str, timeout_s: float = 20.0):
        if not base_url or not model:
            raise AIUnavailable("Chưa cấu hình AI_BASE_URL/AI_MODEL")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._key = api_key
        self.timeout_s = timeout_s

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {self._key}"} if self._key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
            raise AIUnavailable(f"Không gọi được dịch vụ AI: {type(e).__name__}")

    def interpret(self, text: str, tools: ToolRegistry) -> InterpretResult:
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": text}]
        for _ in range(4):
            out = self._post({"model": self.model, "messages": msgs, "tools": TOOL_SPECS, "temperature": 0})
            try:
                msg = out["choices"][0]["message"]
            except (KeyError, IndexError, TypeError):
                raise AIUnavailable("Phản hồi AI sai cấu trúc")
            calls = msg.get("tool_calls") or []
            if not calls:
                return self._parse(msg.get("content") or "", tools)
            msgs.append(msg)
            for c in calls[:5]:
                try:
                    args = json.loads(c["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = tools.call(c["function"]["name"], args if isinstance(args, dict) else {})
                msgs.append({"role": "tool", "tool_call_id": c.get("id"), "content": json.dumps(result, ensure_ascii=False)})
        raise AIUnavailable("AI gọi công cụ quá nhiều lần")

    def _parse(self, content: str, tools: ToolRegistry) -> InterpretResult:
        m = re.search(r"\{.*\}", content, flags=re.S)
        if not m:
            raise AIUnavailable("AI không trả JSON")
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            raise AIUnavailable("JSON của AI không hợp lệ")
        draft = {k: v for k, v in (obj.get("draft") or {}).items() if k in ALLOWED_FIELDS}
        for lim in draft.get("nutrient_limits") or []:
            if isinstance(lim, dict):
                lim["confirmed"] = False  # luôn cần người dùng xác nhận
                lim.setdefault("source", "")
        amb = [a for a in (obj.get("ambiguous") or []) if isinstance(a, dict) and "field" in a]
        return InterpretResult(draft=draft, ambiguous=amb, tool_calls=list(tools.calls), provider=self.name)


def get_provider(settings):
    p = settings.ai_provider
    if p == "mock":
        return MockAI()
    if p == "failing":
        return FailingAI()
    if p == "openai_compatible":
        return OpenAICompatible(settings.ai_base_url, settings.ai_model, settings.ai_api_key, settings.ai_timeout_s)
    return None
