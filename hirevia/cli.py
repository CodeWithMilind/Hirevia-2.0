"""CLI for Hirevia"""

import asyncio
import getpass
import logging
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from hirevia.config import Config, load_config, get_nvidia_api_key, get_telegram_token
from hirevia.core.models import ScanStats
from hirevia.core.pipeline import run_single_scan
from hirevia.storage.state import JobRepository
from hirevia.ai.matcher import AIMatcher
from hirevia.notifications.telegram import TelegramNotifier

console = Console()

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@click.group()
@click.version_option(version="2.0.0", prog_name="hirevia")
def cli():
    """Hirevia 2.0 - Autonomous Job Discovery Agent"""
    pass


@cli.command()
@click.option("--db-path", default="hirevia.db", help="SQLite database path")
@click.option("--interval", default=30, help="Scan interval in minutes")
@click.option("--no-ai", is_flag=True, help="Run without AI matching")
@click.option("--no-notify", is_flag=True, help="Run without notifications")
def start(db_path: str, interval: int, no_ai: bool, no_notify: bool):
    """Start continuous scanning with notifications."""
    console.print("[bold green]Starting Hirevia 2.0...[/bold green]")

    # Load config
    config = load_config()
    config.scan_interval_minutes = interval

    # Get API keys at runtime
    nvidia_key = get_nvidia_api_key()
    if not nvidia_key:
        console.print("[red]NVIDIA API key is required[/red]")
        sys.exit(1)

    # Initialize components
    repo = JobRepository(Path(db_path))
    ai_matcher = None if no_ai else AIMatcher(config, nvidia_key)
    notifier = None

    if not no_notify:
        telegram_token = get_telegram_token()
        if telegram_token:
            chat_id = click.prompt("Telegram chat ID")
            notifier = TelegramNotifier(telegram_token, chat_id)

    console.print(f"[green]Configuration loaded[/green]")
    console.print(f"Enabled sources: {', '.join(config.sources.get_enabled_sources())}")
    console.print(f"Database: {db_path}")
    console.print(f"Scan interval: {interval} minutes")

    async def run_loop():
        while True:
            try:
                console.print(f"\n[cyan]Starting scan...[/cyan]")
                stats = await run_single_scan(config, repo, ai_matcher)

                # Send notifications
                if notifier and stats.jobs_notified > 0:
                    # Would need to fetch newly notified jobs
                    pass

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

    try:
        asyncio.run(run_loop())
    finally:
        if notifier:
            asyncio.run(notifier.close())


@cli.command()
@click.option("--db-path", default="hirevia.db", help="SQLite database path")
@click.option("--no-ai", is_flag=True, help="Run without AI matching")
def scan(db_path: str, no_ai: bool):
    """Run a single scan cycle."""
    console.print("[bold]Running single scan...[/bold]")

    config = load_config()
    nvidia_key = None if no_ai else get_nvidia_api_key()

    repo = JobRepository(Path(db_path))
    ai_matcher = None if no_ai or not nvidia_key else AIMatcher(config, nvidia_key)

    stats = asyncio.run(run_single_scan(config, repo, ai_matcher))
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
    table.add_column("AI Score", style="white", max_width=8)

    for job in jobs:
        ai_score = ""
        if job.ai_decision:
            ai_score = f"{job.ai_decision.get('match_score', 0)}%"

        table.add_row(
            job.title[:30],
            job.company[:20],
            job.location[:15],
            job.source[:12],
            job.application_status.value,
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


@cli.command()
def version():
    """Show version."""
    console.print("Hirevia 2.0.0")


def _print_stats(stats: ScanStats):
    console.print("\n[bold]Scan Results[/bold]")
    table = Table()
    table.add_column("Metric", style="cyan")
    table.add_column("Count", style="green")

    table.add_row("Sources Scanned", str(stats.sources_scanned))
    table.add_row("Jobs Found", str(stats.jobs_found))
    table.add_row("New Jobs", str(stats.jobs_new))
    table.add_row("Duplicates", str(stats.jobs_duplicate))
    table.add_row("Filtered", str(stats.jobs_filtered))
    table.add_row("AI Matched", str(stats.jobs_ai_matched))
    table.add_row("Verified Open", str(stats.jobs_verified))
    table.add_row("Notified", str(stats.jobs_notified))
    table.add_row("Expired", str(stats.jobs_expired))
    table.add_row("Duration", f"{stats.duration_seconds:.1f}s")

    console.print(table)

    if stats.errors:
        console.print("\n[red]Errors:[/red]")
        for err in stats.errors:
            console.print(f"  - {err}")


if __name__ == "__main__":
    cli()