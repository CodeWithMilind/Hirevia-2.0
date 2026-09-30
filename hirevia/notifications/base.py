"""Notification base interface"""

from abc import ABC, abstractmethod
from html import escape
from typing import List, Optional

from hirevia.core.freshness import freshness_label
from hirevia.core.models import AIResponse, Job, MatchResult


class Notifier(ABC):
    """Abstract base class for notification channels."""

    name: str = "base"

    @abstractmethod
    async def send(self, job: Job, decision: AIResponse) -> bool:
        """Send notification for a matched job. Returns True if successful."""
        pass

    def format_message(self, job: Job, decision: AIResponse) -> str:
        """Render the alert.

        Fixed layout (Phase 6, enriched in Phase 7): priority banner, title,
        company, location, experience, match score, why it matches, freshness,
        application link, source. Identical across channels - only the transport
        differs.

        The *numbers* come from the hybrid verdict when the job carries one
        (Job.verdict_model), so the Telegram alert cannot disagree with the
        dashboard. The AIResponse argument stays: it is the Phase 6 contract, it
        carries the model's own reasons, and it is what a caller without a
        stored verdict can offer.
        """
        result = job.verdict_model()
        priority = result.priority if result else decision.priority
        score = result.match_score if result else decision.match_score

        priority_emoji = {"P1": "🚨", "P2": "⚡", "P3": "📢"}
        emoji = priority_emoji.get(priority.value, "📢")
        lines = [
            f"{emoji} {priority.value} - Job Match ({score}%)",
            "",
            f"Role: {job.title}",
            f"Company: {job.company}",
            f"Location: {job.location}",
            f"Experience: {job.experience or 'Not specified'}",
        ]

        if job.employment_type:
            lines.append(f"Type: {job.employment_type.value.replace('_', ' ').title()}")
        salary = (job.raw_data or {}).get("salary")
        if salary:
            lines.append(f"Compensation: {salary}")

        lines.append("")
        lines.append("Why it matches:")
        bullets = _why_bullets(decision, result)
        if bullets:
            lines.extend(f"- {bullet}" for bullet in bullets)
        else:
            lines.append("- Matched your profile preferences")

        lines.append("")
        lines.append(f"Freshness: {_freshness_text(job)}")
        lines.append(f"Application: {job.application_status.value.upper()}")
        lines.append(f"Apply: {job.application_url or job.url}")

        channels = (job.raw_data or {}).get("source_channels") or [
            (job.raw_data or {}).get("source_channel") or job.source
        ]
        lines.append(f"Source: {', '.join(str(c) for c in channels if c)}")

        return escape("\n".join(lines), quote=False)


def _why_bullets(decision: AIResponse, result: Optional[MatchResult] = None) -> List[str]:
    """The 'Why it matches' bullets, honest about where the score came from."""
    if result is None:  # pre-Phase-7 caller: only the model's own words exist
        return [reason for reason in decision.reasons[:4] if reason]

    bullets: List[str] = []
    if result.reason:
        bullets.append(result.reason)
    else:
        bullets.extend(reason for reason in decision.reasons[:3] if reason)

    if result.matched_skills:
        bullets.append("Matched: " + ", ".join(result.matched_skills[:6]))
    if result.missing_skills:
        bullets.append("Missing: " + ", ".join(result.missing_skills[:4]))

    if result.hard_reject:
        detail = (": " + "; ".join(result.reject_reasons[:2])) if result.reject_reasons else ""
        bullets.append(f"Vetoed by a hard rule{detail}")
    elif result.degraded:
        bullets.append("AI unavailable - score is the deterministic evidence only")
    elif result.deterministic_score is not None and result.ai_score is not None:
        bullets.append(f"Evidence {result.deterministic_score}% + AI {result.ai_score}%")

    return [bullet for bullet in bullets if bullet][:5]


def _freshness_text(job: Job) -> str:
    """Human-readable freshness, e.g. 'fresh (2h ago)'."""
    reference = job.posted_at or job.detected_at
    label = freshness_label(reference)
    if reference is None:
        return label.replace("_", " ")
    from datetime import datetime

    delta = datetime.utcnow() - reference
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        when = "just now"
    elif minutes < 60:
        when = f"{minutes}m ago"
    elif minutes < 24 * 60:
        when = f"{minutes // 60}h ago"
    else:
        when = f"{minutes // (60 * 24)}d ago"
    return f"{label.replace('_', ' ')} ({when})"
