from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from web.auth import (
    SESSION_COOKIE_NAME,
    AuthGateMiddleware,
    create_auth_router,
    current_user,
    load_auth_settings,
)
from web.deps import get_db, get_settings, web_dir
from web.errors import register_error_handlers
from web.production_loop import get_production_loop_controller
from web.routers import (
    accounts,
    aliases,
    claims,
    client_sync,
    groups,
    mailcom,
    mails,
    production,
    production_loop,
    sync,
)
from web.schemas import HomePoolStatsOut


def create_app() -> FastAPI:
    settings = get_settings()
    auth_settings = load_auth_settings()
    base = web_dir()

    if not auth_settings.disabled and not auth_settings.session_secret:
        raise RuntimeError(
            "AUTH_SESSION_SECRET 未配置。请在 .env 中设置，或仅在本地测试时设 AUTH_DISABLED=true"
        )
    if not auth_settings.disabled and not auth_settings.password_hashes:
        raise RuntimeError(
            "未配置任何登录密码。请在 .env 设置 AUTH_PASSWORD_LWS / AUTH_PASSWORD_MHW"
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        controller = get_production_loop_controller()
        controller.startup()
        try:
            yield
        finally:
            controller.shutdown()

    app = FastAPI(
        title="2608B iCloud 邮箱池子",
        description="HME 隐私邮箱池 + 邮件收取与分类展示",
        version="26.9.9A",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    templates = Jinja2Templates(directory=str(base / "templates"))

    def render(request: Request, name: str, **extra: Any) -> HTMLResponse:
        ctx: dict[str, Any] = {
            "app_name": settings.app_name,
            "domain": settings.domain,
            "auth_user": current_user(request),
            "auth_disabled": auth_settings.disabled,
        }
        ctx.update(extra)
        return templates.TemplateResponse(request=request, name=name, context=ctx)

    register_error_handlers(app)
    for module in (
        accounts,
        aliases,
        claims,
        client_sync,
        mailcom,
        mails,
        production,
        production_loop,
        sync,
        groups,
    ):
        app.include_router(module.router)
    # 注册机兼容取码：/api/v1/code?token=...
    app.include_router(claims.code_router)
    app.include_router(
        create_auth_router(
            auth_settings=auth_settings,
            templates=templates,
            app_name=settings.app_name,
        )
    )

    @app.get("/", response_class=HTMLResponse)
    def home_page(request: Request) -> HTMLResponse:
        return render(request, "home.html")

    @app.get("/mailbox", response_class=HTMLResponse)
    def mailbox_page(request: Request) -> HTMLResponse:
        return render(request, "index.html")

    @app.get("/mailcom", response_class=HTMLResponse)
    def mailcom_page(request: Request) -> HTMLResponse:
        return render(request, "mailcom.html")

    @app.get("/claims", response_class=HTMLResponse)
    def claims_page(request: Request) -> HTMLResponse:
        return render(request, "claims.html")

    @app.get("/production", response_class=HTMLResponse)
    def production_page(request: Request) -> HTMLResponse:
        return render(request, "production.html")

    @app.get("/production-loop", response_class=HTMLResponse)
    def production_loop_page(request: Request) -> HTMLResponse:
        return render(request, "production_loop.html")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "app": settings.app_name}

    @app.get("/api/home/stats", response_model=HomePoolStatsOut)
    def home_stats() -> HomePoolStatsOut:
        return HomePoolStatsOut(**get_db().get_alias_pool_stats())

    # 后添加的中间件先执行：先 Gate 再 Session，使 Gate 能读到 session
    app.add_middleware(AuthGateMiddleware, auth_settings=auth_settings)
    # Session cookie 固定 HttpOnly；Secure 由 AUTH_COOKIE_SECURE 控制
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth_settings.session_secret or "auth-disabled-placeholder",
        session_cookie=SESSION_COOKIE_NAME,
        max_age=auth_settings.session_max_age,
        same_site="lax",
        https_only=auth_settings.cookie_secure,
    )

    return app


app = create_app()
