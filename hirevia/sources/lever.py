"""Lever job source - uses public API"""

from typing import List
import httpx

from hirevia.core.models import Job, EmploymentType
from hirevia.sources.base import JobSource


class LeverSource(JobSource):
    name = "lever"

    def __init__(self):
        self.base_url = "https://api.lever.co/v0/postings"
        self.client = httpx.AsyncClient(timeout=30.0)
        # Known company boards
        self.companies = [
            "netflix", "shopify", "dropbox", "twilio", "github",
            "atlassian", "zoom", "slack", "discord", "reddit",
            "quora", "pinterest", "lyft", "doordash", "instacart",
        ]

    async def collect(self) -> List[Job]:
        jobs = []
        for company in self.companies:
            try:
                company_jobs = await self._fetch_company(company)
                jobs.extend(company_jobs)
            except Exception as e:
                print(f"Lever company {company} error: {e}")
        await self.client.aclose()
        return jobs

    async def _fetch_company(self, company: str) -> List[Job]:
        url = f"{self.base_url}/{company}"
        jobs = []

        try:
            resp = await self.client.get(url, params={"mode": "json"})
            if resp.status_code != 200:
                return []

            data = resp.json()
            for item in data:
                job = self._parse_job(item, company)
                if job:
                    jobs.append(job)
        except Exception as e:
            print(f"Error fetching {company}: {e}")

        return jobs

    def _parse_job(self, item: dict, company: str) -> Job | None:
        try:
            # Location
            location = item.get("categories", {}).get("location", "") or "Remote"

            # Employment type
            emp_type = item.get("categories", {}).get("commitment", "").lower().replace(" ", "_")

            employment_type = None
            if emp_type:
                try:
                    employment_type = EmploymentType(emp_type)
                except ValueError:
                    employment_type = None

            return Job(
                source=self.name,
                source_job_id=item.get("id", ""),
                title=item.get("text", ""),
                company=company.capitalize(),
                location=location,
                description=item.get("descriptionPlain", "") or item.get("description", ""),
                url=item.get("hostedUrl", ""),
                application_url=item.get("applyUrl", "") or item.get("hostedUrl", ""),
                posted_at=None,
                employment_type=employment_type,
                skills=item.get("categories", {}).get("team", "").split(", ") if item.get("categories", {}).get("team") else [],
                raw_data=item,
            )
        except Exception as e:
            print(f"Error parsing Lever job: {e}")
            return None