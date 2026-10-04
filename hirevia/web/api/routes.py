"""API routes for Web UI"""

import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Body
import yaml
from pydantic import ValidationError

from hirevia.ai.providers import provider_status
from hirevia.config import AIConfig, FilterConfig, SourcesConfig
from hirevia.core.keyword_matching import parse_keyword_configuration
from hirevia.sources.telegram_config import TelegramChannel, TelegramSettings
from hirevia.web.state import get_web_state
from hirevia.web.api.models import (
    SystemStatus,
    SourceStatus,
    JobStatus,
    SystemStatusResponse,
    ScanStatsResponse,
    SourceInfo,
    JobsResponse,
    JobResponse,
    ScanTriggerResponse,
    StartStopResponse,
    ConfigResponse,
    JobListParams,
    SaveJobRequest,
    ConfigFileRequest,
    AISettingsRequest,
    TelegramPhoneRequest,
    TelegramOTPRequest,
    Telegram2FARequest,
    TelegramSourceUpdate,
)

router = APIRouter()
CONFIG_FILE_NAMES = ("profile.yaml", "filters.yaml", "sources.yaml", "telegram.yaml", "ai.yaml")


def _state():
    return get_web_state()


async def _reload_configuration(state, *, env_keys=()) -> None:
    try:
        await state.reload_configuration(env_keys=env_keys)
    except Exception as error:
        state.add_activity(
            f"Configuration reload failed ({type(error).__name__})", "error"
        )
        raise HTTPException(
            status_code=503,
            detail="Configuration was saved, but the worker did not apply it. Check worker status and retry.",
        ) from error


@router.get("/status", response_model=SystemStatusResponse)
async def get_status():
    """Get current system status"""
    state = _state()
    status = state.get_status()
    sources = state.get_source_status()
    
    return SystemStatusResponse(
        status=SystemStatus(status["status"]),
        current_operation=status["current_operation"],
        last_scan=status["last_scan"],
        next_scan=status["next_scan"],
        scan_duration=status["scan_duration"],
        scan_interval_minutes=status["scan_interval_minutes"],
        is_scanning=status["is_scanning"],
        is_running=status["is_running"],
        telegram_connected=status["telegram_connected"],
        ai_provider=status["ai_provider"],
        ai_provider_configured=status["ai_provider_configured"],
        ai_health_state=status["ai_health_state"],
        ai_health_checked_at=status["ai_health_checked_at"],
        queue_size=status["queue_size"],
        queue_processing=status["queue_processing"],
        queue_oldest_pending_at=status["queue_oldest_pending_at"],
        last_cleanup_at=status["last_cleanup_at"],
        worker_pid=status["worker_pid"],
        worker_alive=status["worker_alive"],
        worker_status=status["worker_status"],
        worker_started_at=status["worker_started_at"],
        worker_heartbeat_at=status["worker_heartbeat_at"],
        worker_last_error=status["worker_last_error"],
        monitor_desired=status["monitor_desired"],
        sources=[SourceInfo(**s) for s in sources],
    )


@router.get("/stats", response_model=ScanStatsResponse)
async def get_stats():
    """Get scan statistics"""
    state = _state()
    stats = state.get_stats()
    return ScanStatsResponse(**stats)


@router.get("/sources")
async def get_sources():
    """Get source status"""
    state = _state()
    return state.get_source_status()


@router.get("/queue/status")
async def get_queue_status():
    state = _state()
    return {
        **state.repo.get_telegram_queue_status(),
        "last_cleanup_at": state.last_cleanup_at.isoformat() if state.last_cleanup_at else None,
        "cleanup_error": state.cleanup_error,
    }


@router.delete("/jobs")
async def delete_all_jobs():
    deleted = _state().delete_all_jobs()
    return {"success": True, "deleted": deleted}


@router.post("/start", response_model=StartStopResponse)
async def start_scanner():
    """Start the background scanner"""
    state = _state()
    if getattr(state, "external_worker", False):
        state._sync_external_worker_status()
    
    if state.is_running:
        return StartStopResponse(
            success=False,
            message="Scanner already running",
            status=SystemStatus.RUNNING,
        )
    
    success = await state.start_scanner()
    
    status = state.get_status()
    return StartStopResponse(
        success=success,
        message=(
            "Scanner started" if success else
            getattr(state, "monitor_start_error", None) or "Failed to start scanner"
        ),
        status=SystemStatus(status["status"]),
    )


@router.post("/stop", response_model=StartStopResponse)
async def stop_scanner():
    """Stop the background scanner"""
    state = _state()
    if getattr(state, "external_worker", False):
        worker = state._sync_external_worker_status()
        desired = worker.get("desired_monitoring", "stopped")
        confirmed_stopped = (
            worker.get("status") == "STOPPED"
            and worker.get("monitor_status") == "STOPPED"
            and desired == "stopped"
        )
        if not worker.get("alive"):
            if confirmed_stopped:
                return StartStopResponse(
                    success=True,
                    message="Monitor already stopped",
                    status=SystemStatus.STOPPED,
                )
            raise HTTPException(
                status_code=503,
                detail="Monitor worker is unavailable; stop was not confirmed.",
            )
    
    if not state.is_running:
        if not getattr(state, "external_worker", False):
            return StartStopResponse(
                success=True,
                message="Monitor already stopped",
                status=SystemStatus.STOPPED,
            )
        if (
            worker.get("status") == "RUNNING"
            and worker.get("monitor_status") == "STOPPED"
            and desired == "stopped"
        ):
            return StartStopResponse(
                success=True,
                message="Monitor already stopped",
                status=SystemStatus.STOPPED,
            )
    
    success = await state.stop_scanner()
    if not success:
        raise HTTPException(
            status_code=503,
            detail=state.monitor_stop_error or "Monitor stop was not confirmed.",
        )
    status = state.get_status()
    if (
        status.get("is_running", False)
        or status.get("is_scanning", False)
        or status["status"] != "STOPPED"
    ):
        raise HTTPException(
            status_code=503,
            detail="STOP was acknowledged, but the backend has not confirmed STOPPED.",
        )
    
    return StartStopResponse(
        success=True,
        message="Monitor stopped",
        status=SystemStatus(status["status"]),
    )


@router.post("/scan", response_model=ScanTriggerResponse)
async def trigger_scan():
    """Trigger an immediate scan"""
    state = _state()
    
    if state.is_scanning:
        raise HTTPException(status_code=400, detail="Scan already in progress")
    
    try:
        stats = await state.trigger_scan(scan_24h=True)
        return ScanTriggerResponse(
            success=True,
            message="Scan completed",
            stats=ScanStatsResponse(**state.get_stats()),
        )
    except Exception as e:
        return ScanTriggerResponse(
            success=False,
            message=f"Scan failed: {e}",
            stats=None,
        )


@router.get("/jobs", response_model=JobsResponse)
async def get_jobs(
    search: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    priority: Optional[str] = Query(None, pattern="^(P1|P2|P3)$"),
    min_match: Optional[int] = Query(None),
    match_status: Optional[str] = Query(None, pattern="^unmatched$"),
    location: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Get filtered jobs list"""
    state = _state()
    result = state.get_jobs(
        search=search,
        source=source,
        status=status,
        priority=priority,
        min_match=min_match,
        match_status=match_status,
        location=location,
        limit=limit,
        offset=offset,
    )
    return JobsResponse(**result)


@router.get("/job-counts")
async def get_job_counts():
    return _state().get_job_counts()


@router.post("/jobs/{job_id}/save")
async def save_job(job_id: str, request: SaveJobRequest = Body(default=SaveJobRequest())):
    if not _state().save_job(job_id, request.saved):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True, "saved": request.saved}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    if not _state().delete_job(job_id):
        raise HTTPException(status_code=404, detail="Job not found")
    return {"success": True}


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str):
    """Get single job by ID"""
    state = _state()
    job = state.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(**job)


@router.get("/activity")
async def get_activity(limit: int = Query(50, ge=1, le=200)):
    """Get recent activity log"""
    state = _state()
    return state.get_activity(limit)


@router.get("/config", response_model=ConfigResponse)
async def get_config():
    """Get current configuration"""
    state = _state()
    return ConfigResponse(**state.get_config())


def _config_path(filename: str) -> Path:
    if filename not in CONFIG_FILE_NAMES:
        raise HTTPException(status_code=404, detail="Configuration file not available")
    path = Path.cwd() / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Configuration file does not exist")
    return path


def _validate_yaml(filename: str, content: str) -> list[str]:
    try:
        payload = yaml.safe_load(content)
        if not isinstance(payload, dict):
            raise ValueError("YAML document must contain a mapping at the root")
        if filename == "profile.yaml":
            parse_keyword_configuration(payload)
            return []
        if filename == "filters.yaml":
            FilterConfig.model_validate(payload)
        elif filename == "sources.yaml":
            SourcesConfig.model_validate(payload)
        elif filename == "telegram.yaml":
            TelegramSettings.model_validate(payload.get("telegram", payload) or {})
        elif filename == "ai.yaml":
            AIConfig.model_validate(payload.get("ai", payload) or {})
        return []
    except (yaml.YAMLError, ValidationError, TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.get("/config/files")
async def list_config_files():
    return {"files": [name for name in CONFIG_FILE_NAMES if (Path.cwd() / name).is_file()]}


@router.get("/config/files/{filename}")
async def read_config_file(filename: str):
    path = _config_path(filename)
    return {"name": filename, "content": path.read_text(encoding="utf-8")}


@router.post("/config/files/{filename}/validate")
async def validate_config_file(filename: str, request: ConfigFileRequest):
    _config_path(filename)
    warnings = _validate_yaml(filename, request.content)
    return {"valid": True, "warnings": warnings}


@router.put("/config/files/{filename}")
async def save_config_file(filename: str, request: ConfigFileRequest):
    state = _state()
    path = _config_path(filename)
    _validate_yaml(filename, request.content)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
            prefix=f".{path.name}.", suffix=".tmp",
        ) as temporary:
            temporary.write(request.content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temp_name = temporary.name
        shutil.copy2(path, path.with_name(f".{path.name}.bak"))
        os.replace(temp_name, path)
    except OSError as error:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Could not save configuration file") from error
    await _reload_configuration(state)
    state.add_activity(f"Configuration file saved: {filename}")
    return {"success": True, "name": filename}


@router.get("/ai/config")
async def get_ai_config():
    config = _state().config
    status = {item["name"]: item for item in provider_status(config)}
    return {
        "primary": config.ai.primary,
        "timeout_seconds": config.ai.timeout_seconds,
        "matching": {
            "p1_threshold": config.priority_p1_min_score,
            "p2_threshold": config.priority_p2_min_score,
            "p3_threshold": config.priority_p3_min_score,
        },
        "providers": [
            {
                "name": name,
                "model": spec.model,
                "base_url": spec.base_url,
                "configured": status[name]["configured"],
                "primary": status[name]["primary"],
                "api_key_configured": bool(spec.api_key_env and os.getenv(spec.api_key_env)),
            }
            for name, spec in config.ai.providers.items()
        ],
    }


def _persist_env_settings(settings: dict[str, str]) -> None:
    env_path = Path.cwd() / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    for key, value in settings.items():
        if "\n" in value or "\r" in value:
            raise ValueError("setting values cannot contain newlines")
        matches = [
            index for index, line in enumerate(lines)
            if line.strip().removeprefix("export ").partition("=")[0].strip() == key
        ]
        if matches:
            lines[matches[0]] = f"{key}={value}"
            for index in reversed(matches[1:]):
                del lines[index]
        else:
            lines.append(f"{key}={value}")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=env_path.parent, delete=False,
            prefix=".env.", suffix=".tmp",
        ) as temporary:
            temporary.write("\n".join(lines) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temp_name = temporary.name
        if env_path.exists():
            backup_path = env_path.with_name(".env.bak")
            shutil.copy2(env_path, backup_path)
            os.chmod(backup_path, 0o600)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, env_path)
    except OSError:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        raise
    os.environ.update(settings)


@router.put("/ai/config")
async def update_ai_config(request: AISettingsRequest):
    state = _state()
    name = request.primary.strip().lower()
    if name not in state.config.ai.providers:
        raise HTTPException(status_code=422, detail="Unknown AI provider")
    if not request.model.strip():
        raise HTTPException(status_code=422, detail="Model is required")
    settings = {
        "AI_PRIMARY_PROVIDER": name,
        f"{name.upper()}_MODEL": request.model.strip(),
        "AI_TIMEOUT_SECONDS": str(request.timeout_seconds),
        "MATCHING_P1_THRESHOLD": str(request.p1_threshold),
        "MATCHING_P2_THRESHOLD": str(request.p2_threshold),
        "MATCHING_P3_THRESHOLD": str(request.p3_threshold),
    }
    if request.base_url is not None:
        parsed = urlparse(request.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username:
            raise HTTPException(status_code=422, detail="Invalid provider base URL")
        settings[f"{name.upper()}_BASE_URL"] = request.base_url.rstrip("/")
    if request.api_key is not None and request.api_key.get_secret_value():
        api_key_env = state.config.ai.providers[name].api_key_env
        if not api_key_env:
            raise HTTPException(status_code=422, detail="This provider has no API-key environment setting")
        settings[api_key_env] = request.api_key.get_secret_value()
    try:
        _persist_env_settings(settings)
    except (OSError, ValueError) as error:
        raise HTTPException(status_code=500, detail="Could not save AI settings") from error
    await _reload_configuration(state, env_keys=settings.keys())
    state.add_activity(f"AI settings updated: primary={name}, model={request.model.strip()}")
    return {"success": True, "primary": state.config.ai.primary, "model": state.config.ai.providers[name].model}


@router.post("/ai/test")
async def test_ai_provider():
    return await _state().test_ai_provider()


@router.get("/telegram/config")
async def get_telegram_config():
    return await _state().telegram_status()


@router.post("/telegram/sources/validate")
async def validate_telegram_sources():
    return await _state().validate_telegram_sources(force=True)


@router.post("/telegram/login/phone")
async def begin_telegram_phone_login(request: TelegramPhoneRequest):
    return await _state().begin_telegram_phone_login(request.phone_number)


@router.post("/telegram/login/otp")
async def verify_telegram_otp(request: TelegramOTPRequest):
    return await _state().verify_telegram_otp(request.code.get_secret_value())


@router.post("/telegram/login/resend")
async def resend_telegram_otp():
    return await _state().resend_telegram_otp()


@router.post("/telegram/login/password")
async def verify_telegram_password(request: Telegram2FARequest):
    return await _state().verify_telegram_password(request.password.get_secret_value())


@router.post("/telegram/logout")
async def logout_telegram():
    return await _state().logout_telegram()


@router.post("/telegram/sources/join")
async def join_telegram_sources():
    return await _state().join_telegram_sources()


@router.put("/telegram/sources/{username}")
async def update_telegram_source(username: str, request: TelegramSourceUpdate):
    state = _state()
    path = Path.cwd() / "telegram.yaml"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="telegram.yaml does not exist")
    try:
        normalized = TelegramChannel(username=username).username
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        section = document.get("telegram", document)
        if not isinstance(section, dict) or not isinstance(section.get("channels"), list):
            raise ValueError("telegram channels must be a list")
        channel = next(
            (item for item in section["channels"]
             if isinstance(item, dict)
             and TelegramChannel(username=item.get("username", "")).username == normalized),
            None,
        )
        if channel is None:
            raise HTTPException(status_code=404, detail="Configured Telegram source not found")
        channel["enabled"] = request.enabled
        serialized = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    except HTTPException:
        raise
    except (yaml.YAMLError, ValidationError, TypeError, ValueError, AttributeError) as error:
        raise HTTPException(status_code=422, detail="telegram.yaml is not valid configuration") from error

    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False,
            prefix=".telegram.yaml.", suffix=".tmp",
        ) as temporary:
            temporary.write(serialized)
            temp_name = temporary.name
        os.replace(temp_name, path)
    except OSError as error:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        raise HTTPException(status_code=500, detail="Could not save Telegram sources") from error
    await _reload_configuration(state)
    state.add_activity(
        f"Telegram source @{normalized} collection {'enabled' if request.enabled else 'disabled'}"
    )
    return {"success": True, "username": normalized, "enabled": request.enabled}


@router.post("/telegram/sources/{username}/join")
async def join_telegram_source(username: str):
    try:
        normalized = TelegramChannel(username=username).username
    except ValidationError as error:
        raise HTTPException(status_code=422, detail="Invalid public channel username") from error
    return await _state().set_telegram_source_membership(normalized, join=True)


@router.post("/telegram/sources/{username}/leave")
async def leave_telegram_source(username: str):
    try:
        normalized = TelegramChannel(username=username).username
    except ValidationError as error:
        raise HTTPException(status_code=422, detail="Invalid public channel username") from error
    return await _state().set_telegram_source_membership(normalized, join=False)
