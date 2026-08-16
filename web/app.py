from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web.deps import get_settings, web_dir
from web.errors import register_error_handlers
from web.production_loop import get_production_loop_controller
from web.routers import accounts, aliases, client_sync, groups, mails, production, production_loop, sync


def create_app() -> FastAPI:
    settings = get_settings()
    base = web_dir()

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
        version="26.8.15",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    app.mount("/static", StaticFiles(directory=base / "static"), name="static")
    templates = Jinja2Templates(directory=str(base / "templates"))

    register_error_handlers(app)
    for module in (accounts, aliases, client_sync, mails, production, production_loop, sync, groups):
        app.include_router(module.router)

    @app.get("/", response_class=HTMLResponse)
    def home_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="home.html",
            context={"app_name": settings.app_name, "domain": settings.domain},
        )

    @app.get("/mailbox", response_class=HTMLResponse)
    def mailbox_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"app_name": settings.app_name, "domain": settings.domain},
        )

    @app.get("/production", response_class=HTMLResponse)
    def production_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="production.html",
            context={"app_name": settings.app_name, "domain": settings.domain},
        )

    @app.get("/production-loop", response_class=HTMLResponse)
    def production_loop_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="production_loop.html",
            context={"app_name": settings.app_name, "domain": settings.domain},
        )

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {"ok": True, "app": settings.app_name}

    return app


app = create_app()
