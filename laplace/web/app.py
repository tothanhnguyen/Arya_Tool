"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from laplace.db import init_db
from laplace.tools.base import load_builtin_tools
from laplace.web import api, evalsview, settings_page, statsview, traceview


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        load_builtin_tools()
        yield

    app = FastAPI(title="Laplace's Demon", lifespan=lifespan)
    app.include_router(api.router, prefix="/api")
    app.include_router(traceview.router)
    app.include_router(settings_page.router)
    app.include_router(statsview.router)
    app.include_router(evalsview.router)
    return app
