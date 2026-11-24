"""Luồng công khai từ checklist đến thực đơn cho một lượt dùng thử."""
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.web import views
from app.web.render import render
from public_storage import demo_workspace


app = FastAPI(title="Thực đơn dùng thử", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
settings = get_settings(
    db_path=Path(":memory:"), data_mode="demo", ai_provider="mock", solver="builtin",
    checklist_energy_rule_set="", checklist_sodium_rule_set="",
)


def _respond(page, **extra):
    return HTMLResponse(
        render(page.template, **page.ctx, **extra),
        status_code=page.status_code,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/healthz")
def health():
    with demo_workspace() as conn:
        conn.execute("SELECT 1 FROM catalog.recipe LIMIT 1").fetchone()
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
@app.get("/profile", response_class=HTMLResponse)
def profile():
    with demo_workspace() as conn:
        return _respond(views.profile_form(conn, settings))


def _submit(form, action):
    # Tạo lại nháp từ biểu mẫu và kiểm tra các điều kiện trên từng yêu cầu.
    form.pop("base_version", None)
    form["action"] = ["checklist"]
    form["profile_mode"] = ["checklist"]
    with demo_workspace() as conn:
        result = views.profile_submit(conn, settings, form)
        if isinstance(result, views.Page):
            return _respond(result)

        version = int(parse_qs(urlsplit(result.url).query)["v"][0])
        if action == "edit":
            page = views.profile_form(conn, settings, version)
            page.ctx["form"]["base_version"] = ""
            return _respond(page)

        review = views.profile_confirm_page(conn, settings, version)
        if action == "review" or review.ctx["health_review"]["state"] != "ready":
            return _respond(review, demo_form=form)

        result = views.profile_confirm_submit(conn, settings, version, review.ctx["token"])
        if isinstance(result, views.Page):
            return _respond(result, demo_form=form)

        route, _, record_id = urlsplit(result.url).path.rpartition("/")
        if route == "/runs":
            return _respond(views.run_page(conn, settings, int(record_id)), demo_form=form)
        if route != "/plans":
            return _respond(review, demo_form=form)

        plan_id = int(record_id)
        first_day = views.plan_page(conn, settings, plan_id)
        days = [first_day.ctx]
        days.extend(views.plan_page(conn, settings, plan_id, day=day).ctx
                    for day in first_day.ctx["days"][1:])
        shopping = views.plan_shopping(conn, settings, plan_id)
        page = views.Page("demo_result.html", {
            **first_day.ctx, "plan_days": days, "shop": shopping.ctx["shop"],
        })
        return _respond(page)


async def _submit_form(request, action):
    scheme = "https" if os.environ.get("VERCEL") else request.url.scheme
    expected_origin = f"{scheme}://{request.url.netloc}"
    origin = request.headers.get("origin")
    if (origin and origin != expected_origin) or request.headers.get("sec-fetch-site") == "cross-site":
        return PlainTextResponse("Hãy mở trang dùng thử trước khi gửi biểu mẫu.", status_code=403)
    async with request.form() as data:
        form = {key: [value for value in data.getlist(key) if isinstance(value, str)]
                for key in data}
    return await run_in_threadpool(_submit, form, action)


@app.post("/profile")
async def review_profile(request: Request):
    return await _submit_form(request, "review")


@app.post("/profile/edit")
async def edit_profile(request: Request):
    return await _submit_form(request, "edit")


@app.post("/profile/confirm")
async def confirm_profile(request: Request):
    return await _submit_form(request, "generate")
