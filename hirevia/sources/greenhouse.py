"""Greenhouse job source - uses public API"""

import asyncio
from typing import List
import httpx

from hirevia.core.models import Job, EmploymentType
from hirevia.sources.base import JobSource


class GreenhouseSource(JobSource):
    name = "greenhouse"

    def __init__(self):
        self.base_url = "https://boards-api.greenhouse.io/v1/boards"
        self.client = httpx.AsyncClient(timeout=30.0)
        # Known company boards - in practice, you'd discover these
        self.boards = [
            "stripe", "airbnb", "coinbase", "databricks", "robinhood",
            "plaid", "brex", "ramp", "mercury", "notion",
            "figma", "linear", "vercel", "supabase", "planetscale",
        ]

    async def collect(self) -> List[Job]:
        jobs = []
        for board in self.boards:
            try:
                board_jobs = await self._fetch_board(board)
                jobs.extend(board_jobs)
                await asyncio.sleep(0.5)  # Rate limiting
            except Exception as e:
                print(f"Greenhouse board {board} error: {e}")
        await self.client.aclose()
        return jobs

    async def _fetch_board(self, board_name: str) -> List[Job]:
        url = f"{self.base_url}/{board_name}/jobs"
        jobs = []

        try:
            resp = await self.client.get(url, params={"content": "true"})
            if resp.status_code != 200:
                return []

            data = resp.json()
            for item in data.get("jobs", []):
                job = self._parse_job(item, board_name)
                if job:
                    jobs.append(job)
        except Exception as e:
            print(f"Error fetching {board_name}: {e}")

        return jobs

    def _parse_job(self, item: dict, board_name: str) -> Job | None:
        try:
            # Extract location
            location_parts = []
            if item.get("location"):
                location_parts.append(item["location"].get("name", ""))
            location = ", ".join(filter(None, location_parts))

            # Extract employment type
            emp_type = None
            if item.get("metadata"):
                for meta in item["metadata"]:
                    if meta.get("name") == "Employment Type":
                        emp_type = meta.get("value", "").lower().replace(" ", "_")
                        break

            # Safely convert to enum
            employment_type = None
            if emp_type:
                try:
                    employment_type = EmploymentType(emp_type)
                except ValueError:
                    employment_type = None

            return Job(
                source=self.name,
                source_job_id=str(item.get("id", "")),
                title=item.get("title", ""),
                company=board_name.capitalize(),
                location=location or "Remote",
                description=item.get("content", "") or item.get("description", ""),
                url=item.get("absolute_url", ""),
                application_url=item.get("absolute_url", ""),
                posted_at=None,  # Greenhouse doesn't always provide this
                employment_type=employment_type,
                skills=[],
                raw_data=item,
            )
        except Exception as e:
            print(f"Error parsing Greenhouse job: {e}")
            return None