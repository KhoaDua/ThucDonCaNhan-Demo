"""Điểm khởi động web (FastAPI). Lớp này chỉ chuyển HTTP ↔ app.web.views.

Chạy:  python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.web import views
from app.web.render import render
from public_storage import connect_guest, current_guest_database

app = FastAPI(title="Hệ thống hoạch định thực đơn cá nhân", docs_url=None,
              redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


def _ctx():
    s = get_settings(db_path=current_guest_database())
    return connect_guest(s.db_path), s


def _respond(result):
    if isinstance(result, views.Redirect):
        return RedirectResponse(result.url, status_code=303)
    return HTMLResponse(render(result.template, **result.ctx), status_code=result.status_code)


async def _form(request: Request) -> dict:
    fd = await request.form()
    return {k: fd.getlist(k) for k in fd.keys()}


def _run(fn, *args, **kw):
    conn, s = _ctx()
    try:
        return _respond(fn(conn, s, *args, **kw))
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def home():
    return _run(views.home)


@app.get("/profile", response_class=HTMLResponse)
def profile(v: int | None = None, advanced: bool = False):
    return _run(views.profile_form, v, advanced)


@app.post("/profile")
async def profile_post(request: Request):
    form = await _form(request)
    if form.get("action", [""])[0] == "interpret":
        return _run(views.profile_interpret, form)
    return _run(views.profile_submit, form)


@app.get("/profile/confirm", response_class=HTMLResponse)
def profile_confirm(v: int, ok: int = 0):
    return _run(views.profile_confirm_page, v, "Đã xác nhận hồ sơ. Bạn có thể lập thực đơn." if ok else None)


@app.post("/profile/confirm")
def profile_confirm_post(v: int = Form(...), request_token: str = Form("")):
    return _run(views.profile_confirm_submit, v, request_token)


@app.post("/plans")
async def plans_post(request: Request):
    return _run(views.plan_generate, await _form(request))


@app.get("/plans", response_class=HTMLResponse)
def plans():
    return _run(views.plans_page)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run(run_id: int):
    return _run(views.run_page, run_id)


@app.get("/plans/{plan_id}", response_class=HTMLResponse)
def plan(plan_id: int, day: int | None = None, version: int | None = None, change: str | None = None):
    return _run(views.plan_page, plan_id, day, version, change)


@app.get("/plans/{plan_id}/trace", response_class=HTMLResponse)
def trace(plan_id: int, day: int, metric: str, version: int | None = None):
    return _run(views.plan_trace, plan_id, day, metric, version)


@app.get("/plans/{plan_id}/shopping", response_class=HTMLResponse)
def shopping(plan_id: int, version: int | None = None):
    return _run(views.plan_shopping, plan_id, version)


@app.post("/plans/{plan_id}/swap")
async def swap(plan_id: int, request: Request):
    return _run(views.plan_swap, plan_id, await _form(request))


@app.get('/plans/{plan_id}/swap-options')
def swap_options(plan_id: int, base_version: int, day: int, meal: str, pos: int):
    conn, settings = _ctx()
    try:
        result = views.plan_swap_options(conn, settings, plan_id, base_version, day, meal, pos)
        return JSONResponse(result, status_code=200 if result['status']=='ok' else 409,
                            headers={'Cache-Control':'no-store'})
    finally:
        conn.close()


@app.post("/feedback")
async def feedback(request: Request):
    return _run(views.feedback_submit, await _form(request))
