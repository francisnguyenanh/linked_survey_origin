"""SurveyMap dataclass and related structures."""

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class QuestionOption:
    """A single selectable option for a radio/checkbox/select question."""

    option_index: int
    option_text: str
    option_value: str


@dataclass
class Question:
    """Represents a single form question extracted from a survey page."""

    q_id: str
    q_index: int
    label_text: str
    label_normalized: str
    q_type: str  # radio | checkbox | select | textarea | text | hidden
    options: list[QuestionOption]
    is_required: bool
    selector_strategy: str  # label_text | index | fallback_selector
    fallback_selector: str
    honeypot: bool = False


@dataclass
class PageNavigation:
    """Describes how to navigate away from a page (which button/method to use)."""

    submit_button_text: str
    submit_selector: str
    method: str  # POST | GET


@dataclass
class BranchingHint:
    """A hint about which page a specific answer leads to."""

    condition: str
    leads_to_fingerprint: str


@dataclass
class SurveyPage:
    """Represents a single page in a multi-page survey."""

    page_id: str
    page_index: int
    url_pattern: str
    page_fingerprint: str
    page_type: str  # login | questions | confirmation | complete
    questions: list[Question]
    navigation: PageNavigation
    branching_hints: list[BranchingHint] = field(default_factory=list)


@dataclass
class SurveyMap:
    """
    Complete representation of a mapped survey including all pages,
    branch tree, and discovery session history.
    """

    schema_version: str
    survey_id: str
    base_url: str
    url_params: dict
    created_at: str
    pages: list[SurveyPage]
    branch_tree: dict = field(default_factory=dict)
    discovery_sessions: list[dict] = field(default_factory=list)
    coverage_stats: dict = field(default_factory=dict)
