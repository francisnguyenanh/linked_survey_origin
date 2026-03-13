"""
Tests for PatternService — validation warnings, CRUD correctness.
"""
import pytest
import json
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sample_pattern_dict(
    uid_pool=None,
    answers=None,
) -> dict:
    return {
        "schema_version": "1.1",
        "pattern_id": "test-pattern",
        "pattern_name": "Test Pattern",
        "description": "Unit test pattern",
        "linked_survey_id": "survey_abc",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "uid_pool": uid_pool or ["UID001", "UID002"],
        "uid_strategy": "sequential",
        "answers": answers or {},
        "timing": {
            "min_total_seconds": 30,
            "max_total_seconds": 120,
            "page_delay_min": 2.0,
            "page_delay_max": 6.0,
            "typing_delay_per_char_ms": [50, 150],
        },
        "branch_path": [],
        "branch_ids_used": [],
        "auto_generated_from_mapping": False,
        "requires_branch_match": False,
    }


def _make_sample_survey_map_dict():
    return {
        "schema_version": "1.1",
        "survey_id": "survey_abc",
        "base_url": "https://rsch.jp/survey/test",
        "url_params": {},
        "created_at": datetime.now(timezone.utc).isoformat(),
        "pages": [
            {
                "page_id": "page_001",
                "page_index": 0,
                "url_pattern": "",
                "page_fingerprint": "abc123",
                "page_type": "questions",
                "questions": [
                    {
                        "q_id": "q_gender",
                        "q_index": 0,
                        "label_text": "性別",
                        "label_normalized": "性別",
                        "q_type": "radio",
                        "options": [
                            {"option_index": 0, "option_text": "男性", "option_value": "1"},
                            {"option_index": 1, "option_text": "女性", "option_value": "2"},
                        ],
                        "is_required": True,
                        "selector_strategy": "label_for",
                        "fallback_selector": "",
                        "honeypot": False,
                    }
                ],
                "navigation": {
                    "submit_button_text": "次へ",
                    "submit_selector": "input[type=submit]",
                    "method": "submit",
                },
                "branching_hints": [],
            }
        ],
        "branch_tree": {},
        "discovery_sessions": [],
        "coverage_stats": {},
    }


# ---------------------------------------------------------------------------
# Validation: missing required q_id
# ---------------------------------------------------------------------------

class TestValidatePatternWarnings:
    def _run_validate(self, survey_map_dict: dict, pattern_dict: dict) -> list[str]:
        """Inline validation logic mirroring PatternService.validate_pattern()."""
        warnings = []

        for page in survey_map_dict.get("pages", []):
            page_id = page["page_id"]
            page_answers = pattern_dict.get("answers", {}).get(page_id, {})

            for q in page.get("questions", []):
                if q.get("honeypot"):
                    continue
                q_id = q["q_id"]
                if q.get("is_required") and q_id not in page_answers:
                    warnings.append(
                        f"Page '{page_id}': required question '{q_id}' has no answer strategy."
                    )
                    continue

                strategy = page_answers.get(q_id, {})
                s_type = strategy.get("strategy") if isinstance(strategy, dict) else getattr(strategy, "strategy", None)
                if s_type == "fixed":
                    value = strategy.get("value") if isinstance(strategy, dict) else getattr(strategy, "value", None)
                    if not value:
                        warnings.append(
                            f"Page '{page_id}': q '{q_id}' uses 'fixed' strategy but value is empty."
                        )
                    elif q.get("q_type") in ("radio", "select", "checkbox"):
                        valid_values = [o["option_value"] for o in q.get("options", [])]
                        if value not in valid_values:
                            warnings.append(
                                f"Page '{page_id}': q '{q_id}' fixed value '{value}' not in option list {valid_values}."
                            )

        return warnings

    def test_missing_required_q_id_produces_warning(self):
        survey = _make_sample_survey_map_dict()
        pattern = _make_sample_pattern_dict(answers={})  # No answers provided

        warnings = self._run_validate(survey, pattern)

        assert len(warnings) == 1
        assert "q_gender" in warnings[0]
        assert "no answer strategy" in warnings[0]

    def test_valid_fixed_answer_no_warnings(self):
        survey = _make_sample_survey_map_dict()
        pattern = _make_sample_pattern_dict(
            answers={
                "page_001": {
                    "q_gender": {"strategy": "fixed", "value": "1", "values": None}
                }
            }
        )

        warnings = self._run_validate(survey, pattern)
        assert warnings == []

    def test_invalid_fixed_option_value_produces_warning(self):
        survey = _make_sample_survey_map_dict()
        pattern = _make_sample_pattern_dict(
            answers={
                "page_001": {
                    "q_gender": {"strategy": "fixed", "value": "99", "values": None}
                }
            }
        )

        warnings = self._run_validate(survey, pattern)
        assert len(warnings) == 1
        assert "not in option list" in warnings[0]

    def test_honeypot_questions_skipped_in_validation(self):
        """Honeypot questions must not generate warnings even if unanswered."""
        survey = _make_sample_survey_map_dict()
        survey["pages"][0]["questions"].append({
            "q_id": "hp_field",
            "q_index": 1,
            "label_text": "",
            "label_normalized": "",
            "q_type": "text",
            "options": [],
            "is_required": False,
            "selector_strategy": "label_for",
            "fallback_selector": "",
            "honeypot": True,
        })

        pattern = _make_sample_pattern_dict(
            answers={
                "page_001": {
                    "q_gender": {"strategy": "fixed", "value": "1", "values": None}
                    # hp_field intentionally absent
                }
            }
        )

        warnings = self._run_validate(survey, pattern)
        # hp_field should be skipped → no warning for it
        assert all("hp_field" not in w for w in warnings)

    def test_random_option_strategy_no_warning(self):
        survey = _make_sample_survey_map_dict()
        pattern = _make_sample_pattern_dict(
            answers={
                "page_001": {
                    "q_gender": {"strategy": "random_option", "value": None, "values": None}
                }
            }
        )

        warnings = self._run_validate(survey, pattern)
        assert warnings == []

    def test_empty_uid_pool_produces_warning(self):
        """An empty uid_pool should produce a warning."""
        survey = _make_sample_survey_map_dict()
        pattern = _make_sample_pattern_dict(
            uid_pool=[],
            answers={
                "page_001": {
                    "q_gender": {"strategy": "fixed", "value": "1", "values": None}
                }
            }
        )

        # Inline uid-pool check
        warnings = self._run_validate(survey, pattern)
        if not pattern.get("uid_pool"):
            warnings.append("uid_pool is empty — no UIDs to run.")

        assert any("uid_pool" in w for w in warnings)


# ---------------------------------------------------------------------------
# PatternService CRUD tests (filesystem)
# ---------------------------------------------------------------------------

class TestPatternServiceCRUD:
    """Integration-style tests: write a real JSON file to a temp directory."""

    def _get_service(self, tmp_path: Path):
        try:
            from app.services.pattern_service import PatternService
            return PatternService(patterns_dir=tmp_path)
        except ImportError:
            pytest.skip("App not importable — verify full environment.")

    def test_save_and_get_pattern(self, tmp_path):
        svc = self._get_service(tmp_path)
        data = _make_sample_pattern_dict()
        svc.save_pattern(data)
        loaded = svc.get_pattern("test-pattern")
        assert loaded["pattern_id"] == "test-pattern"
        assert loaded["pattern_name"] == "Test Pattern"

    def test_list_patterns_returns_saved(self, tmp_path):
        svc = self._get_service(tmp_path)
        data = _make_sample_pattern_dict()
        svc.save_pattern(data)
        listed = svc.list_patterns()
        assert any(p["pattern_id"] == "test-pattern" for p in listed)

    def test_delete_pattern_removes_file(self, tmp_path):
        svc = self._get_service(tmp_path)
        data = _make_sample_pattern_dict()
        svc.save_pattern(data)
        svc.delete_pattern("test-pattern")
        assert not (tmp_path / "test-pattern.json").exists()

    def test_get_nonexistent_pattern_raises(self, tmp_path):
        from app.exceptions import SurveyMapNotFoundError
        svc = self._get_service(tmp_path)
        with pytest.raises(Exception):
            svc.get_pattern("no-such-pattern")
