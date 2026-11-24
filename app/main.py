import asyncio
import contextlib
import logging
import re
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.api.routes.admin import router as admin_router
from app.api.routes.webhooks import router as webhook_router
from app.config.settings import get_settings
from app.db.connection import database_is_ready, make_engine, make_session_factory

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper(), format="%(message)s")
logger = logging.getLogger("whatsapp_assistant")
engine = make_engine(settings)


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    processes: list[asyncio.subprocess.Process] = []
    application.state.worker_processes = processes
    if settings.run_workers_in_api and settings.app_env not in {"development", "test"}:
        raise RuntimeError("RUN_WORKERS_IN_API is restricted to local development and test")

    modules = ["app.workers.conversation", "app.workers.outbound"]
    if settings.zoho_enabled:
        modules.append("app.workers.crm_sync")
    project_directory = Path(__file__).resolve().parent.parent
    try:
        if settings.run_workers_in_api:
            for module in modules:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    module,
                    cwd=project_directory,
                )
                processes.append(process)
                logger.info("embedded_worker_started module=%s pid=%s", module, process.pid)
        yield
    finally:
        for process in reversed(processes):
            if process.returncode is None:
                process.terminate()
        for process in reversed(processes):
            if process.returncode is None:
                try:
                    await asyncio.wait_for(process.wait(), timeout=10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(engine.dispose)


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        candidate = request.headers.get("X-Request-ID", "")
        request_id = (
            candidate if re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", candidate) else str(uuid.uuid4())
        )
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "http_request method=%s path=%s status=%s request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            request_id,
        )
        return response


app = FastAPI(
    title="WhatsApp Real Estate Assistant", version=settings.app_release, lifespan=lifespan
)
app.add_middleware(RequestContextMiddleware)
app.state.engine = engine
app.state.session_factory = make_session_factory(engine)
app.include_router(webhook_router)
app.include_router(admin_router)


@app.get("/health/live", tags=["health"])
def liveness() -> dict[str, str]:
    return {"status": "ok", "release": settings.app_release}


@app.get("/health/ready", tags=["health"])
def readiness() -> JSONResponse:
    issues = settings.readiness_issues()
    database_ready = database_is_ready(engine)
    if not database_ready:
        issues.append("Database is unavailable")
    worker_processes = getattr(app.state, "worker_processes", [])
    if settings.run_workers_in_api and any(
        process.returncode is not None for process in worker_processes
    ):
        issues.append("An embedded worker has stopped")
    status_code = 200 if not issues else 503
    return JSONResponse(
        status_code=status_code,
        content={"status": "ready" if not issues else "not_ready", "checks": issues},
    )


@app.get("/", include_in_schema=False)
def root() -> dict[str, str]:
    return {"service": "whatsapp-real-estate-assistant", "release": settings.app_release}
