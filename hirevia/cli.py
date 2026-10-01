"""CLI for Hirevia"""

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import sys
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
from hirevia.config import Config, load_config, get_telegram_token
from hirevia.core.matching import analyze
from hirevia.core.models import CandidateProfile, Job, ScanStats
from hirevia.core.pipeline import Pipeline, run_single_scan
from hirevia.core.verification import classify_application_url
from hirevia.core.freshness import freshness_label
from hirevia.notifications.telegram import PriorityTelegramNotifier, TelegramNotifier
from hirevia.storage.state import JobRepository
from hirevia.web.state import initialize_web_state

console = Console()
logger = logging.getLogger(__name__)
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s | %(message)s"


def _redact_log_text(message: str) -> str:
    """Remove configured secret values and secret-shaped assignments."""
    secret_markers = ("API_KEY", "API_HASH", "BOT_TOKEN", "ADMIN_TOKEN", "PASSWORD", "OTP", "SECRET", "SESSION")
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
        r"(?i)\b(api[_-]?key|api[_-]?hash|bot[_-]?token|admin[_-]?token|password|otp|secret|session)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2[REDACTED]",
        message,
    )


class _SecretRedactingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return _redact_log_text(super().format(record))


def configure_logging(verbose: bool = False) -> None:
    """Configure the application logger without stacking console handlers."""
    configured_level = os.getenv("HIREVIA_LOG_LEVEL", "").strip().upper()
    level = getattr(logging, configured_level, None) if configured_level else None
    if not isinstance(level, int):
        level = logging.DEBUG if verbose else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)
    logging.getLogger("telethon").setLevel(
        logging.DEBUG if level <= logging.DEBUG else logging.WARNING
    )
    for logger_name in ("httpx", "httpx2"):
        logging.getLogger(logger_name).setLevel(
            logging.DEBUG if level <= logging.DEBUG else logging.WARNING
        )
    stream_handlers = [
        handler for handler in root.handlers
        if isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
    ]
    if not stream_handlers:
        stream_handlers = [logging.StreamHandler()]
        root.addHandler(stream_handlers[0])
    for handler in root.handlers:
        handler.setLevel(level)
    for handler in stream_handlers:
        handler.setFormatter(_SecretRedactingFormatter(LOG_FORMAT))


class _MonitorLogFilter(logging.Filter):
    """Translate selected internal monitor events into concise console lines."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return False

        display = None
        if record.name == "hirevia.sources.telegram_source":
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

        if display:
            if not getattr(record, "_hirevia_monitor_rendered", False):
                record._hirevia_monitor_rendered = True
                console.print(display)
        return False


@contextmanager
def _monitor_log_presentation(verbose: bool):
    """Silence monitor internals in normal mode, restoring logging afterward."""
    if verbose:
        yield
        return

    root = logging.getLogger()
    handlers = list(root.handlers)
    handler_states = [(handler, handler.level) for handler in handlers]
    monitor_loggers = [
        logging.getLogger("hirevia.core.pipeline"),
        logging.getLogger("hirevia.sources.telegram_source"),
    ]
    logger_states = [(item, item.level) for item in monitor_loggers]
    output_filter = _MonitorLogFilter()
    for handler in handlers:
        handler.addFilter(output_filter)
        handler.setLevel(logging.DEBUG)
    for item in monitor_loggers:
        item.setLevel(logging.DEBUG)
    try:
        yield
    finally:
        for handler, level in handler_states:
            handler.removeFilter(output_filter)
            handler.setLevel(level)
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


def _build_notifier(config: Config, token: Optional[str], chat_id: Optional[str]):
    """Build a TelegramNotifier from flags/env, prompting only when needed.

    Returns None (with a hint) instead of crashing when credentials are
    missing: a scan without alerts is still a useful scan.
    """
    token = (token or os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (chat_id or os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    from hirevia.sources.telegram_config import load_telegram_settings

    destinations = load_telegram_settings().community_destinations

    if not token and not destinations:
        try:
            token = (get_telegram_token() or "").strip()
        except (EOFError, KeyboardInterrupt):
            token = ""
    if not chat_id and not destinations:
        try:
            chat_id = (click.prompt("Telegram chat ID", default=os.getenv("TELEGRAM_CHAT_ID", "")) or "").strip()
        except (EOFError, KeyboardInterrupt):
            chat_id = ""

    if not token or not (chat_id or destinations):
        console.print(
            "[yellow]Notifications skipped - provide a bot token and chat id via "
            "--telegram-token/--chat-id or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID[/yellow]"
        )
        return None

    if destinations:
        return PriorityTelegramNotifier(
            token,
            destinations,
            retry_attempts=config.notification_retry_attempts,
            retry_delay=config.notification_retry_delay_seconds,
        )

    return TelegramNotifier(
        token,
        chat_id,
        retry_attempts=config.notification_retry_attempts,
        retry_delay=config.notification_retry_delay_seconds,
    )



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

    repo = JobRepository(Path(os.getenv("HIREVIA_DB_PATH", "hirevia.db")))
    matcher = _build_ai_matcher(config)
    state = initialize_web_state(config, repo, matcher)
    state.service_notifier_configured = bool(
        os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")
    )

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


@cli.command()
@click.option("--db-path", default="hirevia.db", help="SQLite database path")
@click.option("--interval", default=30, help="Scan interval in minutes")
@click.option("--no-ai", is_flag=True, help="Run without AI matching")
@click.option("--notify/--no-notify", "notify", default=True, help="Send Telegram alerts")
@click.option("--telegram-token", default=None, help="Telegram bot token (or TELEGRAM_BOT_TOKEN)")
@click.option("--chat-id", default=None, help="Telegram chat id (or TELEGRAM_CHAT_ID)")
@click.option("--verbose", is_flag=True, help="Show per-job and provider diagnostics")
def start(db_path: str, interval: int, no_ai: bool, notify: bool, telegram_token: str, chat_id: str, verbose: bool):
    """Start continuous scanning with notifications."""
    configure_logging(verbose)
    console.print("[bold green]Starting Hirevia 2.0...[/bold green]")

    # Load config
    config = load_config()
    config.scan_interval_minutes = interval

    # Get API keys at runtime
    # Initialize components
    repo = JobRepository(Path(db_path))
    ai_matcher = _build_ai_matcher(config, no_ai)
    if not ai_matcher and not no_ai:
        console.print("[yellow]No configured AI provider credentials - using deterministic scoring[/yellow]")
    notifier = _build_notifier(config, telegram_token, chat_id) if notify else None

    console.print("[green]Configuration loaded[/green]")
    console.print(f"Enabled sources: {', '.join(config.sources.get_enabled_sources())}")
    console.print(f"Database: {db_path}")
    console.print(f"Scan interval: {interval} minutes")
    console.print(f"AI matching: {'on (' + config.nvidia_model + ')' if ai_matcher else 'off'}")
    console.print(f"Notifications: {notifier.name if notifier else 'off'}")

    async def run_loop():
        try:
            while True:
                try:
                    console.print("\n[cyan]Starting scan...[/cyan]")
                    # The pipeline owns notifications now: it alerts only for
                    # jobs that passed dedup, filters, AI and link checks.
                    stats = await run_single_scan(config, repo, ai_matcher, notifier)
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
        finally:
            if notifier:
                await notifier.close()

    asyncio.run(run_loop())



@cli.command()
@click.option("--db-path", default="hirevia.db", help="SQLite database path")
@click.option("--no-ai", is_flag=True, help="Run without AI matching")
@click.option("--notify/--no-notify", "notify", default=False, help="Also send Telegram alerts")
@click.option("--telegram-token", default=None, help="Telegram bot token (or TELEGRAM_BOT_TOKEN)")
@click.option("--chat-id", default=None, help="Telegram chat id (or TELEGRAM_CHAT_ID)")
@click.option("--verbose", is_flag=True, help="Show per-job and provider diagnostics")
@click.option(
    "--diagnostic",
    is_flag=True,
    help="Print normalized Telegram jobs grouped by channel; suppress notifications",
)
def scan(
    db_path: str,
    no_ai: bool,
    notify: bool,
    telegram_token: str,
    chat_id: str,
    verbose: bool,
    diagnostic: bool,
):
    """Run a single scan cycle."""
    configure_logging(verbose)

    config = load_config()
    repo = JobRepository(Path(db_path))
    ai_matcher = _build_ai_matcher(config, no_ai)
    if not ai_matcher and not no_ai:
        console.print("[yellow]No configured AI provider credentials - using deterministic scoring[/yellow]")
    notifier = _build_notifier(config, telegram_token, chat_id) if notify and not diagnostic else None
    ai_progress_reporter = None if diagnostic else _make_ai_progress_reporter(
        notifications_enabled=bool(notify and notifier)
    )

    async def _run():
        try:
            return await run_single_scan(
                config,
                repo,
                ai_matcher,
                notifier,
                diagnostic_output=diagnostic,
                ai_progress_callback=ai_progress_reporter,
            )
        finally:
            if notifier:
                await notifier.close()

    stats = asyncio.run(_run())
    _print_stats(stats, include_top_opportunities=diagnostic)
    if ai_progress_reporter:
        ai_progress_reporter(
            "complete",
            duration=stats.duration_seconds,
            notifications_enabled=bool(notify and notifier),
        )


@cli.command()
@click.option("--db-path", default="hirevia.db", help="SQLite database path")
@click.option("--no-ai", is_flag=True, help="Run without AI matching")
@click.option("--notify/--no-notify", "notify", default=False, help="Send Telegram alerts")
@click.option("--telegram-token", default=None, help="Telegram bot token (or TELEGRAM_BOT_TOKEN)")
@click.option("--chat-id", default=None, help="Telegram chat id (or TELEGRAM_CHAT_ID)")
@click.option("--verbose", is_flag=True, help="Show per-job and provider diagnostics")
def monitor(
    db_path: str,
    no_ai: bool,
    notify: bool,
    telegram_token: str,
    chat_id: str,
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

    repo = JobRepository(Path(db_path))
    ai_matcher = _build_ai_matcher(config, no_ai)
    if not ai_matcher and not no_ai:
        console.print("[yellow]No configured AI provider credentials - using deterministic scoring[/yellow]")
    notifier = _build_notifier(config, telegram_token, chat_id) if notify else None
    progress_reporter = _make_ai_progress_reporter(
        notifications_enabled=bool(notify and notifier),
        normal_monitor=not verbose,
    )
    pipeline = Pipeline(
        monitor_config,
        repo,
        ai_matcher,
        notifier,
        ai_progress_callback=progress_reporter,
    )
    source = pipeline.registry.get_source("telegram")
    if source is None or not callable(getattr(source, "start_continuous_monitoring", None)):
        if notifier:
            asyncio.run(notifier.close())
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
            if not notify or not notifier:
                console.print("Telegram notifications: DISABLED (--no-notify)")
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
            if notifier:
                await notifier.close()
            progress_reporter(
                "complete",
                duration=asyncio.get_running_loop().time() - started_at,
                notifications_enabled=bool(notify and notifier),
            )

    try:
        with _monitor_log_presentation(verbose):
            asyncio.run(_run_monitor())
    except KeyboardInterrupt:
        console.print("\n[Telegram] Continuous monitoring stopped")



@cli.command()
@click.option("--db-path", default="hirevia.db", help="SQLite database path")
def stats(db_path: str):
    """Show database statistics."""
    repo = JobRepository(Path(db_path))
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
@click.option("--db-path", default="hirevia.db", help="SQLite database path")
@click.option("--limit", default=20, help="Number of recent jobs to show")
def status(db_path: str, limit: int):
    """Show recent jobs and their status."""
    repo = JobRepository(Path(db_path))
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
    table.add_column("AI Score", style="white", max_width=8)

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
    notifications_enabled: bool = False,
    normal_monitor: bool = False,
):
    counts = {"processed": 0, "matched": 0, "not_matched": 0, "skipped": 0, "errors": 0}

    def review_priority(score):
        if score is None:
            return "No Priority"
        if score > 80:
            return "P1"
        if score > 50:
            return "P2"
        if score > 20:
            return "P3"
        return "No Priority"

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
            if details.get("notifications_enabled", notifications_enabled):
                console.print("Telegram notifications: ENABLED")
            else:
                console.print("Telegram notifications: DISABLED (--no-notify)")
            console.print("=" * 60)

    return report


if __name__ == "__main__":
    cli()