"""AI-powered job relevance matching"""

import json
from typing import Optional

from hirevia.ai.client import NVIDIAClient
from hirevia.config import Config
from hirevia.core.models import Job, CandidateProfile, AIResponse, Priority

# JSON Schema for structured output
AI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "priority": {"type": "string", "enum": ["P1", "P2", "P3"]},
        "match_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["relevant", "priority", "match_score", "reasons"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a job relevance evaluator for a job seeker.
Given a candidate profile and a job posting, decide if this job is genuinely relevant.

Consider:
- Skill match (required vs nice-to-have)
- Experience level alignment
- Role title match
- Location preference
- Career trajectory fit

Be selective. Only mark as relevant if the candidate has a realistic chance and genuine interest.

Respond with ONLY valid JSON matching the schema."""


def build_user_prompt(job: Job, profile: CandidateProfile) -> str:
    """Build the user prompt with job and profile details."""
    return f"""Candidate Profile:
Name: {profile.name}
Summary: {profile.summary}
Skills: {", ".join(profile.skills) if profile.skills else "Not specified"}
Experience: {profile.experience_years} years
Preferred Roles: {", ".join(profile.preferred_roles) if profile.preferred_roles else "Not specified"}
Preferred Locations: {", ".join(profile.preferred_locations) if profile.preferred_locations else "Not specified"}

Job Posting:
Title: {job.title}
Company: {job.company}
Location: {job.location}
Employment Type: {job.employment_type.value if job.employment_type else "Not specified"}
Experience Required: {job.experience or "Not specified"}
Description: {job.description[:2000]}

Evaluate relevance and respond with JSON."""


class AIMatcher:
    def __init__(self, config: Config, api_key: str):
        self.client = NVIDIAClient(config, api_key)

    async def match(self, job: Job, profile: CandidateProfile) -> AIResponse:
        """Match job against candidate profile using AI."""
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(job, profile)},
        ]

        try:
            result = await self.client.structured_completion(
                messages=messages,
                schema=AI_RESPONSE_SCHEMA,
                temperature=0.1,
                max_tokens=500,
                retries=1,
            )
            return AIResponse.from_json(result)
        except Exception as e:
            # Fail-safe: return neutral response
            return AIResponse(
                relevant=True,
                priority=Priority.P3,
                match_score=50,
                reasons=[f"AI evaluation failed: {str(e)[:100]}"],
            )