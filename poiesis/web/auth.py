"""Single-user username/password auth via signed session cookies."""

from __future__ import annotations

import logging

import bcrypt
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import Response

from poiesis.config import PoiesisEnv

logger = logging.getLogger(__name__)

# Paths reachable without a session.
PUBLIC_PREFIXES = ("/login", "/logout", "/healthz", "/static", "/favicon")

router = APIRouter()


def verify_password(env: PoiesisEnv, username: str, password: str) -> bool:
    if username != env.admin_username:
        return False
    if env.admin_password_hash:
        try:
            return bcrypt.checkpw(password.encode(), env.admin_password_hash.encode())
        except ValueError:
            return False
    if env.admin_password is not None:  # dev convenience: plaintext compare
        return password == env.admin_password
    return False


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


class AuthMiddleware(BaseHTTPMiddleware):
    """Redirect unauthenticated browser requests to /login (allowlisting public paths)."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path.startswith(PUBLIC_PREFIXES) or request.session.get("user"):
            return await call_next(request)
        return RedirectResponse(url="/login", status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    env: PoiesisEnv = request.app.state.env
    if verify_password(env, username, password):
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": "Invalid username or password."},
        status_code=401,
    )


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)
