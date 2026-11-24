"""Môi trường Jinja2 dùng chung cho web và kiểm thử (render không cần FastAPI)."""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.catalog.lookup import MEAL_LABELS, METRICS
from app.planner.constraints import OP_TEXT, fmt
from app.planner.profile import CATEGORIES, SCOPES
from app.planner.service import STATUS_TEXT
from app.web.display import display_html

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _num(v, digits=1):
    if v is None:
        return "chưa đủ dữ liệu"
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{v:,.{digits}f}"
    if digits:
        s = s.rstrip("0").rstrip(".")
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def _money(v):
    return "chưa tính được" if v is None else _num(round(float(v)), 0) + " đ"


env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]))
env.filters.update(num=_num, money=_money, fmt=fmt,
                   meal=lambda m: MEAL_LABELS.get(m, m),
                   metric=lambda m: METRICS[m][2] if m in METRICS else m,
                   unit=lambda m: METRICS[m][1] if m in METRICS else "",
                   op=lambda o: OP_TEXT.get(o, o),
                   status_text=lambda s: STATUS_TEXT.get(s, s))
env.globals.update(METRICS=METRICS, MEAL_LABELS=MEAL_LABELS, SCOPES=SCOPES, CATEGORIES=CATEGORIES, STATUS_TEXT=STATUS_TEXT)


def render(template: str, **ctx) -> str:
    return display_html(env.get_template(template).render(**ctx))
