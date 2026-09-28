"""Ashby job source - uses public API"""

from typing import List
import httpx

from hirevia.core.models import Job, EmploymentType
from hirevia.sources.base import JobSource


class AshbySource(JobSource):
    name = "ashby"

    def __init__(self):
        self.base_url = "https://jobs.ashbyhq.com/api/non-user-graphql"
        self.client = httpx.AsyncClient(timeout=30.0)
        # Known organizations
        self.organizations = [
            "rippling", "scale", "anthropic", "openai", "cohere",
            "perplexity", "character-ai", "adept", "inflection", "midjourney",
        ]

    async def collect(self) -> List[Job]:
        jobs = []
        for org in self.organizations:
            try:
                org_jobs = await self._fetch_org(org)
                jobs.extend(org_jobs)
            except Exception as e:
                print(f"Ashby org {org} error: {e}")
        await self.client.aclose()
        return jobs

    async def _fetch_org(self, org: str) -> List[Job]:
        # Ashby uses GraphQL
        query = """
        query JobBoard($organizationHostedJobsPageName: String!) {
            jobBoard: jobBoardWithHostedJobsPageName(organizationHostedJobsPageName: $organizationHostedJobsPageName) {
                jobPostings(first: 100) {
                    edges {
                        node {
                            id
                            title
                            locationName
                            employmentType
                            description
                            applyUrl
                            department
                            team
                            isListed
                        }
                    }
                }
            }
        }
        """

        jobs = []
        try:
            resp = await self.client.post(
                self.base_url,
                json={"query": query, "variables": {"organizationHostedJobsPageName": org}},
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            edges = data.get("data", {}).get("jobBoard", {}).get("jobPostings", {}).get("edges", [])

            for edge in edges:
                node = edge.get("node", {})
                if not node.get("isListed", True):
                    continue

                job = self._parse_job(node, org)
                if job:
                    jobs.append(job)
        except Exception as e:
            print(f"Error fetching {org}: {e}")

        return jobs

    def _parse_job(self, node: dict, org: str) -> Job | None:
        try:
            emp_type = node.get("employmentType", "").lower().replace(" ", "_")

            employment_type = None
            if emp_type:
                try:
                    employment_type = EmploymentType(emp_type)
                except ValueError:
                    employment_type = None

            return Job(
                source=self.name,
                source_job_id=node.get("id", ""),
                title=node.get("title", ""),
                company=org.capitalize(),
                location=node.get("locationName", "") or "Remote",
                description=node.get("description", ""),
                url=node.get("applyUrl", ""),
                application_url=node.get("applyUrl", ""),
                posted_at=None,
                employment_type=employment_type,
                skills=[],
                raw_data=node,
            )
        except Exception as e:
            print(f"Error parsing Ashby job: {e}")
            return None