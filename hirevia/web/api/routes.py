"""API routes for Web UI"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Body
from fastapi.responses import JSONResponse

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
    TelegramTestRequest,
    TelegramTestResponse,
    ScanTriggerResponse,
    StartStopResponse,
    ConfigResponse,
    JobListParams,
)

router = APIRouter()


def _state():
    return get_web_state()


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


@router.post("/start", response_model=StartStopResponse)
async def start_scanner(
    nvidia_key: Optional[str] = Body(None, description="NVIDIA API key"),
    telegram_token: Optional[str] = Body(None, description="Telegram bot token"),
    telegram_chat_id: Optional[str] = Body(None, description="Telegram chat ID"),
):
    """Start the background scanner"""
    state = _state()
    
    if state.is_running:
        return StartStopResponse(
            success=False,
            message="Scanner already running",
            status=SystemStatus.RUNNING,
        )
    
    success = await state.start_scanner(
        nvidia_key=nvidia_key,
        telegram_token=telegram_token,
        telegram_chat_id=telegram_chat_id,
    )
    
    status = state.get_status()
    return StartStopResponse(
        success=success,
        message="Scanner started" if success else "Failed to start scanner",
        status=SystemStatus(status["status"]),
    )


@router.post("/stop", response_model=StartStopResponse)
async def stop_scanner():
    """Stop the background scanner"""
    state = _state()
    
    if not state.is_running:
        return StartStopResponse(
            success=False,
            message="Scanner not running",
            status=SystemStatus.STOPPED,
        )
    
    success = await state.stop_scanner()
    status = state.get_status()
    
    return StartStopResponse(
        success=success,
        message="Scanner stopped" if success else "Failed to stop scanner",
        status=SystemStatus(status["status"]),
    )


@router.post("/scan", response_model=ScanTriggerResponse)
async def trigger_scan():
    """Trigger an immediate scan"""
    state = _state()
    
    if not state.is_running:
        raise HTTPException(status_code=400, detail="Scanner not running. Start it first.")
    
    if state.is_scanning:
        raise HTTPException(status_code=400, detail="Scan already in progress")
    
    try:
        stats = await state.trigger_scan()
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
    min_match: Optional[int] = Query(None),
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
        min_match=min_match,
        location=location,
        limit=limit,
        offset=offset,
    )
    return JobsResponse(**result)


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


@router.post("/telegram/test", response_model=TelegramTestResponse)
async def test_telegram(request: TelegramTestRequest):
    """Send a test Telegram message"""
    state = _state()
    result = await state.test_telegram(request.message)
    return TelegramTestResponse(**result)


@router.get("/config", response_model=ConfigResponse)
async def get_config():
    """Get current configuration"""
    state = _state()
    return ConfigResponse(**state.get_config())