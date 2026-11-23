"""Local FastAPI application: 127.0.0.1 only, no CDN, no Node.js."""

from __future__ import annotations

import importlib
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from tg_collab.config import AppPaths, ensure_home, resolve_paths, set_settings
from tg_collab.db import init_db
from tg_collab.models import utcnow
from tg_collab.web.common import base_context, connect
from tg_collab.web.i18n import label, t
from tg_collab.web.jobs import JobManager

WEB_DIR = Path(__file__).parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"
ROUTER_MODULES = ("routes_dashboard", "routes_channels", "routes_settings", "routes_jobs")


def _same_origin(origin: str, request: Request) -> bool:
    parsed = urlsplit(origin)
    return parsed.scheme == request.url.scheme and parsed.netloc.lower() == request.url.netloc.lower()


def _include_routers(app: FastAPI) -> None:
    for name in ROUTER_MODULES:
        try:
            module = importlib.import_module(f"tg_collab.web.{name}")
        except ModuleNotFoundError as exc:
            if exc.name == f"tg_collab.web.{name}":
                continue
            raise
        router = getattr(module, "router", None)
        if router is not None:
            app.include_router(router)


def create_app(paths: AppPaths | str | Path | None = None) -> FastAPI:
    if isinstance(paths, (str, Path)):
        resolved = resolve_paths(str(paths))
    else:
        resolved = paths or resolve_paths()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ensure_home(resolved)
        conn = init_db(resolved.db_path)
        now = utcnow()
        with conn:
            conn.execute(
                "UPDATE jobs SET status='error', message='interrupted by restart', "
                "updated_at=?, finished_at=? WHERE status='running'",
                (now, now),
            )
        conn.close()
        yield
        application.state.jobs.shutdown()

    app = FastAPI(title="tg-collab", lifespan=lifespan)
    app.state.paths = resolved
    app.state.jobs = JobManager(resolved.db_path)
    app.state.templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    app.state.templates.env.globals["t"] = t
    app.state.templates.env.globals["label"] = label
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.middleware("http")
    async def cross_site_guard(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            site = request.headers.get("sec-fetch-site")
            if origin and not _same_origin(origin, request):
                return PlainTextResponse("cross-origin request rejected", status_code=403)
            if site and site != "same-origin":
                return PlainTextResponse("cross-origin request rejected", status_code=403)
        return await call_next(request)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/lang/{code}")
    def switch_language(code: str, request: Request) -> RedirectResponse:
        language = code if code in ("en", "ru") else "en"
        conn = connect(request)
        try:
            set_settings(conn, {"language": language})
        finally:
            conn.close()
        target = request.headers.get("referer") or "/"
        return RedirectResponse(url=target, status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def not_found_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 404:
            templates: Jinja2Templates = request.app.state.templates
            conn = connect(request)
            try:
                context = base_context(request, conn)
            finally:
                conn.close()
            return templates.TemplateResponse(
                request=request, name="404.html", context=context, status_code=404
            )
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    _include_routers(app)
    return app


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8080)
