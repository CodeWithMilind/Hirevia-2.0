"""CLI for Hirevia"""

import asyncio
import ipaddress
import json
import logging
import os
from logging.handlers import RotatingFileHandler
import re
import socket
import sqlite3
import sys
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hirevia.ai.client import AIError
from hirevia.ai.matcher import AIMatcher
from hirevia.ai.providers import build_ai_provider_router, provider_status
from hirevia.config import Config, load_config
from hirevia.core.classification import classify_score
from hirevia.core.matching import analyze
from hirevia.core.models import CandidateProfile, Job, ScanStats
from hirevia.core.pipeline import Pipeline, run_single_scan
from hirevia.core.verification import classify_application_url
from hirevia.core.freshness import freshness_label
from hirevia.storage.state import JobRepository, resolve_database_path
from hirevia.web.state import initialize_web_state

console = Console()
logger = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"


def _redact_log_text(message: str) -> str:
    """Remove configured secret values and secret-shaped assignments."""
    secret_markers = (
        "API_KEY", "API_HASH", "API_ID", "BOT_TOKEN", "ADMIN_TOKEN",
        "PASSWORD", "OTP", "SECRET", "SESSION", "PHONE",
    )
    secret_values = sorted(
        {
            value
            for name, value in os.environ.items()
            if any(marker in name.upper() for marker in secret_markers) and value
        },
        key=len,
        reverse=True,
    )
    for secret in secret_values:
        message = message.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)\b(api[_-]?(?:key|hash|id)|bot[_-]?token|admin[_-]?token|password|passphrase|otp|verification code|one[- ]time code|2fa code|phone(?:[_ ]number)?|secret|session)(\s*(?:[:=]|is\b|-)\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        message,
    )


class _SecretRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact_log_text(super().format(record))


def configure_logging(verbose: bool = False) -> None:
    """Configure concise terminal output and complete rotating file logs."""
    configured_level = os.getenv("HIREVIA_LOG_LEVEL", "").strip().upper()
    level = getattr(logging, configured_level, None) if configured_level else None
    if not isinstance(level, int):
        level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    stream_handlers = [
        handler for handler in root.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        and getattr(handler, "_hirevia_console_handler", False)
    ]
    if not stream_handlers:
        candidates = [
            handler for handler in root.handlers
            if isinstance(handler, logging.StreamHandler)
            and not isinstance(handler, logging.FileHandler)
            and type(handler).__name__ != "LogCaptureHandler"
        ]
        stream_handler = candidates[0] if candidates else logging.StreamHandler(sys.stdout)
        if candidates:
            stream_handler.stream = sys.stdout
        stream_handler._hirevia_console_handler = True
        if stream_handler not in root.handlers:
            root.addHandler(stream_handler)
        stream_handlers = [stream_handler]
        for handler in candidates[1:]:
            root.removeHandler(handler)
            handler.close()
    for handler in stream_handlers:
        handler.stream = sys.stdout
        handler.setLevel(logging.DEBUG)
        handler.filters.clear()
        handler.addFilter(_MonitorLogFilter(verbose=verbose))
        handler.setFormatter(_SecretRedactingFormatter("%(message)s"))

    log_directory = Path(os.getenv("HIREVIA_LOG_DIR", "logs")).expanduser()
    log_directory.mkdir(parents=True, exist_ok=True)
    log_path = (log_directory / "hirevia.log").resolve()
    file_handlers = [
        handler for handler in root.handlers
        if isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename).resolve() == log_path
    ]
    for handler in list(root.handlers):
        if (
            isinstance(handler, RotatingFileHandler)
            and getattr(handler, "_hirevia_file_handler", False)
            and Path(handler.baseFilename).resolve() != log_path
        ):
            root.removeHandler(handler)
            handler.close()
    file_handlers = [
        handler for handler in root.handlers
        if isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename).resolve() == log_path
    ]
    for handler in file_handlers[1:]:
        root.removeHandler(handler)
        handler.close()
    file_handlers = file_handlers[:1]
    if not file_handlers:
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=max(1024 * 1024, int(os.getenv("HIREVIA_LOG_MAX_BYTES", "10000000"))),
            backupCount=max(1, int(os.getenv("HIREVIA_LOG_BACKUPS", "7"))),
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(_SecretRedactingFormatter(LOG_FORMAT))
        file_handler._hirevia_file_handler = True
        root.addHandler(file_handler)
    else:
        file_handlers[0].setLevel(level)
        file_handlers[0].setFormatter(_SecretRedactingFormatter(LOG_FORMAT))


class _MonitorLogFilter(logging.Filter):
    """Send only concise status and actionable failure lines to the terminal."""

    def __init__(self, verbose: bool = False):
        super().__init__()
        self.verbose = verbose

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return False

        if record.name in {"telethon", "httpx", "httpx2"} and record.levelno < logging.WARNING:
            return False

        display = None
        if self.verbose:
            display = f"{record.levelname} {record.name} | {message}"
        if record.name == "hirevia.cli" and message.startswith("Starting web server at "):
            display = "Hirevia started"
        elif record.name == "hirevia.worker_runtime":
            if message.startswith("Monitor started"):
                display = "Monitor started"
            elif message.startswith("Telegram connected ("):
                display = message
            elif message.startswith("Scan complete | "):
                display = message
        elif record.name == "hirevia.sources.telegram_source":
            if "New message: channel=" in message:
                display = "[Telegram] New message received"
            elif "[Filter] REJECTED" in message:
                display = "[Telegram] New message ignored — not a job"
            elif "New message detected in " in message:
                display = "[Telegram] New job detected"
            elif "Connection lost; reconnecting" in message:
                display = "[Telegram] Connection lost — reconnecting"
            elif "[Telegram] Reconnected" in message:
                display = "[Telegram] Reconnected"
        elif record.name == "hirevia.core.pipeline":
            if "Stage 3: duplicate of " in message:
                display = "[Telegram] Duplicate ignored"
        elif record.name == "hirevia.cli":
            if "Message processing incomplete; retrying" in message or "Startup catch-up incomplete; retrying" in message:
                display = "[Telegram] Processing failed — retrying"

        if display is None and record.levelno >= logging.WARNING:
            error_type = record.exc_info[0].__name__ if record.exc_info else None
            detail = f" ({error_type})" if error_type else ""
            display = f"{record.levelname}: {record.name}{detail}"
        if display is not None:
            timestamp = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            rendered = _redact_log_text(display)
            if self.verbose:
                rendered = _redact_log_text(f"{record.levelname} {record.name} | {message}")
            console.print(f"[{timestamp}] {rendered}", markup=False)
        return False


@contextmanager
def _monitor_log_presentation(verbose: bool):
    """Silence monitor internals in normal mode, restoring logging afterward."""
    if verbose:
        yield
        return

    monitor_loggers = [
        logging.getLogger("hirevia.core.pipeline"),
        logging.getLogger("hirevia.sources.telegram_source"),
    ]
    logger_states = [(item, item.level) for item in monitor_loggers]
    for item in monitor_loggers:
        item.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        for item, level in logger_states:
            item.setLevel(level)


def _resolve_nvidia_key() -> Optional[str]:
    """Read NVIDIA credentials from the environment without prompting."""
    key = (os.getenv("NVIDIA_API_KEY") or "").strip()
    return key or None


def _build_ai_matcher(config: Config, disabled: bool = False) -> Optional[AIMatcher]:
    if disabled or not config.ai.enabled:
        return None
    if not any(provider["configured"] for provider in provider_status(config)):
        return None
    return AIMatcher(config, _resolve_nvidia_key())


@click.group(invoke_without_command=True)
@click.version_option(version="2.0.0", prog_name="hirevia")
@click.pass_context
def cli(ctx):
    """Hirevia 2.0 - Autonomous Job Discovery Agent"""
    if ctx.invoked_subcommand is None:
        run_service()


def _port_owner(host: str, port: int) -> Optional[str]:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            probe.bind((probe_host, port))
            return None
        except OSError:
            pass
    try:
        request = Request(f"http://{probe_host}:{port}/health")
        with urlopen(request, timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("service", "unknown")
    except (OSError, ValueError, HTTPError, URLError):
        return "unknown"


def run_service(host: Optional[str] = None, port: Optional[int] = None) -> None:
    """Start the shared core and web UI in one process."""
    import uvicorn

    configure_logging()
    logger.info("Hirevia command center startup requested")
    try:
        _run_web_service(host, port)
    except click.ClickException as error:
        logger.error("Hirevia startup refused: %s", error)
        raise
    except Exception:
        logger.exception("Hirevia startup or web server failed")
        raise
    finally:
        logger.info("Hirevia launcher stopped")


def _run_web_service(host: Optional[str] = None, port: Optional[int] = None) -> None:
    import uvicorn

    config = load_config()
    host = host or os.getenv("HIREVIA_HOST", "127.0.0.1")
    port = port or int(os.getenv("HIREVIA_PORT", "8000"))
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.casefold() == "localhost"
    if not loopback and not os.getenv("HIREVIA_ADMIN_TOKEN", "").strip():
        raise click.ClickException(
            "Non-local web binding requires HIREVIA_ADMIN_TOKEN; use a TLS reverse proxy for remote access"
        )
    owner = _port_owner(host, port)
    if owner:
        if owner == "hirevia-web":
            display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
            console.print(f"Hirevia is already running at http://{display_host}:{port}")
            return
        raise click.ClickException(
            f"Port {port} is already occupied; set HIREVIA_PORT to another port"
        )

    repo = JobRepository(resolve_database_path())
    matcher = _build_ai_matcher(config)
    state = initialize_web_state(config, repo, matcher)
    state.external_worker = True
    from hirevia.web.main import app

    app.state.auto_start_monitoring = False
    display_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    console.print(Panel("[bold]HIREVIA[/bold]\nAI Opportunity Discovery Agent", width=44))
    console.print("● Core       READY")
    console.print(f"● Web        http://{display_host}:{port}")
    telegram = "ENABLED" if config.sources.sources.get("telegram") and config.sources.sources["telegram"].enabled else "DISABLED"
    console.print(f"● Telegram   {telegram}")
    console.print(f"● AI         {config.ai.primary}")
    console.print("● Agent      STOPPED")
    console.print(f"\nHirevia is running.\nOpen: http://{display_host}:{port}\n\nPress Ctrl+C to stop.")
    logger.info("Starting web server at http://%s:%d", display_host, port)
    uvicorn.run(app, host=host, port=port, log_level="info", log_config=None)


@cli.command()
@click.option("--host", default=None, help="Bind address (or HIREVIA_HOST)")
@click.option("--port", type=int, default=None, help="Web port (or HIREVIA_PORT)")
def web(host: Optional[str], port: Optional[int]) -> None:
    """Run the web interface for administration or debugging."""
    run_service(host, port)


@cli.group(invoke_without_command=True)
@click.pass_context
def worker(ctx) -> None:
    """Manage the independent worker, or run it in the foreground."""
    if ctx.invoked_subcommand is not None:
        return
    configure_logging()
    from hirevia.worker import run_worker

    exit_code = asyncio.run(run_worker())
    if exit_code == 2:
        click.echo("A Hirevia worker is already running", err=True)
        raise click.exceptions.Exit(2)
    if exit_code:
        raise click.ClickException(f"Worker exited with status {exit_code}")


def _worker_controller(*, spawn_enabled: Optional[bool] = None):
    from hirevia.worker_process import WorkerProcessController

    return WorkerProcessController(
        JobRepository(resolve_database_path()), spawn_enabled=spawn_enabled
    )


def _worker_uptime(started_at: Optional[str]) -> str:
    if not started_at:
        return "unknown"
    try:
        started = datetime.fromisoformat(started_at)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        seconds = max(0, int((now - started).total_seconds()))
    except (TypeError, ValueError):
        return "unknown"
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{days}d {hours:02}:{minutes:02}:{seconds:02}"


def _print_worker_status(runtime, db_path: Path) -> None:
    from hirevia.worker_process import get_process_executable_path

    pid = runtime.get("pid")
    alive = bool(runtime.get("alive"))
    console.print(f"Database: {db_path}")
    console.print(f"Worker: {'RUNNING' if alive else runtime.get('status', 'STOPPED')}")
    if pid is not None:
        console.print(f"PID: {pid}")
        executable = get_process_executable_path(pid) if alive else None
        console.print(f"Executable: {executable or 'not available'}")
        if alive:
            console.print(f"Uptime: {_worker_uptime(runtime.get('started_at'))}")
        else:
            console.print("Lease PID is stale; no process was signaled")
    console.print(f"Monitor: {runtime.get('monitor_status', 'STOPPED')}")
    if runtime.get("last_error"):
        console.print(f"Last error: {runtime['last_error']}")


@worker.command("start")
def worker_start() -> None:
    """Start a background worker explicitly."""
    configure_logging()
    controller = _worker_controller()
    try:
        runtime = asyncio.run(controller.ensure_running(spawn_if_missing=True))
    except Exception as error:
        raise click.ClickException(f"Worker could not start: {error}") from error
    _print_worker_status(runtime, controller.repo.db_path)
    if runtime.get("already_running"):
        console.print("Existing worker retained; no second process was started")


@worker.command("status")
def worker_status() -> None:
    """Show the database lease and worker process identity."""
    repo = JobRepository(resolve_database_path())
    _print_worker_status(repo.get_worker_runtime(), repo.db_path)


@worker.command("stop")
def worker_stop() -> None:
    """Request graceful worker shutdown through its durable command queue."""
    controller = _worker_controller(spawn_enabled=False)
    try:
        result = asyncio.run(controller.shutdown_worker())
    except Exception as error:
        raise click.ClickException(f"Worker did not stop cleanly: {error}") from error
    console.print("Worker is stopped" if not result.get("already_stopped") else "Worker was already stopped")
    console.print(f"Database: {controller.repo.db_path}")


@worker.command("restart")
def worker_restart() -> None:
    """Gracefully stop the worker, then start one replacement."""
    configure_logging()
    controller = _worker_controller()

    async def restart():
        await controller.shutdown_worker()
        return await controller.ensure_running(spawn_if_missing=True)

    try:
        runtime = asyncio.run(restart())
    except Exception as error:
        raise click.ClickException(f"Worker restart failed: {error}") from error
    _print_worker_status(runtime, controller.repo.db_path)


@cli.command()
@click.option("--host", default=None, help="API bind address (or HIREVIA_HOST)")
@click.option("--port", type=int, default=None, help="API port (or HIREVIA_PORT)")
def doctor(host: Optional[str], port: Optional[int]) -> None:
    """Read-only checks for the database, worker lease, and local API."""
    from hirevia.worker_process import get_process_executable_path

    database_path = resolve_database_path()
    console.print(f"Database: {database_path}")
    if not database_path.is_file():
        console.print("Database: MISSING (no file was created)")
    else:
        connection = None
        try:
            uri = f"{database_path.as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=5)
            integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            runtime = None
            if "worker_runtime" in tables:
                runtime = connection.execute(
                    "SELECT pid, status, monitor_status, started_at "
                    "FROM worker_runtime WHERE singleton = 1"
                ).fetchone()
            console.print(f"Database integrity: {integrity}")
            console.print(f"SQLite journal mode: {journal_mode}")
            if runtime and runtime[0] is not None:
                pid, status, monitor_status, started_at = runtime
                alive = JobRepository._pid_is_alive(pid)
                console.print(
                    f"Worker lease: PID {pid}, {status}, "
                    f"{'alive' if alive else 'stale PID'}"
                )
                if alive:
                    console.print(
                        f"Worker executable: {get_process_executable_path(pid) or 'not available'}"
                    )
                console.print(f"Monitor: {monitor_status}; uptime {_worker_uptime(started_at)}")
            else:
                console.print("Worker lease: no active worker")
        except (OSError, sqlite3.Error, TypeError) as error:
            console.print(f"Database check failed: {type(error).__name__}: {error}")
        finally:
            if connection is not None:
                connection.close()

    host = host or os.getenv("HIREVIA_HOST", "127.0.0.1")
    port = port or int(os.getenv("HIREVIA_PORT", "8000"))
    owner = _port_owner(host, port)
    if owner == "hirevia-web":
        console.print(f"API: listening at http://{host}:{port}")
    elif owner is None:
        console.print(f"API: not listening on {host}:{port} (port is available)")
    else:
        console.print(f"API port: occupied by {owner} on {host}:{port}")


@cli.command()
@click.option("--db-path", default=None, help="SQLite database path (or HIREVIA_DB_PATH)")
@click.option("--interval", default=30, help="Scan interval in minutes")
@click.option("--no-ai", is_flag=True, help="Run without AI matching")
@click.option("--verbose", is_flag=True, help="Show per-job and provider diagnostics")
def start(db_path: Optional[str], interval: int, no_ai: bool, verbose: bool):
    """Start continuous scanning."""
    configure_logging(verbose)
    console.print("[bold green]Starting Hirevia 2.0...[/bold green]")

    # Load config
    config = load_config()
    config.scan_interval_minutes = interval

    # Get API keys at runtime
    # Initialize components
    repo = JobRepository(Path(db_path) if db_path else None)
    ai_matcher = _build_ai_matcher(config, no_ai)
    if not ai_matcher and not no_ai:
        console.print("[yellow]No configured AI provider credentials - using deterministic scoring[/yellow]")
    console.print("[green]Configuration loaded[/green]")
    console.print(f"Enabled sources: {', '.join(config.sources.get_enabled_sources())}")
    console.print(f"Database: {repo.db_path}")
    console.print(f"Scan interval: {interval} minutes")
    console.print(f"AI matching: {'on (' + config.nvidia_model + ')' if ai_matcher else 'off'}")

    async def run_loop():
        while True:
            try:
                console.print("\n[cyan]Starting scan...[/cyan]")
                stats = await run_single_scan(config, repo, ai_matcher)
                _print_stats(stats)

                console.print(f"[yellow]Waiting {interval} minutes until next scan...[/yellow]")
                await asyncio.sleep(interval * 60)

            except KeyboardInterrupt:
                console.print("\n[red]Stopping...[/red]")
                break
            except Exception as e:
                logger.error(f"Scan loop error: {e}")
                console.print(f"[red]Error: {e}[/red]")
                await asyncio.sleep(60)
    asyncio.run(run_loop())



@cli.command()
@click.option("--db-path", default=None, help="SQLite database path (or HIREVIA_DB_PATH)")
@click.option("--no-ai", is_flag=True, help="Run without AI matching")
@click.option("--verbose", is_flag=True, help="Show per-job and provider diagnostics")
@click.option(
    "--diagnostic",
    is_flag=True,
    help="Print normalized Telegram jobs grouped by channel",
)
def scan(
    db_path: Optional[str],
    no_ai: bool,
    verbose: bool,
    diagnostic: bool,
):
    """Run a single scan cycle."""
    configure_logging(verbose)

    config = load_config()
    repo = JobRepository(Path(db_path) if db_path else None)
    ai_matcher = _build_ai_matcher(config, no_ai)
    if not ai_matcher and not no_ai:
        console.print("[yellow]No configured AI provider credentials - using deterministic scoring[/yellow]")
    ai_progress_reporter = None if diagnostic else _make_ai_progress_reporter(
        config,
        notifications_enabled=False
    )

    async def _run():
        return await run_single_scan(
            config,
            repo,
            ai_matcher,
            None,
            diagnostic_output=diagnostic,
            ai_progress_callback=ai_progress_reporter,
        )

    stats = asyncio.run(_run())
    _print_stats(stats, include_top_opportunities=diagnostic)
    if ai_progress_reporter:
        ai_progress_reporter(
            "complete",
            duration=stats.duration_seconds,
            notifications_enabled=False,
        )


@cli.command()
@click.option("--db-path", default=None, help="SQLite database path (or HIREVIA_DB_PATH)")
@click.option("--no-ai", is_flag=True, help="Run without AI matching")
@click.option("--verbose", is_flag=True, help="Show per-job and provider diagnostics")
def monitor(
    db_path: Optional[str],
    no_ai: bool,
    verbose: bool,
):
    """Continuously process new Telegram channel messages."""
    configure_logging(verbose)
    config = load_config()
    telegram_config = config.sources.sources.get("telegram")
    if telegram_config is None or not telegram_config.enabled:
        raise click.ClickException("Enable the Telegram source before starting monitor mode")

    monitor_config = config.model_copy(deep=True)
    for name, source_config in monitor_config.sources.sources.items():
        source_config.enabled = name == "telegram" and source_config.enabled

    repo = JobRepository(Path(db_path) if db_path else None)
    ai_matcher = _build_ai_matcher(config, no_ai)
    if not ai_matcher and not no_ai:
        console.print("[yellow]No configured AI provider credentials - using deterministic scoring[/yellow]")
    progress_reporter = _make_ai_progress_reporter(
        monitor_config,
        notifications_enabled=False,
        normal_monitor=not verbose,
    )
    pipeline = Pipeline(
        monitor_config,
        repo,
        ai_matcher,
        None,
        ai_progress_callback=progress_reporter,
    )
    source = pipeline.registry.get_source("telegram")
    if source is None or not callable(getattr(source, "start_continuous_monitoring", None)):
        raise click.ClickException("Telegram collector could not be initialized")

    async def _run_monitor():
        started_at = asyncio.get_running_loop().time()
        retry_delay = 2
        try:
            console.print("╔══════════════════════════════════════════════════════════╗")
            console.print("║                    HIREVIA 2.0                          ║")
            console.print("║              Autonomous Job Discovery Agent             ║")
            console.print("╚══════════════════════════════════════════════════════════╝")
            channel_count = await source.start_continuous_monitoring()
            console.print("✓ Telegram monitoring started")
            console.print(f"✓ Monitoring {channel_count} channels")
            console.print("✓ Persistent message tracking enabled")
            while True:
                stats = await pipeline.run_scan()
                if source.last_collection_failed or stats.jobs_failed:
                    logger.warning(
                        "[Telegram] Startup catch-up incomplete; retrying in %ss",
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                    continue
                if source.has_more_backfill:
                    continue
                break

            source.enable_event_mode()
            retry_delay = 2
            console.print("\nWaiting for new opportunities...")
            while True:
                stats = await pipeline.run_scan()
                if source.retry_uncommitted_event():
                    logger.warning(
                        "[Telegram] Message processing incomplete; retrying in %ss",
                        retry_delay,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 60)
                else:
                    retry_delay = 2
                    if (
                        stats.jobs_detected > stats.jobs_duplicate
                        and stats.jobs_failed == 0
                    ):
                        console.print("[Telegram] Processing completed")
        finally:
            await source.stop_continuous_monitoring()
            progress_reporter(
                "complete",
                duration=asyncio.get_running_loop().time() - started_at,
                notifications_enabled=False,
            )

    try:
        with _monitor_log_presentation(verbose):
            asyncio.run(_run_monitor())
    except KeyboardInterrupt:
        console.print("\n[Telegram] Continuous monitoring stopped")



@cli.command()
@click.option("--db-path", default=None, help="SQLite database path (or HIREVIA_DB_PATH)")
def stats(db_path: Optional[str]):
    """Show database statistics."""
    repo = JobRepository(Path(db_path) if db_path else None)
    stats = repo.get_stats()

    console.print("\n[bold]Database Statistics[/bold]")
    table = Table()
    table.add_column("Metric", style="cyan")
    table.add_column("Count", style="green")

    table.add_row("Total Jobs", str(stats.jobs_found))
    table.add_row("Open/Active", str(stats.jobs_verified))
    table.add_row("Expired", str(stats.jobs_expired))
    table.add_row("Notified", str(stats.jobs_notified))

    console.print(table)

    # By status
    status_counts = repo.count_by_status()
    if status_counts:
        console.print("\n[bold]By Status[/bold]")
        table2 = Table()
        table2.add_column("Status", style="cyan")
        table2.add_column("Count", style="green")
        for status, count in status_counts.items():
            table2.add_row(status, str(count))
        console.print(table2)

    # By source
    source_counts = repo.count_by_source()
    if source_counts:
        console.print("\n[bold]By Source[/bold]")
        table3 = Table()
        table3.add_column("Source", style="cyan")
        table3.add_column("Count", style="green")
        for source, count in source_counts.items():
            table3.add_row(source, str(count))
        console.print(table3)


@cli.command()
@click.option("--db-path", default=None, help="SQLite database path (or HIREVIA_DB_PATH)")
@click.option("--limit", default=20, help="Number of recent jobs to show")
def status(db_path: Optional[str], limit: int):
    """Show recent jobs and their status."""
    repo = JobRepository(Path(db_path) if db_path else None)
    jobs = repo.get_recent_jobs(hours=168, limit=limit)

    if not jobs:
        console.print("[yellow]No jobs found[/yellow]")
        return

    console.print(f"\n[bold]Recent Jobs (last 7 days, limit {limit})[/bold]")
    table = Table()
    table.add_column("Title", style="cyan", max_width=30)
    table.add_column("Company", style="green", max_width=20)
    table.add_column("Location", style="yellow", max_width=15)
    table.add_column("Source", style="blue", max_width=12)
    table.add_column("Status", style="magenta", max_width=10)
    table.add_column("Fresh", style="white", max_width=12)
    table.add_column("Link", style="white", max_width=11)
    table.add_column("Match %", style="white", max_width=8)

    for job in jobs:
        # The hybrid verdict is the score that matters; ai_decision rows written
        # before Phase 7 still display through Job.verdict().
        verdict = job.verdict()
        ai_score = f"{verdict.get('match_score', 0)}%" if verdict else ""

        # Freshness + link verdict are computed during the scan and stored on
        # the job; fall back to recomputing for rows written by older versions.
        fresh = job.raw_data.get("freshness_label") or freshness_label(
            job.posted_at or job.first_seen or job.detected_at
        )
        link = job.raw_data.get("application_url_quality") or classify_application_url(
            str(job.application_url) if job.application_url else None
        ).quality

        table.add_row(
            job.title[:30],
            job.company[:20],
            job.location[:15],
            job.source[:12],
            job.application_status.value,
            fresh,
            link,
            ai_score,
        )

    console.print(table)


@cli.command()
def sources():
    """List configured sources."""
    config = load_config()

    console.print("\n[bold]Configured Sources[/bold]")
    table = Table()
    table.add_column("Source", style="cyan")
    table.add_column("Enabled", style="green")

    for name, source_config in config.sources.sources.items():
        status = "Yes" if source_config.enabled else "No"
        table.add_row(name, status)

    console.print(table)


@cli.command()
def test():
    """Run self-tests."""
    console.print("[bold]Running self-tests...[/bold]")

    # Test config loading
    config = load_config()
    console.print(f"[green]+ Config loaded[/green] (sources: {len(config.sources.get_enabled_sources())})")

    # Test database
    repo = JobRepository(Path("test_hirevia.db"))
    console.print("[green]+ Database initialized[/green]")

    # Test deduplication
    from hirevia.core.dedup import compute_fingerprint
    from hirevia.core.models import Job
    from hirevia.core.freshness import calculate_expiry
    from datetime import datetime

    test_job = Job(
        source="test",
        source_job_id="123",
        title="Python Developer",
        company="Test Corp",
        location="Pune",
        description="Python FastAPI role",
        url="https://example.com/job/123",
    )
    fp1 = compute_fingerprint(test_job)
    fp2 = compute_fingerprint(test_job)
    assert fp1 == fp2, "Fingerprint should be deterministic"
    console.print("[green]+ Deduplication fingerprint deterministic[/green]")

    # Test freshness
    expiry = calculate_expiry(datetime.utcnow())
    assert expiry > datetime.utcnow()
    console.print("[green]+ Freshness calculation works[/green]")

    # Test filtering
    from hirevia.core.filtering import apply_filters
    result = apply_filters(test_job, config.filters)
    console.print(f"[green]+ Filtering works[/green] (passed: {result.passed})")

    # Cleanup
    Path("test_hirevia.db").unlink(missing_ok=True)

    console.print("\n[bold green]All tests passed![/bold green]")


@cli.command(name="telegram-login")
def telegram_login():
    """One-time Telegram login for the community collector (MTProto).

    Asks for phone + OTP once and stores a local session file, so scans never
    prompt again. The collector only READS the public channels listed in
    telegram.yaml - job alerts still go out over the separate Bot API.
    """
    from hirevia.sources.telegram_config import load_telegram_credentials, load_telegram_settings

    try:
        from telethon import TelegramClient
        from telethon.errors import SessionPasswordNeededError
    except ImportError:
        console.print("[red]Telethon is not installed - run: pip install telethon[/red]")
        sys.exit(1)

    credentials = load_telegram_credentials()
    if credentials is None:
        console.print("[red]Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env first[/red]")
        console.print("[dim]Free keys: my.telegram.org -> API development tools[/dim]")
        sys.exit(1)

    credentials.session_path.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"[dim]Session file: {credentials.session_path} (private - never commit it)[/dim]")

    async def _login():
        client = TelegramClient(
            str(credentials.session_path), credentials.api_id, credentials.api_hash
        )
        try:
            await client.connect()
            if await client.is_user_authorized():
                me = await client.get_me()
                console.print(f"[green]Already logged in[/green] as {me.first_name or me.username or 'user'}")
            else:
                phone = click.prompt("Phone number (international format, e.g. +91XXXXXXXXXX)")
                await client.send_code_request(phone)
                code = click.prompt("Login code (OTP from Telegram)", hide_input=True)
                try:
                    await client.sign_in(phone=phone, code=code.strip())
                except SessionPasswordNeededError:
                    password = click.prompt("2FA password", hide_input=True)
                    await client.sign_in(password=password)
                me = await client.get_me()
                console.print(f"[green]Logged in[/green] as {me.first_name or me.username or 'user'}")

            channels = load_telegram_settings().get_enabled_channels()
            console.print(
                f"Collector will monitor [bold]{len(channels)}[/bold] channel(s) from telegram.yaml"
            )
        finally:
            await client.disconnect()

    asyncio.run(_login())


@cli.command()
def version():
    """Show version."""
    console.print("Hirevia 2.0.0")


@cli.group()
def ai():
    """Inspect configured AI providers."""
    pass


@ai.command(name="status")
def ai_status():
    """Show provider configuration without contacting external services."""
    config = load_config()
    console.print("\n[bold]Hirevia AI Providers[/bold]")
    if not config.ai.enabled:
        console.print("AI matching: disabled")
        return
    for provider in sorted(provider_status(config), key=lambda item: not item["primary"]):
        role = " (primary)" if provider["primary"] else ""
        state = "configured" if provider["configured"] else "not configured"
        console.print(f"\n{provider['name'].upper()}{role}")
        console.print(f"  Model: {provider['model'] or 'not set'}")
        console.print(f"  Status: {state}")


def _persist_primary_provider(provider: str) -> None:
    """Persist the non-secret primary provider setting in the existing dotenv file."""
    env_path = Path.cwd() / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    setting = f"AI_PRIMARY_PROVIDER={provider}"
    updated = False
    for index, line in enumerate(lines):
        key = line.strip().removeprefix("export ").partition("=")[0].strip()
        if key == "AI_PRIMARY_PROVIDER":
            lines[index] = setting
            updated = True
    if not updated:
        lines.append(setting)
    content = "\n".join(lines) + "\n"
    temp_path = env_path.with_name(env_path.name + ".tmp")
    try:
        temp_path.write_text(content, encoding="utf-8")
        temp_path.replace(env_path)
    except OSError:
        temp_path.unlink(missing_ok=True)
        raise


@ai.command(name="set-primary")
@click.argument("provider")
def ai_set_primary(provider: str):
    """Set the primary AI provider after validating its configuration."""
    name = provider.strip().lower()
    config = load_config()
    previous = config.ai.primary
    if name not in {"nvidia", "groq", "ollama", "llamacpp"} or name not in config.ai.providers:
        console.print(f"[red]✗ Unknown AI provider: {provider}[/red]")
        raise click.ClickException("Supported providers: nvidia, groq, ollama, llamacpp")

    row = next((item for item in provider_status(config) if item["name"] == name), None)
    if row is None or not row["configured"]:
        console.print(f"[red]✗ Cannot set {name.title()} as primary[/red]")
        console.print(f"\n  Provider: {name}\n  Status: not configured\n\n  Configure the provider first.")
        raise click.exceptions.Exit(1)

    try:
        _persist_primary_provider(name)
    except OSError as error:
        raise click.ClickException(f"Could not persist primary provider ({type(error).__name__})") from error
    console.print("[green]✓ Primary AI provider changed[/green]")
    console.print(f"\n  Previous: {previous}\n  Primary : {name}\n  Model   : {row['model']}\n  Status  : configured")


@ai.command(name="test")
def ai_test():
    """Send a representative matching request to the configured primary provider."""
    config = load_config()
    primary = config.ai.primary
    row = next((item for item in provider_status(config) if item["name"] == primary), None)
    if row is None or not row["configured"]:
        console.print(f"[red]✗ Primary provider {primary} is not configured[/red]")
        raise click.exceptions.Exit(1)

    router = build_ai_provider_router(config)
    provider = router.providers.get(primary)
    if provider is None:
        console.print(f"[red]✗ Primary provider {primary} could not be initialized[/red]")
        raise click.exceptions.Exit(1)

    console.print("Testing primary provider...\n")
    console.print(f"Provider: {primary}\nModel: {row['model']}")
    if primary == "llamacpp":
        spec = config.ai.providers[primary]
        endpoint = os.getenv("LLAMACPP_BASE_URL", spec.base_url).rstrip("/")
        api_key = os.getenv(spec.api_key_env, "") if spec.api_key_env else ""
        success, detail = _test_llamacpp_provider(provider, endpoint, api_key, row["model"])
        if not success:
            console.print(f"[red]✗ {detail}[/red]")
            raise click.exceptions.Exit(1)
        console.print("\n[green]✓ Connection successful[/green]")
        console.print("[green]✓ Model responded[/green]")
        console.print("[green]✓ AI provider is ready[/green]")
        return

    job = Job(
        source="ai-health-check",
        source_job_id="synthetic-python-backend-role",
        title="Junior Python Backend Developer",
        company="Example Co",
        location="Remote",
        description="Fresher-friendly backend role using Python and SQL. 0-2 years.",
        url="https://example.com/jobs/ai-health-check",
        application_url="https://example.com/jobs/ai-health-check/apply",
    )
    profile = CandidateProfile(
        candidate_type="fresher",
        experience_years=0,
        target_max_experience_years=2,
        accept_fresher=True,
        skills=["Python", "SQL"],
        preferred_roles=["Python Developer", "Backend Developer"],
        preferred_locations=["Remote"],
    )
    matcher = AIMatcher(config, provider=provider)
    response = asyncio.run(matcher.match(job, profile, analyze(job, profile)))
    if response.degraded or response.status != "ok":
        reason = response.error or "model returned an unusable matching response"
        for spec in config.ai.providers.values():
            secret = os.getenv(spec.api_key_env) if spec.api_key_env else None
            if secret:
                reason = reason.replace(secret, "[redacted]")
        console.print(f"[red]✗ Provider test failed: {reason}[/red]")
        raise click.exceptions.Exit(1)

    console.print("\n[green]✓ Connection successful[/green]")
    console.print("[green]✓ Model responded[/green]")
    console.print("[green]✓ AI provider is ready[/green]")


def _test_llamacpp_provider(provider, endpoint: str, api_key: str, model: str) -> tuple[bool, str]:
    """Probe llama.cpp's models endpoint, then send one bounded non-streaming prompt."""
    models_url = endpoint + "/models"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = Request(models_url, headers=headers)
    try:
        with urlopen(request, timeout=3) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        if error.code in {401, 403}:
            return False, f"llama.cpp server rejected its API key (HTTP {error.code})\n  URL: {models_url}"
        return False, f"llama.cpp /v1/models returned HTTP {error.code}\n  URL: {models_url}"
    except URLError as error:
        reason = error.reason
        if isinstance(reason, (TimeoutError,)) or "timeout" in type(reason).__name__.casefold():
            return False, f"llama.cpp /v1/models request timed out\n  URL: {models_url}"
        winerror = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
        code = f" (WinError {winerror})" if winerror else ""
        return False, f"llama.cpp server unreachable{code}\n  URL: {endpoint}"
    except (TimeoutError, OSError):
        return False, f"llama.cpp server unreachable\n  URL: {endpoint}"
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return False, f"llama.cpp /v1/models returned malformed JSON\n  URL: {models_url}"

    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return False, f"llama.cpp /v1/models returned an unexpected response\n  URL: {models_url}"
    available_models = [
        str(item["id"]) for item in payload["data"]
        if isinstance(item, dict) and item.get("id")
    ]

    try:
        response = asyncio.run(provider.client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            temperature=0,
            max_tokens=4,
            stream=False,
        ))
    except Exception as error:
        error_type = type(error).__name__.casefold()
        status = getattr(error, "status_code", None)
        error_detail = str(error).casefold()
        body = getattr(error, "body", None)
        if body is not None:
            error_detail += f" {body}".casefold()
        if "timeout" in error_type or "timed out" in str(error).casefold():
            return False, f"llama.cpp model request timed out\n  Server reachable: yes\n  Model: {model}"
        if status in {400, 404}:
            available = ", ".join(available_models) or "not reported"
            model_error = "model" in error_detail and any(
                phrase in error_detail
                for phrase in ("not found", "not exist", "invalid", "unavailable", "unknown")
            )
            if model_error:
                return False, (
                    f"llama.cpp rejected the configured model (HTTP {status})\n"
                    f"  Model: {model}\n  Available models: {available}\n"
                    "  Set LLAMACPP_MODEL to a model ID accepted by this server."
                )
            if status == 404:
                return False, (
                    "llama.cpp server does not provide the expected chat-completions endpoint "
                    f"(HTTP 404)\n  Model: {model}"
                )
            return False, f"llama.cpp rejected the chat request (HTTP {status})\n  Check request compatibility and model configuration."
        if status in {401, 403}:
            return False, f"llama.cpp server rejected its API key (HTTP {status})\n  Server reachable: yes"
        if status is not None:
            return False, f"llama.cpp chat completion returned HTTP {status}\n  Server reachable: yes"
        if "connection" in error_type or "connect" in error_type:
            return False, f"llama.cpp chat endpoint connection failed\n  Server reachable: yes\n  Model: {model}"
        return False, f"llama.cpp chat request failed ({type(error).__name__})\n  Model: {model}"

    choices = getattr(response, "choices", None) or []
    message = getattr(choices[0], "message", None) if choices else None
    content = getattr(message, "content", None)
    if not isinstance(content, str) or content.strip() != "OK":
        return False, f"llama.cpp returned an unexpected health-check response\n  Model: {model}"
    return True, ""


def _print_stats(stats: ScanStats, include_top_opportunities: bool = True):
    noise_filtered = sum((
        stats.promotional_rejected,
        stats.educational_rejected,
        stats.informational_rejected,
        stats.spam_rejected,
        stats.selection_rejected,
        stats.aggregator_rejected,
    ))
    summary = Table.grid(padding=(0, 2))
    summary.add_column(style="cyan", justify="right")
    summary.add_column(style="white")
    summary.add_row("Telegram channels", str(stats.telegram_channels_scanned or stats.sources_scanned))
    summary.add_row("Messages scanned", str(stats.messages_checked))
    summary.add_row("Opportunities", str(stats.jobs_found))
    summary.add_row("Noise filtered", str(noise_filtered))
    summary.add_row("Profile matched", str(stats.jobs_matched))
    summary.add_row("High-quality", str(stats.jobs_notification_eligible))
    summary.add_row("AI provider", stats.ai_provider or "disabled")
    console.print(Panel(summary, title="Hirevia Scan", border_style="cyan"))

    if include_top_opportunities and stats.top_opportunities:
        console.print("\n[bold]TOP OPPORTUNITIES[/bold]")
        for index, item in enumerate(stats.top_opportunities, start=1):
            console.print(
                f"\n{index}. [bold]{item['title']}[/bold]\n"
                f"   Company : {item['company']}\n"
                f"   Match   : {item['match_score']}%\n"
                f"   Priority: {item['priority']}\n"
                f"   Location: {item['location']}\n"
                f"   Apply   : {item['application_url']}"
            )

    console.print(f"\n[green]Scan completed in {stats.duration_seconds:.1f}s[/green]")
    if stats.ai_failures:
        console.print(f"[yellow]AI degraded for {stats.ai_failures} job(s); deterministic fallback used[/yellow]")
    if stats.jobs_failed:
        console.print(f"[red]Jobs failed: {stats.jobs_failed}[/red]")

    if stats.errors:
        console.print("\n[red]Errors requiring attention:[/red]")
        for err in stats.errors[:5]:
            console.print(f"  - {err}")


def _make_ai_progress_reporter(
    config: Optional[Config] = None,
    notifications_enabled: bool = False,
    normal_monitor: bool = False,
):
    counts = {"processed": 0, "matched": 0, "not_matched": 0, "skipped": 0, "errors": 0}

    def review_priority(score):
        if score is None:
            return "No Priority"
        return classify_score(
            score,
            getattr(config, "priority_p1_min_score", 70),
            getattr(config, "priority_p2_min_score", 40),
            getattr(config, "priority_p3_min_score", 1),
        )

    def normalized_work_mode(job):
        raw_data = getattr(job, "raw_data", None) or {}
        value = raw_data.get("work_mode", raw_data.get("workMode"))
        if not value:
            return "Unknown"
        normalized = str(value).strip().casefold().replace("_", " ").replace("-", " ")
        return {
            "wfh": "WFH",
            "work from home": "WFH",
            "wfo": "WFO",
            "work from office": "WFO",
            "onsite": "WFO",
            "on site": "WFO",
            "hybrid": "Hybrid",
            "remote": "Remote",
        }.get(normalized, "Unknown")

    def display_source(job):
        raw_data = getattr(job, "raw_data", None) or {}
        source = raw_data.get("source_channel") or raw_data.get("source_name") or getattr(job, "source", None)
        if not source:
            return "Unknown"
        source = str(source).strip()
        if raw_data.get("source_channel"):
            return source
        return {
            "linkedin": "LinkedIn",
            "company_careers": "Company Careers",
        }.get(source.casefold(), source.replace("_", " ").title())

    def display_employment(job):
        employment = getattr(job, "employment_type", None)
        if employment is None:
            return "Not specified"
        value = getattr(employment, "value", employment)
        return {
            "full_time": "Full-time",
            "part_time": "Part-time",
            "contract": "Contract",
            "internship": "Internship",
            "temporary": "Temporary",
        }.get(str(value).casefold(), str(value).replace("_", " ").title())

    def build_card(index, total, job, status, score):
        experience = getattr(job, "experience", None) or "Not specified"
        experience = str(experience).replace("-", "–") if experience != "Not specified" else experience
        location = getattr(job, "location", None) or "Not specified"
        posted_at = getattr(job, "posted_at", None)
        posted = posted_at.strftime("%d %b %Y") if posted_at else "Unknown"
        application_url = getattr(job, "application_url", None)
        company = getattr(job, "company", None) or "Not specified"
        title = getattr(job, "title", None) or "Not specified"
        priority = review_priority(score)
        display_score = score if score is not None else "Not available"
        return (
            f"[{index}/{total}] {title} — {company}\n"
            f"DONE\n\n"
            f"Company     : {company}\n"
            f"Experience  : {experience}\n"
            f"Location    : {location}\n"
            f"Work Mode   : {normalized_work_mode(job)}\n"
            f"Employment  : {display_employment(job)}\n"
            f"Posted      : {posted}\n"
            f"Source      : {display_source(job)}\n\n"
            f"Apply       : {application_url or 'Not available'}\n\n"
            f"Status      : {status}\n"
            f"Score       : {display_score}\n"
            f"Priority    : {priority}\n"
            f"{'=' * 60}"
        )

    def report(event, **details):
        if event == "start":
            if normal_monitor:
                return
            console.print("\n" + "=" * 60)
            console.print("HIREVIA JOB PROCESSING")
            console.print("=" * 60)
            return

        if event == "processing":
            job = details["job"]
            console.print(
                f"\n[{details['index']}/{details['total']}] "
                f"[bold]{job.title}[/bold] — {job.company}\n"
                "    Processing..."
            )
            return

        if event == "sending_ai":
            console.print("    → Sending job to AI matcher...")
            return

        if event == "ai_response":
            console.print("    → AI response received")
            return

        if event == "ai_error":
            error_text = str(details["verdict"].ai_error or "").lower()
            if "timeout" in error_text or "timed out" in error_text:
                error_category = "timeout"
            elif "connect" in error_text or "network" in error_text:
                error_category = "connection"
            else:
                error_category = "provider failure"
            console.print(f"    → AI ERROR | {error_category}")
            return

        if event == "skipped":
            console.print("    → AI skipped by existing pipeline")
            return

        if event == "job_complete":
            job = details["job"]
            status = details["status"]
            score = details["score"]
            index = details["index"]
            total = details["total"]
            if status == "MATCHED":
                counts["matched"] += 1
            elif status == "NOT MATCHED":
                counts["not_matched"] += 1
            elif status == "AI ERROR":
                counts["errors"] += 1
            else:
                counts["skipped"] += 1
            counts["processed"] += 1
            console.print(build_card(index, total, job, status, score))
            console.print("-" * 60)
            return

        if event == "complete":
            if normal_monitor:
                return
            console.print("\n" + "=" * 60)
            console.print("HIREVIA SCAN COMPLETE")
            console.print("=" * 60)
            console.print(f"Processed       : {counts['processed']}")
            console.print(f"Matched         : {counts['matched']}")
            console.print(f"Not Matched     : {counts['not_matched']}")
            console.print(f"AI Skipped      : {counts['skipped']}")
            console.print(f"AI Errors       : {counts['errors']}")
            console.print(f"\nScan completed in: {details.get('duration', 0):.1f}s")
            console.print("=" * 60)

    return report


if __name__ == "__main__":
    cli()