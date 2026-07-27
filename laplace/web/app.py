"""FastAPI application factory."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from laplace.db import init_db
from laplace.tools.base import load_builtin_tools
from laplace.web import api, evalsview, settings_page, statsview, traceview
from laplace.web.social import api as social_api
from laplace.web.social import facebook_connect, instagram_connect
from laplace.web.social import views as social_views


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()
        load_builtin_tools()
        yield

    app = FastAPI(title="Arya_Tool", lifespan=lifespan)
    app.include_router(api.router, prefix="/api")
    app.include_router(traceview.router)
    app.include_router(settings_page.router)
    app.include_router(statsview.router)
    app.include_router(evalsview.router)
    app.include_router(social_api.router)
    app.include_router(social_views.router)
    app.include_router(facebook_connect.router)
    app.include_router(facebook_connect.api_router)
    app.include_router(instagram_connect.router)
    return app
