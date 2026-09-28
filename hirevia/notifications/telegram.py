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

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        self.client = httpx.AsyncClient(timeout=30.0)
        self._retry_attempts = 3
        self._retry_delay = 2

    async def send(self, job: Job, decision: AIResponse) -> bool:
        message = self.format_message(job, decision)

        for attempt in range(self._retry_attempts):
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
                else:
                    logger.warning(f"Telegram API error: {resp.status_code} - {resp.text}")
            except Exception as e:
                logger.warning(f"Telegram send attempt {attempt + 1} failed: {e}")

            if attempt < self._retry_attempts - 1:
                await asyncio.sleep(self._retry_delay * (attempt + 1))

        logger.error(f"Telegram notification failed after {self._retry_attempts} attempts")
        return False

    async def close(self):
        await self.client.aclose()