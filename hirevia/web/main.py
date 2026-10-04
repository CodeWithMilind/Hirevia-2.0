"""FastAPI application for Hirevia Web Control Panel"""

import logging
import hmac
import os
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from hirevia.web.api.routes import router as api_router
from hirevia.web.auth import (
    AuthConfigurationError,
    SESSION_COOKIE,
    bootstrap_initial_admin,
    clear_failed_logins,
    create_session,
    ensure_session_secret,
    is_rate_limited,
    load_auth_config,
    record_failed_login,
    revoke_session,
    session_is_valid,
    verify_password,
)
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
    from dotenv import load_dotenv

    env_file = Path(os.getenv("HIREVIA_ENV_FILE", Path.cwd() / ".env"))
    load_dotenv(dotenv_path=env_file, override=False)
    ensure_session_secret(env_file)
    bootstrap_initial_admin(env_file)
    state = get_web_state()
    cleanup_task = None
    if state.external_worker:
        logger.info("Worker lifecycle is independent; web will not start or restart it")
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
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        if not state.external_worker and state.is_running:
            await state.stop_scanner(persist_desired=False)
        if not state.external_worker:
            await state._close_telegram_clients()


app = FastAPI(
    title="Hirevia 2.0 Web Control Panel",
    description="Web interface for Hirevia autonomous job discovery agent",
    version="2.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

trusted_hosts = [
    host.strip()
    for host in os.getenv("HIREVIA_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if host.strip()
]
app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)


@app.middleware("http")
async def require_admin_session(request: Request, call_next):
    path = request.url.path
    public_path = path in {"/login", "/auth/login", "/auth/session", "/health"} or path.startswith("/static/")
    if path.startswith("/api/") or path == "/" or path == "/auth/logout":
        try:
            config = load_auth_config()
        except AuthConfigurationError:
            if path == "/":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse(status_code=503, content={"detail": "Admin authentication is not configured"})
        if not session_is_valid(config, request.cookies.get(SESSION_COOKIE)):
            if path == "/":
                return RedirectResponse("/login", status_code=303)
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
            )
        if path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            host = request.headers.get("host", "")
            if not origin or urlsplit(origin).netloc.casefold() != host.casefold():
                return JSONResponse(status_code=403, content={"detail": "Request origin rejected"})
    if public_path and path == "/login":
        try:
            config = load_auth_config()
        except AuthConfigurationError:
            config = None
        if config and session_is_valid(config, request.cookies.get(SESSION_COOKIE)):
            return RedirectResponse("/", status_code=303)
    return await call_next(request)


# Include API routes
app.include_router(api_router, prefix="/api")

static_dir = Path(__file__).parent / "static"


@app.post("/auth/login")
async def login(request: Request):
    try:
        config = load_auth_config()
    except AuthConfigurationError:
        return JSONResponse(status_code=503, content={"detail": "Admin authentication is not configured"})

    try:
        payload = await request.json()
        email = payload.get("email", "")
        password = payload.get("password", "")
    except (ValueError, AttributeError):
        email, password = "", ""
    if not isinstance(email, str) or not isinstance(password, str) or not email.strip() or not password:
        return JSONResponse(status_code=401, content={"detail": "Invalid email or password"})
    if len(email) > 254 or len(password) > 1024:
        return JSONResponse(status_code=401, content={"detail": "Invalid email or password"})

    remote_address = request.client.host if request.client else "unknown"
    submitted_email = email.strip().casefold()
    if is_rate_limited(config, submitted_email, remote_address):
        return JSONResponse(status_code=429, content={"detail": "Too many attempts. Try again later."})

    password_valid = verify_password(config.password_hash, password)
    if not (hmac.compare_digest(submitted_email.encode("utf-8"), config.admin_email.encode("utf-8")) and password_valid):
        record_failed_login(config, submitted_email, remote_address)
        return JSONResponse(status_code=401, content={"detail": "Invalid email or password"})

    clear_failed_logins(config, submitted_email, remote_address)
    token, expires_at = create_session(config)
    response = JSONResponse(content={"authenticated": True, "expires_at": expires_at})
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=config.session_ttl_seconds,
        httponly=True,
        secure=config.cookie_secure,
        samesite="lax",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/auth/session")
async def auth_session(request: Request):
    try:
        config = load_auth_config()
    except AuthConfigurationError:
        return {"authenticated": False}
    return {"authenticated": session_is_valid(config, request.cookies.get(SESSION_COOKIE))}


@app.post("/auth/logout")
async def logout(request: Request):
    try:
        config = load_auth_config()
    except AuthConfigurationError:
        config = None
    if config:
        revoke_session(config, request.cookies.get(SESSION_COOKIE))
    response = JSONResponse(content={"authenticated": False})
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=bool(config and config.cookie_secure), samesite="lax")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the main dashboard HTML"""
    index_path = static_dir / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return HTMLResponse(
        "<html><body><h1>Hirevia Web UI</h1><p>Dashboard not built yet. Run build step.</p></body></html>"
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return FileResponse(static_dir / "login.html", headers={"Cache-Control": "no-store"})


@app.get("/static/{asset_name}")
async def static_asset(asset_name: str):
    if asset_name not in {"app.js", "style.css", "login.js", "login.css"}:
        return HTMLResponse("Not found", status_code=404)
    return FileResponse(static_dir / asset_name)


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "ok", "service": "hirevia-web"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)