"""Notification base interface"""

from abc import ABC, abstractmethod
from html import escape

from hirevia.core.freshness import freshness_label
from hirevia.core.models import AIResponse, Job


class Notifier(ABC):
    """Abstract base class for notification channels."""

    name: str = "base"

    @abstractmethod
    async def send(self, job: Job, decision: AIResponse) -> bool:
        """Send notification for a matched job. Returns True if successful."""
        pass

    def format_message(self, job: Job, decision: AIResponse) -> str:
        """Render the alert.

        Fixed layout (Phase 6): priority banner, title, company, location,
        experience, match score, why it matches, freshness, application link,
        source. Identical across channels - only the transport differs.
        """
        priority_emoji = {"P1": "🚨", "P2": "⚡", "P3": "📢"}
        emoji = priority_emoji.get(decision.priority.value, "📢")
        lines = [
            f"{emoji} {decision.priority.value} - Job Match ({decision.match_score}%)",
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
        if decision.reasons:
            lines.extend(f"- {reason}" for reason in decision.reasons[:4])
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
