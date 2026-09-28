"""Notification base interface"""

from abc import ABC, abstractmethod

from hirevia.core.models import Job, AIResponse


class Notifier(ABC):
    """Abstract base class for notification channels."""

    name: str = "base"

    @abstractmethod
    async def send(self, job: Job, decision: AIResponse) -> bool:
        """Send notification for a matched job. Returns True if successful."""
        pass

    def format_message(self, job: Job, decision: AIResponse) -> str:
        """Format notification message."""
        priority_emoji = {"P1": "🚨", "P2": "⚡", "P3": "📢"}
        emoji = priority_emoji.get(decision.priority.value, "📢")

        lines = [
            f"{emoji} {decision.priority.value} MATCH ({decision.match_score}%)",
            "",
            f"{job.title}",
            f"🏢 Company: {job.company}",
            f"📍 Location: {job.location}",
        ]

        if job.employment_type:
            lines.append(f"💼 Type: {job.employment_type.value.replace('_', ' ').title()}")

        if job.experience:
            lines.append(f"🎓 Experience: {job.experience}")

        if decision.reasons:
            lines.append("")
            lines.append("Why it matches:")
            for reason in decision.reasons[:5]:
                lines.append(f"✓ {reason}")

        lines.append("")
        lines.append(f"Detected: Just now")
        lines.append(f"Application: {job.application_status.upper()}")

        if job.application_url:
            lines.append("")
            lines.append(f"Apply: {job.application_url}")

        return "\n".join(lines)