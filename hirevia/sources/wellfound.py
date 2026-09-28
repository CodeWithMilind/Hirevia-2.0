"""Wellfound (AngelList) job source"""

import asyncio
from typing import List
import httpx

from hirevia.core.models import Job, EmploymentType
from hirevia.sources.base import JobSource


class WellfoundSource(JobSource):
    name = "wellfound"

    def __init__(self):
        self.base_url = "https://wellfound.com/api"
        self.client = httpx.AsyncClient(timeout=30.0)

    async def collect(self) -> List[Job]:
        jobs = []
        try:
            # Wellfound doesn't have a public API for job search
            # This is a placeholder - would need to implement scraping or use their GraphQL
            # For now, return empty list
            pass
        except Exception as e:
            print(f"Wellfound source error: {e}")
        finally:
            await self.client.aclose()
        return jobs