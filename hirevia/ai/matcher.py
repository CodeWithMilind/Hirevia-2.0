"""AI-powered job relevance matching"""

import json
import logging
from typing import Optional

from hirevia.ai.client import NVIDIAClient
from hirevia.config import Config
from hirevia.core.models import Job, CandidateProfile, AIResponse, Priority

logger = logging.getLogger(__name__)

# JSON Schema for structured output. The pipeline reads these names; the
# parser additionally tolerates the common aliases models invent on their own
# ("match" for relevant, "score" for match_score, "reason" for reasons).
AI_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevant": {"type": "boolean"},
        "priority": {"type": "string", "enum": ["P1", "P2", "P3"]},
        "match_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasons": {"type": "array", "items": {"type": "string"}},
        "explanation": {"type": "string"},
    },
    "required": ["relevant", "priority", "match_score", "reasons"],
    "additionalProperties": True,
}

SYSTEM_PROMPT = """You are a job relevance evaluator for a job seeker.
Given a candidate profile and a job posting, decide if this job is genuinely relevant.

Consider:
- Skill match (required vs nice-to-have)
- Experience level alignment
- Role title match
- Location preference (remote/hybrid/city)
- Career trajectory fit

Rules:
- Be selective. Mark relevant=true only when the candidate has a realistic chance
  and genuine interest; otherwise relevant=false.
- priority: P1 = strong fit, apply now; P2 = decent fit; P3 = weak/marginal fit.
- match_score: integer 0-100.
- reasons: 1-5 short bullet strings (max 20 words each) explaining the decision.
- If the posting lacks information (company, role or apply link unclear), lower
  match_score and say so in reasons.
- Never invent skills, salary or company details that are not in the posting.

Respond with ONLY a JSON object like:
{"relevant": true, "priority": "P1", "match_score": 82, "reasons": ["Python + FastAPI match", "0-2 years fits experience"], "explanation": "one short sentence"}
No markdown, no text outside the JSON."""



def build_user_prompt(job: Job, profile: CandidateProfile) -> str:
    """Build the user prompt with job and profile details."""
    raw = job.raw_data or {}
    channels = raw.get("source_channels") or ([raw.get("source_channel")] if raw.get("source_channel") else [])
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
Salary/Stipend: {raw.get("salary") or "Not specified"}
Source: {job.source}{(" (" + ", ".join(str(c) for c in channels if c) + ")") if channels else ""}
Application link present: {"yes" if job.application_url else "no"}
Description: {job.description[:2000]}

Evaluate relevance and respond with JSON."""



class AIMatcher:
    def __init__(self, config: Config, api_key: str):
        self.client = NVIDIAClient(config, api_key)
        self.model = self.client.model

    async def match(self, job: Job, profile: CandidateProfile) -> AIResponse:
        """Match job against candidate profile using AI.

        Never raises: every failure mode (unavailable model, auth problem,
        timeout, rate limit, malformed JSON) returns the documented degraded
        response so one job - or a whole AI outage - cannot stop a scan.
        """
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
            response = AIResponse.from_json(result)
            response.raw_data = {"model": self.model, "payload": result if isinstance(result, dict) else {}}
            return response
        except Exception as e:  # noqa: BLE001 - AI must never break the pipeline
            logger.warning("[AI] match failed for %r (%s): %s", job.title, self.model, e)
            response = AIResponse.failure(e)
            response.raw_data = {"model": self.model, "payload": {}}
            return response
