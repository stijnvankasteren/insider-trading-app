from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import router as api_router
from app.db import init_db
from app.security import RateLimitExceeded, rate_limit_dependency
from app.settings import get_settings
from app.web import router as web_router


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db()
        yield

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

    @app.exception_handler(RateLimitExceeded)
    async def _rate_limit_exceeded_handler(
        request: Request, exc: RateLimitExceeded
    ) -> HTMLResponse | JSONResponse:
        headers = {"Retry-After": str(exc.retry_after_seconds)}
        payload = {
            "detail": "Rate limit exceeded",
            "policy": exc.policy_name,
            "limit_kind": exc.limit_kind,
            "limit": exc.limit,
            "window_seconds": exc.window_seconds,
            "retry_after_seconds": exc.retry_after_seconds,
        }

        accept = (request.headers.get("accept") or "").lower()
        wants_html = "text/html" in accept and not request.url.path.startswith("/api")
        if wants_html:
            # Keep the HTML minimal; the important part is a stable 429 with Retry-After.
            return HTMLResponse(
                content=(
                    "<!doctype html><html><head><title>Too Many Requests</title>"
                    "<meta charset='utf-8'></head><body>"
                    "<h1>Too many requests</h1>"
                    f"<p>Please retry in {exc.retry_after_seconds} seconds.</p>"
                    "</body></html>"
                ),
                status_code=429,
                headers=headers,
            )
        return JSONResponse(content=payload, status_code=429, headers=headers)

    if settings.session_secret:
        app.add_middleware(
            SessionMiddleware,
            secret_key=settings.session_secret,
            same_site="lax",
            https_only=settings.cookie_secure,
        )
    elif not settings.auth_disabled:
        raise RuntimeError("SESSION_SECRET is required when AUTH_DISABLED=false")

    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(web_router, dependencies=[Depends(rate_limit_dependency)])
    app.include_router(api_router, prefix="/api", dependencies=[Depends(rate_limit_dependency)])

    return app


app = create_app()
