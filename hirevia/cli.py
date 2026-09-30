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

from hirevia.ai.client import AIError
from hirevia.ai.matcher import AIMatcher
from hirevia.ai.providers import build_ai_provider_router, provider_status
from hirevia.config import Config, load_config, get_telegram_token
from hirevia.core.matching import analyze
from hirevia.core.models import CandidateProfile, Job, ScanStats
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
    for logger_name in ("httpx", "httpx2"):
        logging.getLogger(logger_name).setLevel(
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

    async def _run():
        try:
            return await run_single_scan(
                config,
                repo,
                ai_matcher,
                notifier,
                diagnostic_output=diagnostic,
                ai_progress_callback=None if diagnostic else _make_ai_progress_reporter(),
            )
        finally:
            if notifier:
                await notifier.close()

    stats = asyncio.run(_run())
    _print_stats(stats)
    if not diagnostic:
        _print_final_results(stats, notifications_enabled=bool(notify and notifier))



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


def _print_final_results(stats: ScanStats, notifications_enabled: bool) -> None:
    console.print("\n" + "=" * 60)
    console.print("FINAL HIREVIA RESULTS")
    console.print("=" * 60)
    console.print(f"\nFinal eligible jobs: {stats.jobs_notification_eligible}")

    if not stats.jobs_notification_eligible:
        console.print("No eligible jobs found.")
    else:
        for index, item in enumerate(stats.top_opportunities, start=1):
            console.print(
                f"\n{index}. [bold]{item['title']}[/bold] — {item['company']}\n"
                f"   Company: {item['company']}\n"
                f"   Location: {item['location'] or 'Not specified'}\n"
                f"   Match Score: {item['match_score']}\n"
                f"   Priority: {item['priority']}\n"
                f"   Application: {item['application_url'] or 'Not available'}"
            )

    if notifications_enabled:
        console.print("\nTelegram notifications: ENABLED")
    else:
        console.print("\nTelegram notifications: DISABLED (--no-notify)")
    console.print("=" * 60)


def _make_ai_progress_reporter():
    counts = {"processed": 0, "matched": 0, "not_matched": 0, "errors": 0}

    def report(event, **details):
        if event == "start":
            console.print("\n" + "=" * 60)
            console.print("AI MATCHING")
            console.print("=" * 60)
            return

        if event == "job":
            counts["processed"] += 1
            job = details["job"]
            verdict = details["verdict"]
            provider = str(details.get("provider") or "unknown").upper()
            model = str(details.get("model") or "unknown")
            console.print(
                f"\n[{details['index']}/{details['total']}] "
                f"[bold]{job.title}[/bold] — {job.company}"
            )
            console.print(f"        → {provider} | {model}")

            if verdict.ai_status == "failed":
                counts["errors"] += 1
                error_text = str(verdict.ai_error or "").lower()
                error_label = "timeout" if "timeout" in error_text or "timed out" in error_text else "request failed"
                console.print(f"        → AI ERROR | {error_label}")
            elif verdict.match:
                counts["matched"] += 1
                console.print(
                    f"        → MATCHED | Score: {verdict.match_score} | {verdict.priority.value}"
                )
            else:
                counts["not_matched"] += 1
                console.print(f"        → NOT MATCHED | Score: {verdict.match_score}")
            return

        if event == "complete":
            console.print("\n" + "=" * 60)
            console.print("AI MATCHING COMPLETE")
            console.print("=" * 60)
            console.print(f"Processed: {counts['processed']}")
            console.print(f"Matched: {counts['matched']}")
            console.print(f"Not matched: {counts['not_matched']}")
            console.print(f"AI errors: {counts['errors']}")
            console.print("=" * 60)

    return report


if __name__ == "__main__":
    cli()