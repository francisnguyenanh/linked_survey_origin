"""Pattern (Scenario) dataclass and related structures."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class TimingConfig:
    """Controls timing behaviour for a survey run to appear human-like."""

    min_total_seconds: int
    max_total_seconds: int
    page_delay_min: float
    page_delay_max: float
    typing_delay_per_char_ms: list[int]  # [min_ms, max_ms]


@dataclass
class AnswerStrategy:
    """Defines how to answer a single question."""

    strategy: str  # fixed | random_option | random_from_list | text_from_list
    value: Optional[str] = None                   # used by 'fixed'
    values: Optional[list[str]] = None            # used by 'random_from_list', 'text_from_list'
    weights: Optional[list[float]] = None         # optional weights for 'random_from_list'
    exclude_indices: Optional[list[int]] = None   # used by 'random_option'


@dataclass
class Pattern:
    """
    A complete scenario configuration describing how to answer an entire survey.
    Linked to a specific survey map.
    """

    schema_version: str
    pattern_id: str
    pattern_name: str
    description: str
    linked_survey_id: str
    created_at: str
    uid_pool: list[str]
    uid_strategy: str  # sequential | random
    answers: dict       # page_id -> q_id -> AnswerStrategy dict
    timing: TimingConfig
    branch_path: list[str] = field(default_factory=list)
    branch_ids_used: list[str] = field(default_factory=list)
    auto_generated_from_mapping: bool = False
    requires_branch_match: bool = False
