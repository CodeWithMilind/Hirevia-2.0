"""FastAPI application for Hirevia Web Control Panel"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
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
    # Startup
    logger.info("Starting Hirevia Web UI")
    state = get_web_state()
    yield
    # Shutdown
    logger.info("Shutting down Hirevia Web UI")
    if state.is_running:
        await state.stop_scanner()


app = FastAPI(
    title="Hirevia 2.0 Web Control Panel",
    description="Web interface for Hirevia autonomous job discovery agent",
    version="2.0.0",
    lifespan=lifespan,
)

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