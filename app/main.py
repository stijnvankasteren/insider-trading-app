from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import router as api_router
from app.db import init_db
from app.settings import get_settings
from app.web import router as web_router


def create_app() -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db()
        yield

    app = FastAPI(title=settings.app_name, lifespan=lifespan)

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
    app.include_router(web_router)
    app.include_router(api_router, prefix="/api")

    return app


app = create_app()
