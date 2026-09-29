"""Telegram bot notification"""

import asyncio
import logging
from typing import Optional

import httpx

from hirevia.core.models import Job, AIResponse
from hirevia.notifications.base import Notifier

logger = logging.getLogger(__name__)


class TelegramNotifier(Notifier):
    name = "telegram"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        retry_attempts: int = 3,
        retry_delay: float = 2.0,
        timeout: float = 30.0,
    ):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.client = httpx.AsyncClient(timeout=timeout)
        self._retry_attempts = max(1, int(retry_attempts))
        self._retry_delay = max(0.0, float(retry_delay))

    async def send(self, job: Job, decision: AIResponse) -> bool:
        """Send one alert, retrying transient failures with backoff.

        Telegram's own rate limit (429) is respected through its retry_after
        hint; anything else just backs off. Permanent client errors (400/403 -
        bad chat id, bot blocked) fail fast so a scan is not held hostage.
        """
        message = self.format_message(job, decision)

        for attempt in range(self._retry_attempts):
            delay = self._retry_delay * (attempt + 1)
            try:
                resp = await self.client.post(
                    f"{self.base_url}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code == 200:
                    logger.info(f"Telegram notification sent for {job.title}")
                    return True

                logger.warning(f"Telegram API error: {resp.status_code} - {resp.text[:200]}")
                if resp.status_code in (400, 403, 404):
                    # Bad chat id / bot blocked / bad payload: retrying cannot help.
                    logger.error(
                        "Telegram rejected the request permanently - check TELEGRAM_CHAT_ID "
                        "and that the bot was started by the user (/start)"
                    )
                    return False
                if resp.status_code == 429:
                    delay = max(delay, _retry_after(resp))
            except Exception as e:
                logger.warning(f"Telegram send attempt {attempt + 1} failed: {e}")

            if attempt < self._retry_attempts - 1:
                await asyncio.sleep(delay)

        logger.error(f"Telegram notification failed after {self._retry_attempts} attempts")
        return False

    async def close(self):
        await self.client.aclose()


def _retry_after(response) -> float:
    """Seconds Telegram asks us to wait, capped so a scan cannot stall."""
    try:
        payload = response.json()
        seconds = float(payload["parameters"]["retry_after"])
    except Exception:
        seconds = float(response.headers.get("retry-after", 0) or 0)
    return min(seconds, 30.0)
