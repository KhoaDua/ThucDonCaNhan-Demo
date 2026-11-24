"""Điểm chạy bản công khai. Khách vào bằng link, phiên được cấp tự động."""
from functools import lru_cache
from hashlib import sha256
import os
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse

from app.main import app
from public_storage import (
    MAX_WORKSPACE_BYTES,
    SESSION_SECONDS,
    SessionStore,
    StorageUnavailable,
    guest_database,
    initialize_workspace,
)


os.environ.update(
    APP_DATA_MODE="demo", AI_PROVIDER="mock", PLANNER_SOLVER="builtin",
    CHECKLIST_ENERGY_RULE_SET="", CHECKLIST_SODIUM_RULE_SET="",
)
SECURE = bool(os.environ.get("VERCEL"))
COOKIE = "__Host-ktht_guest" if SECURE else "ktht_guest"


@lru_cache(maxsize=1)
def session_store():
    return SessionStore()


def message(text, status):
    return PlainTextResponse(text, status_code=status, headers={"Cache-Control": "no-store"})


@app.get("/healthz")
def health():
    try:
        session_store()
    except StorageUnavailable:
        return JSONResponse({"status": "storage_unavailable"}, status_code=503)
    return {"status": "ok"}


@app.middleware("http")
async def guest_session(request: Request, call_next):
    path = request.url.path
    if path in ("/healthz", "/admin") or path.startswith(("/static/", "/admin/")):
        return await call_next(request)
    is_write = request.method not in ("GET", "HEAD", "OPTIONS")
    if is_write:
        expected = ("https" if SECURE else request.url.scheme) + "://" + request.url.netloc
        origin = request.headers.get("origin")
        if (origin and origin != expected) or request.headers.get("sec-fetch-site") == "cross-site":
            return message("Hãy mở website trực tiếp trước khi gửi biểu mẫu.", 403)

    token = request.cookies.get(COOKIE, "")
    new_session = not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    if new_session and is_write:
        return message("Hãy bật cookie rồi mở lại trang trước khi gửi biểu mẫu.", 409)
    if new_session:
        token = secrets.token_urlsafe(32)
    guest_key = sha256(token.encode("ascii")).hexdigest()

    try:
        store = await run_in_threadpool(session_store)
        revision, snapshot = (0, None) if new_session else await run_in_threadpool(store.load, guest_key)
        with TemporaryDirectory(prefix="ktht-workspace-") as directory:
            path = Path(directory) / "guest.db"
            await run_in_threadpool(initialize_workspace, path, snapshot)
            context = guest_database.set(path)
            try:
                response = await call_next(request)
            finally:
                guest_database.reset(context)

            saved = is_write and response.status_code < 400
            if saved:
                if path.stat().st_size > MAX_WORKSPACE_BYTES:
                    return message("Kho thử trên trình duyệt đã đầy. Hãy bắt đầu phiên thử mới.", 413)
                if not await run_in_threadpool(store.save, guest_key, revision, path.read_bytes()):
                    return message("Một thao tác khác vừa lưu thay đổi. Hãy tải lại trang rồi thử lại.", 409)
    except StorageUnavailable:
        return message("Website chưa kết nối được kho lưu thực đơn. Hãy thử lại sau.", 503)

    if (new_session or saved) and response.status_code < 400:
        response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True,
                            secure=SECURE, samesite="lax", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response
