"""Base source interface"""

from abc import ABC, abstractmethod
from typing import List

from hirevia.core.models import Job


class JobSource(ABC):
    """Abstract base class for job sources."""

    name: str = "base"

    @abstractmethod
    async def collect(self) -> List[Job]:
        """Collect jobs from this source."""
        pass

    async def verify_application(self, job: Job) -> bool:
        """Verify if application is still open. Default: assume open."""
        return True