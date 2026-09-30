"""CLI for Hirevia"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hirevia.ai.matcher import AIMatcher
from hirevia.ai.providers import provider_status
from hirevia.config import Config, load_config, get_telegram_token
from hirevia.core.models import ScanStats
from hirevia.core.pipeline import run_single_scan
from hirevia.core.verification import classify_application_url
from hirevia.core.freshness import freshness_label
from hirevia.notifications.telegram import TelegramNotifier
from hirevia.storage.state import JobRepository

console = Console()
logger = logging.getLogger(__name__)


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
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        ))
        root.addHandler(handler)
    for handler in root.handlers:
        handler.setLevel(level)


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

    if not token:
        try:
            token = (get_telegram_token() or "").strip()
        except (EOFError, KeyboardInterrupt):
            token = ""
    if not chat_id:
        try:
            chat_id = (click.prompt("Telegram chat ID", default=os.getenv("TELEGRAM_CHAT_ID", "")) or "").strip()
        except (EOFError, KeyboardInterrupt):
            chat_id = ""

    if not token or not chat_id:
        console.print(
            "[yellow]Notifications skipped - provide a bot token and chat id via "
            "--telegram-token/--chat-id or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID[/yellow]"
        )
        return None

    return TelegramNotifier(
        token,
        chat_id,
        retry_attempts=config.notification_retry_attempts,
        retry_delay=config.notification_retry_delay_seconds,
    )



@click.group()
@click.version_option(version="2.0.0", prog_name="hirevia")
def cli():
    """Hirevia 2.0 - Autonomous Job Discovery Agent"""
    pass


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
def scan(db_path: str, no_ai: bool, notify: bool, telegram_token: str, chat_id: str, verbose: bool):
    """Run a single scan cycle."""
    configure_logging(verbose)

    config = load_config()
    repo = JobRepository(Path(db_path))
    ai_matcher = _build_ai_matcher(config, no_ai)
    if not ai_matcher and not no_ai:
        console.print("[yellow]No configured AI provider credentials - using deterministic scoring[/yellow]")
    notifier = _build_notifier(config, telegram_token, chat_id) if notify else None

    async def _run():
        try:
            return await run_single_scan(config, repo, ai_matcher, notifier)
        finally:
            if notifier:
                await notifier.close()

    stats = asyncio.run(_run())
    _print_stats(stats)



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
    for provider in provider_status(config):
        role = " (primary)" if provider["primary"] else ""
        state = "configured" if provider["configured"] else "not configured"
        console.print(f"\n{provider['name'].upper()}{role}")
        console.print(f"  Model: {provider['model'] or 'not set'}")
        console.print(f"  Status: {state}")


def _print_stats(stats: ScanStats):
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

    if stats.top_opportunities:
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


if __name__ == "__main__":
    cli()