"""AI-powered job matching (Phase 7: hybrid deterministic + semantic).

The model is asked for exactly one JSON object - never prose - so the pipeline
never has to guess what it meant. What it returns is one AIResponse, which the
matching engine blends with the deterministic evidence into a MatchResult.

The prompt deliberately includes the deterministic findings: the model's job is
semantic judgement (seniority, role intent, transferable skills), not
re-counting skills the code already counted. That grounding measurably reduces
invented matches, and the score is pinned near the evidence so an enthusiastic
model cannot outrank a thin posting.
"""

import logging
from typing import Optional

from hirevia.ai.client import NVIDIAClient
from hirevia.config import Config
from hirevia.core.matching import MatchEvidence, evidence_prompt_block
from hirevia.core.models import AIResponse, CandidateProfile, Job
from hirevia.profile.profile import profile_prompt_block

logger = logging.getLogger(__name__)

# The exact shape the parser reads. AIResponse.from_json additionally tolerates
# the aliases models invent ("relevant" for "match", "high" for "P1", ...).
MATCH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "match": {"type": "boolean"},
        "match_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "priority": {"type": "string", "enum": ["P1", "P2", "P3"]},
        "reason": {"type": "string"},
        "matched_skills": {"type": "array", "items": {"type": "string"}},
        "missing_skills": {"type": "array", "items": {"type": "string"}},
        "experience_match": {"type": "boolean"},
        "location_match": {"type": "boolean"},
        "role_match": {"type": "boolean"},
        "education_match": {"type": "boolean"},
        "employment_match": {"type": "boolean"},
    },
    "required": ["match", "match_score", "priority", "reason"],
    "additionalProperties": True,
}

SYSTEM_PROMPT = """You judge how relevant ONE job posting is for ONE candidate, as a recruiter screening for that candidate. You are protecting their time: every job you mark relevant becomes a notification.

Judge, in this order:
1. Role fit - is this actually the kind of role the candidate wants?
2. Skill fit - required vs nice-to-have. Use the deterministic evidence as the
   ground truth for which skills the posting mentions; do not invent skills and
   do not claim a skill match the evidence contradicts.
3. Experience/seniority fit, including whether the range is a real bar or boilerplate.
4. Career trajectory: would applying here make sense for this person next,
   excluding location / remote / employment-type fit.

Rules:
- Be selective. relevant=true only when there is a realistic chance AND genuine
  interest. Nearby-but-wrong (DevOps for a backend candidate, sales for an
  engineer) is false even if keywords overlap.
- A vague posting (no company, no clear role, no apply link) is not a strong match.
- Transferable skills earn partial credit, never full credit.
- Location, remote mode and employment type are already scored in the
    deterministic evidence. Do not use those signals to change relevance,
    match_score or priority; do not count them a second time.
- match_score: integer 0-100. Stay within ~10 points of the deterministic score
  unless the posting clearly contradicts it; explain a larger gap in reasons.
- priority: P1 apply now, P2 worth applying, P3 weak/marginal.
- reason: ONE sentence, max 220 characters, no flattery, no marketing tone,
  stating the concrete evidence for the verdict.
- matched_skills / missing_skills: canonical single names, only skills that
  actually appear (or are genuinely absent) - never filler.
- The *_match booleans: true/false only when the posting gives evidence; if the
  posting never says, omit the field.
- Salary and company benefits are out of scope unless the profile sets a range.

Reply with ONLY one JSON object, no markdown, no text before or after:
{"match": true, "match_score": 78, "priority": "P2", "reason": "...",
 "matched_skills": ["Python"], "missing_skills": ["Kubernetes"],
 "experience_match": true, "location_match": true, "role_match": true}"""


def build_user_prompt(
    job: Job,
    profile: CandidateProfile,
    evidence: Optional[MatchEvidence] = None,
) -> str:
    """The user turn: profile block, posting, deterministic findings, ask."""
    raw = job.raw_data or {}
    channels = raw.get("source_channels") or (
        [raw.get("source_channel")] if raw.get("source_channel") else []
    )
    salary = raw.get("salary") or "not stated"
    sources = ", ".join(str(c) for c in channels if c) or job.source

    blocks = [
        "CANDIDATE (single source of truth - judge against this, nothing else):",
        profile_prompt_block(profile),
        "",
        "JOB POSTING:",
        f"title: {job.title}",
        f"company: {job.company or 'not stated'}",
        f"location: {job.location or 'not stated'}",
        f"experience_required: {job.experience or 'not stated'}",
        f"employment_type: {job.employment_type.value if job.employment_type else 'not stated'}",
        f"salary_or_stipend: {salary}",
        f"source: {sources}",
        f"application_link_present: {'yes' if job.application_url else 'no'}",
        "description:",
        (job.description or "").strip()[:2200] or "(empty)",
    ]
    if evidence is not None:
        blocks += ["", "DETERMINISTIC EVIDENCE (computed from the posting text):", evidence_prompt_block(evidence)]
    blocks += ["", "Judge this posting now. Reply with the JSON object only."]
    return "\n".join(blocks)


class AIMatcher:
    """One job in, one structured verdict out. Never raises."""

    def __init__(self, config: Config, api_key: str):
        self.client = NVIDIAClient(config, api_key)
        self.model = self.client.model
        self.config = config

    async def match(
        self,
        job: Job,
        profile: CandidateProfile,
        evidence: Optional[MatchEvidence] = None,
    ) -> AIResponse:
        """Ask the model about one job.

        Every failure mode (no key, model retired, auth problem, timeout, rate
        limit, malformed JSON) returns the documented degraded response, so one
        job - or a total AI outage - cannot stop a scan. The pipeline then falls
        back to the deterministic score with a capped confidence.
        """
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(job, profile, evidence)},
        ]
        try:
            result = await self.client.structured_completion(
                messages=messages,
                schema=MATCH_RESPONSE_SCHEMA,
                temperature=0.1,
                max_tokens=600,
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

