"""RunResult dataclass representing the outcome of a single survey execution."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RunResult:
    """Result of a single survey automation run."""

    run_id: str
    batch_id: str
    uid: str
    survey_id: str
    pattern_id: str
    success: bool
    start_time: str
    end_time: str
    duration_seconds: float
    pages_completed: int
    error_message: Optional[str] = None
    screenshot_path: Optional[str] = None
    branch_path_taken: list[str] = field(default_factory=list)
    branch_diverged: bool = False
