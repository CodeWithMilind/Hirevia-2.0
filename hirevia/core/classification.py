"""Shared score classification cutoffs for matching surfaces."""

DEFAULT_P1_THRESHOLD = 70
DEFAULT_P2_THRESHOLD = 40
DEFAULT_P3_THRESHOLD = 1


def classify_score(
    score: float,
    p1_threshold: int = DEFAULT_P1_THRESHOLD,
    p2_threshold: int = DEFAULT_P2_THRESHOLD,
    p3_threshold: int = DEFAULT_P3_THRESHOLD,
) -> str:
    """Classify a score using the shared, ordered cutoffs."""
    if score <= 0 or score < p3_threshold:
        return "Unmatched"
    if score >= p1_threshold:
        return "P1"
    if score >= p2_threshold:
        return "P2"
    return "P3"