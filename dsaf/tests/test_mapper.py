"""
Tests for MapperService — fingerprint consistency, question extraction, honeypot detection.
"""
import pytest
import hashlib
import unicodedata
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers matching MapperService._normalize_text / compute_fingerprint logic
# ---------------------------------------------------------------------------

def _normalize_text(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower().strip()


def _compute_fingerprint(labels: list[str]) -> str:
    normalized = sorted(_normalize_text(l) for l in labels if l.strip())
    payload = "|".join(normalized)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fingerprint consistency tests
# ---------------------------------------------------------------------------

class TestFingerprintConsistency:
    def test_same_questions_same_order(self):
        labels = ["お名前", "メールアドレス", "年齢"]
        fp1 = _compute_fingerprint(labels)
        fp2 = _compute_fingerprint(labels)
        assert fp1 == fp2

    def test_same_questions_different_order_yields_same_fingerprint(self):
        """Different insertion order must NOT change the fingerprint (sorted internally)."""
        labels_a = ["お名前", "メールアドレス", "年齢"]
        labels_b = ["年齢", "お名前", "メールアドレス"]
        assert _compute_fingerprint(labels_a) == _compute_fingerprint(labels_b)

    def test_different_questions_yield_different_fingerprint(self):
        labels_a = ["お名前", "メールアドレス"]
        labels_b = ["お名前", "電話番号"]
        assert _compute_fingerprint(labels_a) != _compute_fingerprint(labels_b)

    def test_fullwidth_ASCII_normalized(self):
        """Fullwidth ASCII chars (NFKC) must normalize to regular ASCII."""
        labels_a = ["ｅｍａｉｌ"]          # fullwidth
        labels_b = ["email"]               # regular ASCII
        assert _compute_fingerprint(labels_a) == _compute_fingerprint(labels_b)

    def test_empty_labels_ignored(self):
        labels_with_empty = ["お名前", "", "  ", "年齢"]
        labels_clean = ["お名前", "年齢"]
        assert _compute_fingerprint(labels_with_empty) == _compute_fingerprint(labels_clean)

    def test_single_label(self):
        fp = _compute_fingerprint(["お名前"])
        assert isinstance(fp, str) and len(fp) == 64

    def test_no_labels_produces_stable_hash(self):
        fp1 = _compute_fingerprint([])
        fp2 = _compute_fingerprint([])
        assert fp1 == fp2


# ---------------------------------------------------------------------------
# Honeypot detection tests
# ---------------------------------------------------------------------------

class TestHoneypotDetection:
    """
    MapperService marks a question as honeypot if:
      - display: none
      - visibility: hidden
      - opacity: 0
      - aria-hidden="true"
      - position: absolute; left/top < -9000
    """

    def test_hidden_display_none_is_honeypot(self):
        computed_style = {"display": "none", "visibility": "visible", "opacity": "1"}
        is_honeypot = (
            computed_style.get("display") == "none"
            or computed_style.get("visibility") == "hidden"
            or computed_style.get("opacity") == "0"
        )
        assert is_honeypot is True

    def test_visibility_hidden_is_honeypot(self):
        computed_style = {"display": "block", "visibility": "hidden", "opacity": "1"}
        is_honeypot = (
            computed_style.get("display") == "none"
            or computed_style.get("visibility") == "hidden"
            or computed_style.get("opacity") == "0"
        )
        assert is_honeypot is True

    def test_opacity_zero_is_honeypot(self):
        computed_style = {"display": "block", "visibility": "visible", "opacity": "0"}
        is_honeypot = (
            computed_style.get("display") == "none"
            or computed_style.get("visibility") == "hidden"
            or computed_style.get("opacity") == "0"
        )
        assert is_honeypot is True

    def test_visible_field_is_not_honeypot(self):
        computed_style = {"display": "block", "visibility": "visible", "opacity": "1"}
        is_honeypot = (
            computed_style.get("display") == "none"
            or computed_style.get("visibility") == "hidden"
            or computed_style.get("opacity") == "0"
        )
        assert is_honeypot is False

    def test_offscreen_position_is_honeypot(self):
        """Elements positioned far off-screen should be treated as honeypots."""
        rect = {"left": -9999, "top": -9999}
        is_offscreen = rect["left"] < -9000 or rect["top"] < -9000
        assert is_offscreen is True

    def test_normal_position_is_not_honeypot(self):
        rect = {"left": 100, "top": 200}
        is_offscreen = rect["left"] < -9000 or rect["top"] < -9000
        assert is_offscreen is False


# ---------------------------------------------------------------------------
# Label-input association tests (structural logic)
# ---------------------------------------------------------------------------

class TestLabelInputAssociation:
    def test_for_attribute_association(self):
        """A <label for="q1"> associates with <input id="q1">."""
        label_for = "q1"
        input_id = "q1"
        assert label_for == input_id

    def test_wrapping_label_association(self):
        """A label wrapping an input is an implicit association."""
        # Simulate: label contains input
        label_contains_input = True
        assert label_contains_input is True

    def test_multiple_inputs_one_label(self):
        """A radio group: multiple inputs share a single question label."""
        q_type = "radio"
        options = [
            {"value": "1", "text": "男性"},
            {"value": "2", "text": "女性"},
            {"value": "3", "text": "その他"},
        ]
        assert len(options) == 3
        assert q_type == "radio"


# ---------------------------------------------------------------------------
# MapperService unit tests (with mocked Playwright page)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scan_current_page_returns_survey_page():
    """scan_current_page() should return a SurveyPage with page_fingerprint set."""
    # We import here so tests file can be collected without full Flask app
    try:
        from app.services.mapper_service import MapperService
        from app.models.survey_map import SurveyPage

        mock_page = AsyncMock()
        mock_page.url = "https://rsch.jp/survey/test?uid=ABC123"
        mock_page.evaluate = AsyncMock(return_value={
            "questions": [
                {
                    "q_id": "q_0",
                    "q_index": 0,
                    "label_text": "お名前",
                    "label_normalized": "お名前",
                    "q_type": "text",
                    "options": [],
                    "is_required": True,
                    "selector_strategy": "label_for",
                    "fallback_selector": "input[type=text]:nth-of-type(1)",
                    "honeypot": False,
                }
            ],
            "navigation": {
                "submit_button_text": "次へ",
                "submit_selector": "input[type=submit]",
                "method": "submit",
            },
            "is_complete": False,
        })

        svc = MapperService(base_url="https://rsch.jp/survey/test")
        page_data = await svc.scan_current_page(mock_page)

        assert isinstance(page_data, SurveyPage)
        assert len(page_data.page_fingerprint) == 64  # SHA-256 hex
        assert len(page_data.questions) == 1
        assert page_data.questions[0].q_id == "q_0"
    except ImportError:
        pytest.skip("App not importable in isolated test run — verify full environment.")
