"""Candidate profile loading"""

from pathlib import Path
from typing import Optional

import yaml

from hirevia.core.models import CandidateProfile


def load_profile(profile_path: Optional[Path] = None) -> CandidateProfile:
    """Load candidate profile from YAML file."""
    if profile_path is None:
        profile_path = Path.cwd() / "profile.yaml"

    if not profile_path.exists():
        # Return default empty profile
        return CandidateProfile(
            name="Candidate",
            summary="",
            skills=[],
            experience_years=0,
        )

    with open(profile_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return CandidateProfile(**data)