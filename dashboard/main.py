"""Dashboard for output/osd_error.txt.

Detection files stay read-only.  Human confirmations and the daily report
state are stored in a separate SQLite database, so this service never changes
the osd20 container or any of its files.
"""
from __future__ import annotations

import base64
import html
import mimetypes
import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import data

STATIC_DIR = Path(__file__).resolve().parent / "static"
mimetypes.add_type("font/woff2", ".woff2")
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "osd")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")
SESSION_COOKIE = "osd_dashboard_session"
SESSION_TTL = max(300, int(os.getenv("DASHBOARD_SESSION_HOURS", "12")) * 3600)

LOGIN_TEMPLATE = (STATIC_DIR / "login.html").read_text(encoding="utf-8")
_sessions: dict[str, float] = {}
_session_lock = threading.Lock()

app = FastAPI(
    title="OSD 检测结果看板（Demo）",
    version="0.4.0",
    description="检测数据只读展示；人工确认结果和日报状态单独存储。",
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _request_is_https(request: Request) -> bool:
    proto = request.headers.get("x-forwarded-proto", "")
    if proto:
        return proto.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def _basic_valid(request: Request) -> bool:
    """Allow curl/scripts to use the old Basic auth credentials as well."""
    if not DASHBOARD_PASSWORD:
        return False
    expected = "Basic " + base64.b64encode(
        f"{DASHBOARD_USER}:{DASHBOARD_PASSWORD}".encode("utf-8")
    ).decode("ascii")
    supplied = request.headers.get("authorization", "")
    return secrets.compare_digest(supplied, expected)


def _session_valid(token: str | None) -> bool:
    if not token:
        return False
    now = time.time()
    with _session_lock:
        expires = _sessions.get(token)
        if expires is None:
            return False
        if expires <= now:
            _sessions.pop(token, None)
            return False
        return True


def _authorized(request: Request) -> bool:
    return (_basic_valid(request)
            or _session_valid(request.cookies.get(SESSION_COOKIE)))


def _login_html(error: str = "") -> str:
    block = ""
    if error:
        block = f'<p class="error" role="alert">{html.escape(error)}</p>'
    return LOGIN_TEMPLATE.replace("{{ERROR}}", block)


class ConfirmationRequest(BaseModel):
    row_key: str
    result: str | None = None


def _same_origin(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = (request.headers.get("x-forwarded-host")
            or request.headers.get("host", ""))
    try:
        origin_host = origin.split("://", 1)[1].split("/", 1)[0]
    except IndexError:
        return False
    return origin_host.casefold() == host.casefold()


def _write_context(request: Request) -> tuple[str, str]:
    if DASHBOARD_PASSWORD:
        return "public", DASHBOARD_USER
    return "internal", "internal"


@app.middleware("http")
async def dashboard_middleware(request: Request, call_next):
    path = request.url.path

    # The healthcheck and the login page itself stay public.  Everything else
    # is behind a session cookie when a password is configured (the public
    # dashboard instance).  The internal instance runs without a password.
    if DASHBOARD_PASSWORD and path not in ("/health", "/login"):
        if not _authorized(request):
            if path.startswith("/api/"):
                return JSONResponse({"detail": "需要登录"}, status_code=401)
            return RedirectResponse("/login", status_code=303)

    response = await call_next(request)
    if (path.startswith("/api/") or path.startswith("/static/")
            or path in ("/login", "/logout")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/health", summary="Liveness probe")
def health() -> dict:
    return {
        "status": "ok",
        "time": data.now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_page(request: Request):
    if not DASHBOARD_PASSWORD or _authorized(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_login_html())


@app.post("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_submit(request: Request):
    if not DASHBOARD_PASSWORD:
        return RedirectResponse("/", status_code=303)

    body = (await request.body()).decode("utf-8", "replace")
    form = parse_qs(body)
    username = form.get("username", [""])[0]
    password = form.get("password", [""])[0]

    if (secrets.compare_digest(username, DASHBOARD_USER)
            and secrets.compare_digest(password, DASHBOARD_PASSWORD)):
        token = secrets.token_urlsafe(32)
        with _session_lock:
            _sessions[token] = time.time() + SESSION_TTL
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=SESSION_TTL,
            httponly=True,
            samesite="lax",
            secure=_request_is_https(request),
            path="/",
        )
        return response

    return HTMLResponse(_login_html("用户名或密码错误"), status_code=401)


@app.get("/logout", include_in_schema=False)
def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        with _session_lock:
            _sessions.pop(token, None)
    response = RedirectResponse(
        "/login" if DASHBOARD_PASSWORD else "/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@app.get("/api/dashboard/dates", summary="Dates that have anomaly records")
def dates() -> dict:
    return data.get_dates()


@app.get("/api/dashboard/results", summary="Paginated anomalies grouped by round")
def results(
    date: str | None = Query(
        None, description="YYYY-MM-DD, defaults to today (Beijing time)"),
    page: int = Query(1, ge=1),
    page_size: int = Query(
        data.DEFAULT_PAGE_SIZE, ge=1, le=data.MAX_PAGE_SIZE,
        description="rows per page"),
    channel_name: str | None = Query(
        None, description="channel name substring; today's results only"),
    channel_code: str | None = Query(
        None, include_in_schema=False, deprecated=True,
        description="deprecated alias for channel_name"),
    order: str = Query("desc", pattern="^(asc|desc)$"),
) -> dict:
    chosen = date or data.today_str()
    try:
        chosen = datetime.strptime(chosen, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="日期格式应为 YYYY-MM-DD") from exc

    try:
        return data.get_results(
            date=chosen,
            page=page,
            page_size=page_size,
            channel_name=channel_name or channel_code or "",
            order=order,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _valid_date(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="日期格式应为 YYYY-MM-DD") from exc


@app.get("/api/dashboard/report", summary="Completed-day stream report")
def daily_report(date: str = Query(..., description="YYYY-MM-DD")) -> dict:
    return data.build_daily_report(_valid_date(date))


@app.get("/api/dashboard/report.txt", summary="Download completed-day report")
def daily_report_txt(date: str = Query(..., description="YYYY-MM-DD")) -> Response:
    chosen = _valid_date(date)
    report = data.build_daily_report(chosen)
    if not report["available"]:
        raise HTTPException(status_code=409, detail=report["reason"])
    return Response(
        content=report["text"],
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="osd_report_{chosen}.txt"',
        },
    )


@app.get("/api/dashboard/round.csv", summary="Download one round as CSV")
def round_csv(
    date: str = Query(..., description="YYYY-MM-DD"),
    round_key: str = Query(..., min_length=1),
) -> Response:
    chosen = _valid_date(date)
    try:
        result = data.round_csv(chosen, round_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return Response(
        content=result["content"],
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{result["filename"]}"',
        },
    )


@app.post("/api/dashboard/confirmations",
          summary="Confirm or reject today's detected row")
def save_confirmation(payload: ConfirmationRequest, request: Request) -> dict:
    if not _same_origin(request):
        raise HTTPException(status_code=403, detail="跨站请求被拒绝")
    source, actor = _write_context(request)
    try:
        return data.save_confirmation(
            payload.row_key,
            payload.result,
            source=source,
            actor=actor,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/dashboard/frame", summary="Captured frame for one channel")
def frame(
    index: int = Query(..., ge=0, description="channel index"),
    channel_code: str = Query(..., description="channel code"),
    frame_dir: str | None = Query(
        None, description="per-round folder, e.g. round_0004_20261022_125603"),
) -> FileResponse:
    path = data.frame_path(index, channel_code, frame_dir)
    if path is None:
        raise HTTPException(status_code=404, detail="未找到该通道的抓帧图片")
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
