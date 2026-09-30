"""Telegram community collector (MTProto via Telethon, strictly read-only).

This is the job-discovery side of Telegram. It is deliberately separate from
the notification side (hirevia/notifications/telegram.py, Bot API):

    Telegram public channels  ->  this module (MTProto user session, READ ONLY)
    Hirevia alerts            ->  TelegramNotifier (Bot API, WRITE ONLY)

The collector never sends messages, never joins channels, never prompts for
OTP (authentication happens once via `hirevia telegram-login`) and only reads
the public communities listed in telegram.yaml.

The SourceRegistry instantiates sources per scan, so the connected client and
per-channel message cursors live at module level and are reused across scans.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from hirevia.core.models import Job
from hirevia.sources.base import JobSource
from hirevia.sources.telegram_config import (
    TelegramAuthError,
    TelegramChannel,
    load_telegram_credentials,
    load_telegram_settings,
)
from hirevia.sources.telegram_extract import (
    IntentCategory,
    TgMessage,
    classify_job_intent,
    extract_links,
    message_to_job,
)

try:  # Telethon is imported lazily/optionally so the rest of Hirevia runs without it
    from telethon.errors import FloodWaitError, UsernameNotOccupiedError
except ImportError:  # pragma: no cover
    class FloodWaitError(Exception):  # type: ignore
        ...

    class UsernameNotOccupiedError(Exception):  # type: ignore
        ...

logger = logging.getLogger(__name__)
PREFIX = "[Telegram]"

# Shared across TelegramSource instances (registry re-creates them per scan).
_shared_client = None
_last_seen_ids: Dict[str, int] = {}

# Optional JobRepository injected by the pipeline (TelegramSource.bind_state)
# so cursors survive a restart instead of re-reading the whole channel.
_state_store = None
CURSOR_KEY = "last_message_id:{}"


def bind_state_store(repo) -> None:
    """Persist cursors in the jobs DB (pass None to stay memory-only)."""
    global _state_store
    _state_store = repo


def _cursor_for(channel: str) -> int:
    """Highest already-processed message id (memory first, then the DB)."""
    if channel in _last_seen_ids:
        return _last_seen_ids[channel]
    if _state_store is not None:
        try:
            stored = _state_store.get_source_state("telegram", CURSOR_KEY.format(channel))
        except Exception as e:  # noqa: BLE001 - a broken cursor store is not fatal
            logger.debug("%s cursor read failed for %s: %s", PREFIX, channel, e)
            stored = None
        try:
            value = int(stored) if stored else 0
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            _last_seen_ids[channel] = value
            logger.debug("%s Resumed %s from message %s", PREFIX, channel, value)
            return value
    return 0


def _remember(channel: str, message_id: int) -> None:
    """Advance the cursor (monotonic) and persist it when a store is bound."""
    if message_id <= _cursor_for(channel):
        return
    _last_seen_ids[channel] = message_id
    if _state_store is not None:
        try:
            _state_store.set_source_state("telegram", CURSOR_KEY.format(channel), message_id)
        except Exception as e:  # noqa: BLE001 - worst case: a restart re-reads messages
            logger.debug("%s cursor write failed for %s: %s", PREFIX, channel, e)



def _safe_err(exc: Exception) -> str:
    """Exception text, truncated. Telegram errors never carry our secrets,
    but keep this bounded and never log credentials from here."""
    return str(exc).replace("\n", " ")[:200]


def _to_naive_utc(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def message_links(message) -> List[str]:
    """Pull URLs from Telegram message entities (incl. hidden button/hyperlink
    URLs), falling back to regex over the text."""
    links: List[str] = []
    seen = set()

    def add(url):
        if url and url not in seen:
            seen.add(url)
            links.append(url)

    for ent in getattr(message, "entities", None) or []:
        add(getattr(ent, "url", None))

    from hirevia.sources.telegram_extract import extract_links

    for url in extract_links(getattr(message, "raw_text", None) or getattr(message, "message", None) or ""):
        add(url)
    return links


def to_tgmessage(message, channel_username: str) -> TgMessage:
    text = getattr(message, "raw_text", None) or getattr(message, "message", None) or ""
    date = getattr(message, "date", None) or datetime.now(timezone.utc)
    return TgMessage(
        channel=channel_username,
        message_id=int(getattr(message, "id", 0)),
        date=date,
        text=text,
        links=message_links(message),
    )


async def ensure_client():
    """Return a connected + authorized Telethon client (module singleton).

    Raises TelegramAuthError when credentials/session are missing or invalid.
    Never prompts for phone/OTP here - interactive auth lives in the
    `hirevia telegram-login` CLI command so the scanner stays non-blocking.
    """
    global _shared_client

    credentials = load_telegram_credentials()
    if credentials is None:
        raise TelegramAuthError(
            "Telegram credentials missing - set TELEGRAM_API_ID / TELEGRAM_API_HASH in .env"
        )

    if _shared_client is not None:
        try:
            if not _shared_client.is_connected():
                await _shared_client.connect()
            if await _shared_client.is_user_authorized():
                return _shared_client
            raise TelegramAuthError(
                "Telegram session is not authorized (expired?) - "
                "run 'python -m hirevia telegram-login' to re-authenticate"
            )
        except TelegramAuthError:
            raise
        except Exception:
            # Broken singleton - rebuild below.
            _shared_client = None

    try:
        from telethon import TelegramClient
    except ImportError as e:
        raise TelegramAuthError("Telethon is not installed - run: pip install telethon") from e

    logger.debug("%s Connecting...", PREFIX)
    credentials.session_path.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(credentials.session_path),
        credentials.api_id,
        credentials.api_hash,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            raise TelegramAuthError(
                "No valid Telegram session found - "
                "run 'python -m hirevia telegram-login' once to authenticate (phone + OTP)"
            )
    except TelegramAuthError:
        raise
    except Exception as e:
        raise TelegramAuthError(f"Telegram connection failed: {_safe_err(e)}") from e

    _shared_client = client
    logger.debug("%s Authentication successful", PREFIX)
    return client


async def close_shared_client():
    """Best-effort disconnect of the shared client (used by tests/shutdown)."""
    global _shared_client
    if _shared_client is not None:
        try:
            await _shared_client.disconnect()
        except Exception:
            pass
        _shared_client = None


class TelegramSource(JobSource):
    """Read-only collector over public Telegram channels listed in telegram.yaml."""

    name = "telegram"

    def __init__(self, diagnostic_output: bool = False):
        # Per-scan counters read by the pipeline to fill ScanStats
        # (Messages Checked / Jobs Detected / Jobs Normalized).
        self.diagnostic_output = diagnostic_output
        self.last_stats = {"messages_checked": 0, "jobs_detected": 0, "jobs_normalized": 0}
        self._intent_counts = {
            "promotional_rejected": 0,
            "educational_rejected": 0,
            "informational_rejected": 0,
            "spam_rejected": 0,
            "selection_rejected": 0,
            "aggregator_rejected": 0,
            "intent_ambiguous": 0,
        }

    def bind_state(self, repo) -> None:
        """Called by the Pipeline: store per-channel cursors in the jobs DB."""
        bind_state_store(repo)

    async def collect(self) -> List[Job]:
        settings = load_telegram_settings()
        if not settings.enabled:
            logger.debug("%s Disabled in telegram.yaml - skipping", PREFIX)
            return []

        channels = settings.get_enabled_channels()
        if not channels:
            logger.info("%s No enabled channels in telegram.yaml - skipping", PREFIX)
            return []

        logger.info("%s Monitoring %d channels", PREFIX, len(channels))

        try:
            client = await self._ensure_client()
        except TelegramAuthError as e:
            logger.warning("%s %s", PREFIX, e)
            return []
        except Exception as e:
            logger.error("%s Connection failed: %s", PREFIX, _safe_err(e))
            return []

        jobs: List[Job] = []
        unique: Dict[str, Job] = {}
        self._intent_counts = {
            "promotional_rejected": 0,
            "educational_rejected": 0,
            "informational_rejected": 0,
            "spam_rejected": 0,
            "selection_rejected": 0,
            "aggregator_rejected": 0,
            "intent_ambiguous": 0,
        }
        messages_checked = 0
        jobs_detected = 0
        jobs_printed = 0
        for channel in channels:
            if self.diagnostic_output:
                self._print_channel_header(channel.username)
            try:
                messages = await self._fetch_channel_messages(client, channel)
            except FloodWaitError as e:
                wait_seconds = min(int(getattr(e, "seconds", 30) or 30), 60)
                logger.warning(
                    "%s Rate limited - waiting %ss, will resume from last message",
                    PREFIX, wait_seconds,
                )
                await asyncio.sleep(wait_seconds)
                continue
            except (UsernameNotOccupiedError, ValueError) as e:
                logger.warning("%s Channel unavailable: %s (%s)", PREFIX, channel.username, _safe_err(e))
                continue
            except Exception as e:
                logger.error("%s Channel %s failed: %s - continuing", PREFIX, channel.username, _safe_err(e))
                continue

            messages_checked += len(messages)
            channel_jobs = self._normalize_messages(channel, messages)
            jobs_detected += len(channel_jobs)
            printed_ids = set()
            for job in channel_jobs:
                if self.diagnostic_output and job.source_job_id not in printed_ids:
                    self._print_diagnostic_job(job)
                    printed_ids.add(job.source_job_id)
                    jobs_printed += 1
                jobs.append(job)
                existing = unique.get(job.source_job_id)
                if existing is not None:
                    # Repost seen in another channel this scan: keep ONE job,
                    # accumulate every source channel as metadata.
                    channels_meta = existing.raw_data.setdefault(
                        "source_channels", [existing.raw_data.get("source_channel")]
                    )
                    other = job.raw_data.get("source_channel")
                    if other and other not in channels_meta:
                        channels_meta.append(other)
                else:
                    unique[job.source_job_id] = job

        self.last_stats = {
            "channels_scanned": len(channels),
            "messages_checked": messages_checked,
            "jobs_detected": jobs_detected,
            "jobs_normalized": len(unique),
            **self._intent_counts,
        }
        if self.diagnostic_output:
            self._print_diagnostic_summary(
                channels_scanned=len(channels),
                messages_checked=messages_checked,
                jobs_detected=jobs_detected,
                jobs_normalized=len(unique),
                jobs_printed=jobs_printed,
                duplicates_removed=jobs_detected - len(unique),
                intent_counts=self._intent_counts,
            )

        # Collapse reposts of the same opening inside one scan; cross-scan
        # reposts are absorbed later by repository dedup (source+source_job_id).
        return list(unique.values())

    @staticmethod
    def _print_channel_header(channel_name: str) -> None:
        print("=" * 60)
        print(f"📢 CHANNEL: {channel_name}")
        print("=" * 60)

    @staticmethod
    def _print_diagnostic_job(job: Job) -> None:
        def display(value, unavailable="Not specified"):
            text = str(value).strip() if value is not None else ""
            if not text or text.lower() in {"unknown", "n/a", "na", "none", "nil", "-"}:
                return unavailable
            return text

        print(f"Job Role: {display(job.title)}")
        print(f"Company Name: {display(job.company)}")
        print(f"Location: {display(job.location)}")
        print(f"Apply Link: {display(job.application_url, 'Not available')}")
        print(f"Experience Level: {display(job.experience)}")
        print("-" * 26)
        print()

    @staticmethod
    def _print_diagnostic_summary(
        *,
        channels_scanned: int,
        messages_checked: int,
        jobs_detected: int,
        jobs_normalized: int,
        jobs_printed: int,
        duplicates_removed: int,
        intent_counts: Optional[Dict[str, int]] = None,
    ) -> None:
        intent_counts = intent_counts or {}
        print("=" * 60)
        print("SCAN SUMMARY")
        print("=" * 60)
        print(f"Channels scanned: {channels_scanned}")
        print(f"Messages checked: {messages_checked}")
        print(f"Jobs detected: {jobs_detected}")
        print(f"Jobs normalized: {jobs_normalized}")
        print(f"Jobs printed: {jobs_printed}")
        print(f"Duplicates removed: {duplicates_removed}")
        print(f"Promotional rejected: {intent_counts.get('promotional_rejected', 0)}")
        print(f"Educational rejected: {intent_counts.get('educational_rejected', 0)}")
        print(f"Informational rejected: {intent_counts.get('informational_rejected', 0)}")
        print(f"Spam rejected: {intent_counts.get('spam_rejected', 0)}")
        print("=" * 60)

    # -- internals (monkeypatched in tests) ---------------------------------

    async def _ensure_client(self):
        return await ensure_client()

    async def _fetch_channel_messages(self, client, channel: TelegramChannel) -> List[TgMessage]:
        """Newer-than-cursor messages for one channel (or a lookback window on
        the first ever scan), newest first. Read-only iteration."""
        entity = await client.get_entity(channel.username)
        last_seen_id = _cursor_for(channel.username)

        collected: List[TgMessage] = []
        if last_seen_id:
            iterator = client.iter_messages(entity, offset_id=last_seen_id, limit=channel.max_messages)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=channel.lookback_hours)
            iterator = self._lookback_iterator(client, entity, channel, cutoff)

        async for message in iterator:
            collected.append(to_tgmessage(message, channel.username))

        if collected:
            _remember(channel.username, max(m.message_id for m in collected))
        return collected

    async def _lookback_iterator(self, client, entity, channel: TelegramChannel, cutoff):
        async for message in client.iter_messages(entity, limit=channel.max_messages):
            date = getattr(message, "date", None)
            if date is not None and date < cutoff:
                break
            yield message

    def _normalize_messages(self, channel: TelegramChannel, messages: List[TgMessage]) -> List[Job]:
        jobs: List[Job] = []
        for msg in messages:
            links = msg.links or extract_links(msg.text)
            decision = classify_job_intent(msg.text, links)
            if not decision.accepted:
                counter = {
                    IntentCategory.PROMOTIONAL: "promotional_rejected",
                    IntentCategory.EDUCATIONAL: "educational_rejected",
                    IntentCategory.INFORMATIONAL: "informational_rejected",
                    IntentCategory.SPAM: "spam_rejected",
                    IntentCategory.SELECTION_ANNOUNCEMENT: "selection_rejected",
                    IntentCategory.AGGREGATOR_LIST: "aggregator_rejected",
                }.get(decision.category)
                if counter:
                    self._intent_counts[counter] += 1
                logger.debug(
                    "%s [Filter] REJECTED %s: %s",
                    PREFIX, decision.category.value, decision.reason,
                )
                continue
            if decision.category is IntentCategory.UNKNOWN:
                self._intent_counts["intent_ambiguous"] += 1
            elif decision.category is IntentCategory.AMBIGUOUS:
                self._intent_counts["intent_ambiguous"] += 1
            else:
                logger.debug(
                    "%s [Filter] ACCEPTED %s: %s",
                    PREFIX, decision.category.value, decision.reason,
                )
            logger.debug(
                "%s New message detected in %s #%s", PREFIX, channel.username, msg.message_id
            )
            job = message_to_job(msg, decision)
            if job is None:
                continue
            logger.debug(
                "%s Job detected/normalized: %s @ %s | %s | freshness: %s",
                PREFIX, job.title, job.company, channel.username,
                job.raw_data.get("freshness_label", "?"),
            )
            jobs.append(job)
        if not messages:
            logger.debug("%s %s: no new messages", PREFIX, channel.username)
        return jobs

