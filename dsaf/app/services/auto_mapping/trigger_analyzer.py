"""
TriggerAnalyzer — Identifies which questions on a page actually cause branching.

Algorithm per page:
  1. Baseline run: fill all questions with option[0], click Next, record fingerprint.
  2. For each candidate (radio/select/checkbox) question q:
       - Repeat: fill all = option[0], but set q = option[1]
       - Click Next, record fingerprint
       - If fingerprint ≠ baseline → q is a TRIGGER
  3. Return trigger_questions, data_questions, trigger_option_matrix.

Each probe uses a fresh browser context with the safe_uid to avoid dirtying
real survey data.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import TYPE_CHECKING

from app.services.browser_service import BrowserService, TimingHelper

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Question types that can potentially cause branching
_CANDIDATE_TYPES = {"radio", "select", "checkbox"}


class TriggerAnalyzer:
    """
    Probes each candidate question by varying its answer and checking
    whether the subsequent page fingerprint changes.
    """

    def __init__(self, browser_service: BrowserService, safe_uid: str) -> None:
        self.browser = browser_service
        self.safe_uid = safe_uid

        # Cache: page_fingerprint → analysis result (avoid re-probing same page)
        self._cache: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze_page(
        self,
        survey_url: str,
        page_sequence: list[dict],
        current_page_data: dict,
    ) -> dict:
        """
        Determine which questions on current_page_data cause branching.

        Args:
            survey_url:        Base survey URL (uid will be appended).
            page_sequence:     Previous pages + their answers used to reach here,
                               e.g. [{"page_id": "page_001", "answers": {...}}, ...]
            current_page_data: Output of MapperService.scan_current_page() for
                               the page being analyzed.

        Returns:
            {
                "trigger_questions": ["q_001"],
                "data_questions":    ["q_002", "q_003"],
                "trigger_option_matrix": {"q_001": ["1", "2", "9"]},
                "estimated_branches": 3,
            }
        """
        fingerprint = current_page_data.get("page_fingerprint", "")
        if fingerprint in self._cache:
            logger.debug("TriggerAnalyzer cache hit for %s", fingerprint[:8])
            return self._cache[fingerprint]

        questions = current_page_data.get("questions", [])
        candidates = [
            q for q in questions
            if q.get("q_type") in _CANDIDATE_TYPES
            and not q.get("honeypot")
            and len(q.get("options", [])) >= 2
        ]

        if not candidates:
            result = {
                "trigger_questions": [],
                "data_questions": [q["q_id"] for q in questions if not q.get("honeypot")],
                "trigger_option_matrix": {},
                "estimated_branches": 1,
            }
            self._cache[fingerprint] = result
            return result

        # Build default answers (option[0] for all candidates + dummy text for text)
        default_answers = self._make_default_answers(questions)

        # Baseline: all defaults
        baseline_fp = await self._probe(
            survey_url, page_sequence, default_answers
        )

        triggers: list[str] = []
        trigger_matrix: dict[str, list[str]] = {}

        for q in candidates:
            q_id = q["q_id"]
            options = q.get("options", [])
            if len(options) < 2:
                continue

            # Probe with option[1] on this question, others at option[0]
            test_answers = dict(default_answers)
            test_answers[q_id] = options[1]["option_value"]

            probe_fp = await self._probe(
                survey_url, page_sequence, test_answers
            )

            # Small random delay between probes
            await asyncio.sleep(random.uniform(1.5, 3.5))

            if probe_fp and probe_fp != baseline_fp:
                triggers.append(q_id)
                trigger_matrix[q_id] = [o["option_value"] for o in options]
                logger.info(
                    "TriggerAnalyzer: q_id=%s is TRIGGER (fp %s→%s)",
                    q_id, (baseline_fp or "")[:8], probe_fp[:8],
                )
            else:
                logger.debug("TriggerAnalyzer: q_id=%s is DATA (no branch)", q_id)

        data_questions = [
            q["q_id"] for q in questions
            if not q.get("honeypot") and q["q_id"] not in triggers
        ]

        estimated_branches = 1
        for opts in trigger_matrix.values():
            estimated_branches *= len(opts)

        result = {
            "trigger_questions": triggers,
            "data_questions": data_questions,
            "trigger_option_matrix": trigger_matrix,
            "estimated_branches": estimated_branches,
        }
        self._cache[fingerprint] = result
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _probe(
        self,
        survey_url: str,
        page_sequence: list[dict],
        test_answers: dict,
    ) -> str | None:
        """
        Run through page_sequence, fill test_answers on the final page,
        click Next, and return the fingerprint of the next page.
        Returns None on any error.
        """
        from app.services.mapper_service import MapperService

        uid_url = self._inject_uid(survey_url)
        context, page = await self.browser.create_context()
        try:
            await page.goto(uid_url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            # Replay prior pages
            for step in page_sequence:
                await self._fill_and_advance(page, step.get("answers", {}))
                await asyncio.sleep(random.uniform(1.0, 2.5))

            # Fill current page with test answers and click Next
            await self._fill_answers(page, test_answers)
            await self._click_next(page)
            await page.wait_for_load_state("networkidle", timeout=15_000)

            mapper = MapperService(self.browser)
            page_data = await mapper.scan_current_page(page)
            return page_data.get("page_fingerprint")

        except Exception as exc:
            logger.warning("_probe error: %s", exc)
            return None
        finally:
            try:
                await context.close()
            except Exception:
                pass

    async def _fill_and_advance(self, page, answers: dict) -> None:
        """Fill the current page with given answers and click Next."""
        await self._fill_answers(page, answers)
        await self._click_next(page)
        await page.wait_for_load_state("networkidle", timeout=15_000)

    async def _fill_answers(self, page, answers: dict) -> None:
        """Fill form elements according to the answers dict."""
        for q_id, value in answers.items():
            if value is None:
                continue
            try:
                # Try by name/id attribute matching q_id
                handled = await page.evaluate(
                    """([q_id, value]) => {
                        // Radio / checkbox
                        const radio = document.querySelector(
                            `input[type=radio][value="${value}"][name="${q_id}"],`+
                            `input[type=radio][value="${value}"]`
                        );
                        if (radio) { radio.click(); return true; }

                        // Select
                        const sel = document.querySelector(
                            `select[name="${q_id}"], select[id="${q_id}"]`
                        );
                        if (sel) { sel.value = value; sel.dispatchEvent(new Event('change')); return true; }

                        // Text / textarea
                        const txt = document.querySelector(
                            `input[name="${q_id}"], textarea[name="${q_id}"],`+
                            `input[id="${q_id}"], textarea[id="${q_id}"]`
                        );
                        if (txt) {
                            txt.value = value;
                            txt.dispatchEvent(new Event('input'));
                            txt.dispatchEvent(new Event('change'));
                            return true;
                        }
                        return false;
                    }""",
                    [q_id, str(value)],
                )
                if not handled:
                    logger.debug("_fill_answers: could not fill q_id=%s", q_id)
            except Exception as exc:
                logger.debug("_fill_answers error for %s: %s", q_id, exc)

    async def _click_next(self, page) -> None:
        """Click the Next/Submit button on the current form page."""
        next_texts = ["次へ", "次のページ", "進む", "続ける", "Next", "Continue"]
        for text in next_texts:
            try:
                btn = page.get_by_role("button", name=text)
                if await btn.count() > 0:
                    await btn.first.click()
                    return
                btn = page.get_by_role("link", name=text)
                if await btn.count() > 0:
                    await btn.first.click()
                    return
            except Exception:
                pass
        # Fallback: generic submit
        try:
            await page.evaluate(
                "() => { const s = document.querySelector('input[type=submit]'); if(s) s.click(); }"
            )
        except Exception:
            pass

    def _inject_uid(self, survey_url: str) -> str:
        """Replace or append the uid parameter with safe_uid."""
        import re
        if "uid=" in survey_url:
            return re.sub(r"uid=[^&]*", f"uid={self.safe_uid}", survey_url)
        sep = "&" if "?" in survey_url else "?"
        return f"{survey_url}{sep}uid={self.safe_uid}"

    def _make_default_answers(self, questions: list[dict]) -> dict:
        answers: dict[str, str] = {}
        for q in questions:
            if q.get("honeypot"):
                continue
            q_id = q["q_id"]
            q_type = q.get("q_type", "")
            options = q.get("options", [])
            if q_type in _CANDIDATE_TYPES and options:
                answers[q_id] = options[0]["option_value"]
            elif q_type in ("text", "textarea"):
                answers[q_id] = "テスト"
        return answers
