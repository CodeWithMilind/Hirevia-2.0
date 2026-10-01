"""FastAPI application for Hirevia Web Control Panel"""

import logging
import hmac
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from hirevia.web.api.routes import router as api_router
from hirevia.web.state import get_web_state

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown"""
    logger.info("Starting Hirevia Web UI")
    state = get_web_state()
    cleanup_task = None
    supervisor_task = None
    supervisor_stop = None
    if state.external_worker:
        from hirevia.worker_process import WorkerProcessController

        controller = WorkerProcessController(state.repo)
        state.worker_controller = controller
        if controller.spawn_enabled:
            try:
                await controller.ensure_running(spawn_if_missing=True)
            except Exception as error:
                logger.error("Local worker startup failed (%s)", type(error).__name__)
            supervisor_stop = asyncio.Event()
            supervisor_task = asyncio.create_task(
                controller.supervise(supervisor_stop)
            )
        else:
            logger.info("Worker lifecycle is managed externally; web will not spawn it")
        await state.restore_telegram_auth_for_startup()
    else:
        state.repo.recover_processing_queue()
        cleanup_task = asyncio.create_task(state.cleanup_loop())
        await state.restore_telegram_auth_for_startup()
        await state.recover_monitoring_for_startup()
    try:
        yield
    finally:
        logger.info("Shutting down Hirevia Web UI")
        if supervisor_stop is not None:
            supervisor_stop.set()
        for task in (cleanup_task, supervisor_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (cleanup_task, supervisor_task) if task is not None),
            return_exceptions=True,
        )
        if not state.external_worker and state.is_running:
            await state.stop_scanner(persist_desired=False)
        if not state.external_worker:
            await state._close_telegram_clients()


app = FastAPI(
    title="Hirevia 2.0 Web Control Panel",
    description="Web interface for Hirevia autonomous job discovery agent",
    version="2.0.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def require_admin_token(request, call_next):
    expected = (os.getenv("HIREVIA_ADMIN_TOKEN") or "").strip()
    if expected and request.url.path.startswith("/api/"):
        authorization = request.headers.get("authorization", "")
        scheme, _, supplied = authorization.partition(" ")
        if scheme.casefold() != "bearer" or not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "A valid Hirevia admin token is required"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


# Include API routes
app.include_router(api_router, prefix="/api")

# Static files
static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the main dashboard HTML"""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse(
        "<html><body><h1>Hirevia Web UI</h1><p>Dashboard not built yet. Run build step.</p></body></html>"
    )


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "hirevia-web"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)