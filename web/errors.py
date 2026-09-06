from __future__ import annotations

"""领域异常 → HTTP 状态码映射。"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from tools.client import CookieInvalidError, ICloudError
from tools.icloud_plan import ICloudFreePlanError
from tools.rate_limit import HMEAccountAliasLimitError, HMECreateRateLimitError


def _payload(code: str, message: str) -> dict[str, dict[str, str]]:
    return {"error": {"code": code, "message": message}}


def _server_error(exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content=_payload("internal_error", f"{type(exc).__name__}: {exc}"),
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ICloudFreePlanError)
    async def _free_plan(_: Request, exc: ICloudFreePlanError) -> JSONResponse:
        return JSONResponse(status_code=409, content=_payload(exc.code, str(exc)))

    @app.exception_handler(CookieInvalidError)
    async def _cookie_invalid(_: Request, exc: CookieInvalidError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_payload("COOKIE_INVALID", str(exc)),
        )

    @app.exception_handler(HMECreateRateLimitError)
    async def _rate_limit(_: Request, exc: HMECreateRateLimitError) -> JSONResponse:
        retry = max(1, int(getattr(exc, "retry_after_sec", 0) or 1))
        return JSONResponse(
            status_code=429,
            content=_payload("rate_limited", str(exc)),
            headers={"Retry-After": str(retry)},
        )

    @app.exception_handler(HMEAccountAliasLimitError)
    async def _account_limit(_: Request, exc: HMEAccountAliasLimitError) -> JSONResponse:
        return JSONResponse(
            status_code=409,
            content=_payload(exc.code, str(exc)),
        )

    @app.exception_handler(ICloudError)
    async def _icloud(_: Request, exc: ICloudError) -> JSONResponse:
        msg = str(exc)
        # 421 / cookie 失效需要用户去跑 CLI 的 cookie-login，前端要能区分出来
        code = "COOKIE_INVALID" if ("421" in msg or "Cookie" in msg or "COOKIE_INVALID" in msg) else "upstream_error"
        return JSONResponse(status_code=502, content=_payload(code, msg))

    @app.exception_handler(LookupError)
    async def _not_found(_: Request, exc: LookupError) -> JSONResponse:
        # KeyError / IndexError 也是 LookupError，但那是代码缺陷而非资源不存在，
        # 映射成 404 会掩盖 bug。
        if isinstance(exc, (KeyError, IndexError)):
            return _server_error(exc)
        return JSONResponse(status_code=404, content=_payload("not_found", str(exc)))

    @app.exception_handler(ValueError)
    async def _bad_request(_: Request, exc: ValueError) -> JSONResponse:
        # pydantic 的 ValidationError 继承 ValueError；出现在响应侧说明是服务端模型
        # 与数据不匹配，属于 500 而不是客户端请求错误。
        if isinstance(exc, ValidationError):
            return _server_error(exc)
        return JSONResponse(status_code=400, content=_payload("bad_request", str(exc)))

    @app.exception_handler(RuntimeError)
    async def _upstream(_: Request, exc: RuntimeError) -> JSONResponse:
        # IMAP/SMTP 连接与登录失败大多是 RuntimeError
        return JSONResponse(
            status_code=502, content=_payload("mail_server_error", str(exc))
        )
