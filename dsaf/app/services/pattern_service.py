"""
PatternService — CRUD operations for pattern JSON scenario files.

Patterns live in data/patterns/{pattern_id}.pattern.json.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from slugify import slugify

logger = logging.getLogger(__name__)


class PatternService:
    """CRUD operations for pattern JSON files stored in the patterns directory."""

    def __init__(self, patterns_dir: str | Path):
        """
        Initialise PatternService.

        Args:
            patterns_dir: Directory where pattern JSON files are stored.
        """
        self.patterns_dir = Path(patterns_dir)
        self.patterns_dir.mkdir(parents=True, exist_ok=True)

    def _pattern_path(self, pattern_id: str) -> Path:
        return self.patterns_dir / f"{pattern_id}.pattern.json"

    def list_patterns(self) -> list[dict]:
        """
        List all saved patterns with their top-level metadata.

        Returns:
            List of pattern dicts (full content of each file).
        """
        patterns = []
        for fp in sorted(self.patterns_dir.glob("*.pattern.json")):
            try:
                with open(fp, encoding="utf-8") as fh:
                    data = json.load(fh)
                patterns.append(data)
            except Exception as exc:
                logger.warning(f"Could not load pattern file {fp}: {exc}")
        return patterns

    def get_pattern(self, pattern_id: str) -> Optional[dict]:
        """
        Retrieve a single pattern by ID.

        Args:
            pattern_id: Pattern identifier string.

        Returns:
            Pattern dict, or None if not found.
        """
        path = self._pattern_path(pattern_id)
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def save_pattern(self, pattern: dict) -> str:
        """
        Save (create or overwrite) a pattern JSON file.

        Sets 'created_at' if not already present. Derives pattern_id from
        pattern_name if pattern_id is missing.

        Args:
            pattern: Complete pattern dict conforming to the schema.

        Returns:
            The pattern_id that was saved.
        """
        if not pattern.get("pattern_id"):
            name = pattern.get("pattern_name", "pattern")
            pattern["pattern_id"] = f"pattern_{slugify(name, separator='_')}"

        if not pattern.get("created_at"):
            pattern["created_at"] = datetime.now(timezone.utc).isoformat()

        pattern.setdefault("schema_version", "1.1")

        path = self._pattern_path(pattern["pattern_id"])
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(pattern, fh, ensure_ascii=False, indent=2)

        logger.info(f"Pattern saved: {pattern['pattern_id']}")
        return pattern["pattern_id"]

    def delete_pattern(self, pattern_id: str) -> bool:
        """
        Delete a pattern file by ID.

        Args:
            pattern_id: Pattern identifier to delete.

        Returns:
            True if deleted, False if file did not exist.
        """
        path = self._pattern_path(pattern_id)
        if path.exists():
            path.unlink()
            logger.info(f"Pattern deleted: {pattern_id}")
            return True
        return False

    def validate_pattern(self, pattern: dict, survey_map: dict) -> list[str]:
        """
        Validate a pattern against its corresponding survey map.

        Checks:
        - All page_ids in pattern.answers exist in survey_map.pages
        - All q_ids in pattern.answers exist in the corresponding page
        - Fixed strategy option values match available options on that question
        - Required questions have answers assigned

        Args:
            pattern: Pattern dict to validate.
            survey_map: Corresponding survey map dict.

        Returns:
            List of warning/error strings. Empty list means valid.
        """
        warnings: list[str] = []

        # Build lookup structures from survey map
        pages_by_id: dict[str, dict] = {p["page_id"]: p for p in survey_map.get("pages", [])}
        questions_by_page: dict[str, dict[str, dict]] = {}
        for page in survey_map.get("pages", []):
            questions_by_page[page["page_id"]] = {
                q["q_id"]: q for q in page.get("questions", [])
            }

        pattern_answers: dict = pattern.get("answers", {})

        for page_id, page_answers in pattern_answers.items():
            if page_id not in pages_by_id:
                warnings.append(f"page_id '{page_id}' in pattern not found in survey map")
                continue

            page_questions = questions_by_page.get(page_id, {})
            for q_id, answer_strategy in page_answers.items():
                if q_id not in page_questions:
                    warnings.append(
                        f"q_id '{q_id}' in page '{page_id}' not found in survey map"
                    )
                    continue

                question = page_questions[q_id]
                strategy = answer_strategy.get("strategy", "")
                available_values = {
                    opt["option_value"] for opt in question.get("options", [])
                }

                if strategy == "fixed":
                    value = answer_strategy.get("value")
                    if available_values and value not in available_values:
                        warnings.append(
                            f"page '{page_id}' q '{q_id}': fixed value '{value}' "
                            f"not in available options {available_values}"
                        )
                elif strategy == "random_from_list":
                    values = answer_strategy.get("values", [])
                    if available_values:
                        invalid = [v for v in values if v not in available_values]
                        if invalid:
                            warnings.append(
                                f"page '{page_id}' q '{q_id}': values {invalid} "
                                f"not in available options {available_values}"
                            )

        # Check required questions have answers
        for page in survey_map.get("pages", []):
            page_id = page["page_id"]
            page_ans = pattern_answers.get(page_id, {})
            for question in page.get("questions", []):
                if question.get("is_required") and not question.get("honeypot"):
                    if question["q_id"] not in page_ans:
                        warnings.append(
                            f"Required question '{question['q_id']}' on page '{page_id}' "
                            f"has no answer strategy assigned"
                        )

        return warnings
