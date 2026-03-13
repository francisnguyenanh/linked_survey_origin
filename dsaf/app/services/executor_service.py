"""
ExecutorService — Core automation engine for survey runs.

Executes survey automation based on a survey map + pattern config.
Emits real-time progress events via Flask-SocketIO.
"""

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.exceptions import (
    BrowserContextError,
    PageFingerprintMismatchError,
    ProxyBlockedError,
    SurveyCompletionError,
)
from app.models.run_result import RunResult
from app.services.browser_service import BrowserService, TimingHelper
from app.services.mapper_service import COMPLETE_PAGE_SIGNALS

logger = logging.getLogger(__name__)


async def capture_error_state(page, run_id: str, error: Exception, screenshots_dir: Path) -> str:
    """
    Capture a screenshot and page HTML when an unexpected error occurs.

    Args:
        page: Active Playwright page.
        run_id: Unique run identifier for file naming.
        error: The exception that triggered capture.
        screenshots_dir: Directory to save artefacts.

    Returns:
        Path to the saved screenshot file, or empty string on failure.
    """
    ts = int(time.time())
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    png_path = screenshots_dir / f"{run_id}_{ts}.png"
    html_path = screenshots_dir / f"{run_id}_{ts}.html"

    try:
        await page.screenshot(path=str(png_path), full_page=True)
        content = await page.content()
        html_path.write_text(content, encoding="utf-8")
        logger.info(f"Error state captured: {png_path}")
        return str(png_path)
    except Exception as cap_exc:
        logger.warning(f"Failed to capture error state: {cap_exc}")
        return ""


class ExecutorService:
    """
    Core automation engine. Executes survey runs based on pattern configs.
    Emits real-time progress via Flask-SocketIO.
    """

    def __init__(self, browser_service: BrowserService, socketio, data_dir: Path):
        """
        Initialise ExecutorService.

        Args:
            browser_service: Configured BrowserService instance.
            socketio: Flask-SocketIO instance for real-time event emission.
            data_dir: Base data directory (for results/ and screenshots/).
        """
        self.browser_service = browser_service
        self.socketio = socketio
        self.data_dir = Path(data_dir)
        self.results_dir = self.data_dir / "results"
        self.screenshots_dir = self.data_dir / "screenshots"
        self._stop_flags: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_single(
        self,
        survey_map: dict,
        pattern: dict,
        uid: str,
        run_id: str,
        batch_id: str,
    ) -> RunResult:
        """
        Execute ONE complete survey run.

        Flow:
        1. Fresh browser context.
        2. Navigate to login URL with uid injected.
        3. Main loop: fingerprint current page → fill → delay → submit.
        4. Enforce minimum timing threshold before final submit.
        5. Return RunResult.

        Args:
            survey_map: Loaded survey map dict.
            pattern: Loaded pattern dict.
            uid: Single UID for this run.
            run_id: Unique identifier for this run.
            batch_id: Parent batch identifier for SocketIO events.

        Returns:
            RunResult dataclass.
        """
        start_time = time.monotonic()
        start_ts = datetime.now(timezone.utc).isoformat()
        pages_completed = 0
        branch_path_taken: list[str] = []
        context = None

        self._emit(
            "run_progress",
            {
                "batch_id": batch_id,
                "run_id": run_id,
                "uid": uid,
                "status": "started",
                "message": f"Starting run for UID {uid}",
            },
        )

        try:
            context, page = await self.browser_service.create_context()

            # Build start URL
            base_url = survey_map.get("base_url", "")
            url_params = {
                k: (uid if v == "{uid_placeholder}" else v)
                for k, v in survey_map.get("url_params", {}).items()
            }
            from urllib.parse import urlencode
            start_url = f"{base_url}?{urlencode(url_params)}"

            ok = await self.browser_service.navigate_with_retry(page, start_url)
            if not ok:
                raise SurveyCompletionError(f"Failed to load start URL: {start_url}")

            # Build page-fingerprint → page_data lookup from survey map
            fingerprint_map: dict[str, dict] = {}
            for p in survey_map.get("pages", []):
                fingerprint_map[p["page_fingerprint"]] = p
            # Also include branch tree nodes
            for node in survey_map.get("branch_tree", {}).get("nodes", {}).values():
                fp = node.get("fingerprint")
                if fp and "page_data" in node:
                    fingerprint_map[fp] = node["page_data"]

            current_step = 0
            max_steps = len(survey_map.get("pages", [])) + 20  # safety limit

            while current_step < max_steps:
                if self._stop_flags.get(batch_id):
                    logger.info(f"Batch {batch_id} stop flag detected — aborting run {run_id}")
                    break

                current_url = page.url
                logger.debug(f"Run {run_id}: step {current_step}, URL={current_url}")

                # Check for survey completion signals
                if self._is_complete_page(current_url):
                    self._emit(
                        "run_progress",
                        {
                            "batch_id": batch_id, "run_id": run_id, "uid": uid,
                            "status": "completed", "message": "Survey completed",
                        },
                    )
                    break

                # Compute fingerprint of visible questions
                from app.services.mapper_service import MapperService
                mapper = MapperService(self.browser_service)
                page_data = await mapper.scan_current_page(page)
                current_fp = page_data.get("page_fingerprint", "")

                # Match to survey map
                matched_page = fingerprint_map.get(current_fp)
                if not matched_page:
                    logger.warning(
                        f"Run {run_id}: unknown fingerprint {current_fp[:8]}… at {current_url}"
                    )
                    screenshot_path = await capture_error_state(
                        page, run_id, Exception("Unknown fingerprint"), self.screenshots_dir
                    )
                    raise PageFingerprintMismatchError(
                        f"Unknown page fingerprint: {current_fp[:8]}…"
                    )

                page_id = matched_page.get("page_id", f"page_{current_step:03d}")
                branch_path_taken.append(page_id)

                # Validate branch if required by pattern
                if pattern.get("requires_branch_match") and pattern.get("branch_path"):
                    expected_pages = pattern["branch_path"]
                    if current_step < len(expected_pages):
                        expected_id = expected_pages[current_step]
                        if page_id != expected_id:
                            recovered = await self._adaptive_branch_recovery(
                                page, current_fp,
                                fingerprint_map.get(expected_id, {}).get("page_fingerprint", ""),
                                pattern, fingerprint_map,
                            )
                            if not recovered:
                                raise PageFingerprintMismatchError(
                                    f"Branch mismatch at step {current_step}: "
                                    f"expected {expected_id}, got {page_id}"
                                )

                # Fill the page
                pattern_answers = pattern.get("answers", {}).get(page_id, {})
                await self._fill_page(
                    page, matched_page, pattern_answers, pattern.get("timing", {})
                )
                pages_completed += 1

                self._emit(
                    "run_progress",
                    {
                        "batch_id": batch_id, "run_id": run_id, "uid": uid,
                        "status": "in_progress",
                        "message": f"Completed page {page_id} ({current_step + 1})",
                    },
                )

                # Enforce minimum timing before the last page submit
                if current_step >= max_steps - 2:
                    timing = pattern.get("timing", {})
                    await TimingHelper.ensure_minimum_duration(
                        start_time, timing.get("min_total_seconds", 90)
                    )

                # Random page transition delay
                think_time = TimingHelper.page_think_time(pattern.get("timing", {}))
                await asyncio.sleep(think_time)

                # Click navigation button
                nav = matched_page.get("navigation", {})
                submit_selector = nav.get("submit_selector", "input[type='submit']")
                try:
                    await self.browser_service.human_click(page, submit_selector)
                    await page.wait_for_load_state("domcontentloaded", timeout=15_000)
                except PlaywrightTimeoutError:
                    logger.warning(f"Run {run_id}: page load timeout after submit on step {current_step}")

                current_step += 1

            end_ts = datetime.now(timezone.utc).isoformat()
            duration = time.monotonic() - start_time

            return RunResult(
                run_id=run_id,
                batch_id=batch_id,
                uid=uid,
                survey_id=survey_map.get("survey_id", ""),
                pattern_id=pattern.get("pattern_id", ""),
                success=True,
                start_time=start_ts,
                end_time=end_ts,
                duration_seconds=round(duration, 2),
                pages_completed=pages_completed,
                branch_path_taken=branch_path_taken,
            )

        except (ProxyBlockedError, PageFingerprintMismatchError, SurveyCompletionError,
                BrowserContextError) as exc:
            logger.error(f"Run {run_id} failed [{type(exc).__name__}]: {exc}")
            screenshot_path = ""
            try:
                screenshot_path = await capture_error_state(
                    page, run_id, exc, self.screenshots_dir
                )
            except Exception:
                pass
            return RunResult(
                run_id=run_id,
                batch_id=batch_id,
                uid=uid,
                survey_id=survey_map.get("survey_id", ""),
                pattern_id=pattern.get("pattern_id", ""),
                success=False,
                start_time=start_ts,
                end_time=datetime.now(timezone.utc).isoformat(),
                duration_seconds=round(time.monotonic() - start_time, 2),
                pages_completed=pages_completed,
                error_message=str(exc),
                screenshot_path=screenshot_path,
                branch_path_taken=branch_path_taken,
            )
        except Exception as exc:
            logger.exception(f"Unexpected error in run {run_id}: {exc}")
            screenshot_path = ""
            try:
                screenshot_path = await capture_error_state(
                    page, run_id, exc, self.screenshots_dir
                )
            except Exception:
                pass
            return RunResult(
                run_id=run_id,
                batch_id=batch_id,
                uid=uid,
                survey_id=survey_map.get("survey_id", ""),
                pattern_id=pattern.get("pattern_id", ""),
                success=False,
                start_time=start_ts,
                end_time=datetime.now(timezone.utc).isoformat(),
                duration_seconds=round(time.monotonic() - start_time, 2),
                pages_completed=pages_completed,
                error_message=str(exc),
                screenshot_path=screenshot_path,
                branch_path_taken=branch_path_taken,
            )
        finally:
            if context:
                try:
                    await context.close()
                except Exception:
                    pass

    async def _fill_page(
        self,
        page,
        page_data: dict,
        pattern_answers: dict,
        timing_config: dict,
    ):
        """
        Fill all non-honeypot questions on a page according to pattern answers.

        Strategy dispatch:
        - 'fixed'            → use value directly
        - 'random_option'    → choose random option (respecting exclude_indices)
        - 'random_from_list' → weighted random from values list
        - 'text_from_list'   → random pick from text strings, type it

        Args:
            page: Active Playwright page.
            page_data: Scanned page data dict.
            pattern_answers: Dict of q_id -> answer strategy dict for this page.
            timing_config: Pattern timing config for typing delays.
        """
        delay_range = timing_config.get("typing_delay_per_char_ms", [50, 150])
        questions = page_data.get("questions", [])

        for question in questions:
            if question.get("honeypot"):
                logger.debug(f"Skipping honeypot: {question.get('q_id')}")
                continue

            q_id = question.get("q_id")
            answer_strategy = pattern_answers.get(q_id)
            if not answer_strategy:
                continue

            strategy = answer_strategy.get("strategy", "random_option")
            q_type = question.get("q_type", "text")
            options = question.get("options", [])
            selector_strategy = question.get("selector_strategy", "label_text")
            fallback = question.get("fallback_selector", "")
            label_text = question.get("label_text", "")

            # Determine the value to use
            value = self._resolve_answer_value(answer_strategy, options)
            if value is None:
                continue

            # Determine element selector
            if selector_strategy == "label_text" and label_text:
                selector = self._build_label_selector(label_text, q_type, value)
            elif fallback:
                selector = fallback
            else:
                logger.warning(f"No selector for q_id {q_id}, skipping")
                continue

            # Interact with element
            try:
                if q_type in ("radio", "checkbox"):
                    await self.browser_service.human_click(page, selector)
                elif q_type == "select":
                    await page.select_option(fallback or f"select", value=value)
                    await asyncio.sleep(random.uniform(0.2, 0.5))
                elif q_type in ("text", "textarea"):
                    await self.browser_service.human_type(
                        page, fallback or selector, value, delay_range
                    )
                await asyncio.sleep(random.uniform(0.3, 0.8))
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout filling {q_id} with selector {selector}, trying fallback")
                if fallback and fallback != selector:
                    try:
                        await self.browser_service.human_click(page, fallback)
                    except Exception as exc:
                        logger.warning(f"Fallback also failed for {q_id}: {exc}")
            except Exception as exc:
                logger.warning(f"Error filling {q_id}: {exc}")

    @staticmethod
    def _resolve_answer_value(answer_strategy: dict, options: list) -> Optional[str]:
        """Resolve the actual value to use from an answer strategy dict."""
        strategy = answer_strategy.get("strategy", "")

        if strategy == "fixed":
            return answer_strategy.get("value")

        if strategy == "random_option":
            exclude = set(answer_strategy.get("exclude_indices", []))
            available = [o for o in options if o["option_index"] not in exclude]
            if available:
                return random.choice(available)["option_value"]
            return None

        if strategy == "random_from_list":
            values = answer_strategy.get("values", [])
            weights = answer_strategy.get("weights")
            if not values:
                return None
            if weights and len(weights) == len(values):
                return random.choices(values, weights=weights, k=1)[0]
            return random.choice(values)

        if strategy == "text_from_list":
            values = answer_strategy.get("values", [])
            return random.choice(values) if values else None

        return None

    @staticmethod
    def _build_label_selector(label_text: str, q_type: str, value: str) -> str:
        """Construct a Playwright selector that targets an option label by its text."""
        if q_type in ("radio", "checkbox"):
            return f"label:has-text('{label_text}') input"
        return f"label:has-text('{label_text}') + input"

    async def _match_page_to_branch(
        self,
        current_fingerprint: str,
        pattern: dict,
        current_step: int,
        fingerprint_map: dict,
    ) -> tuple[Optional[str], Optional[dict]]:
        """
        Match current page fingerprint to expected branch in the pattern.

        Returns:
            (page_id, page_answers) tuple, or (None, None) if should abort.
        """
        expected_pages = pattern.get("branch_path", [])
        if current_step < len(expected_pages):
            expected_page_id = expected_pages[current_step]
            for node_fp, node_data in fingerprint_map.items():
                if node_data.get("page_id") == expected_page_id:
                    if node_fp == current_fingerprint:
                        answers = pattern.get("answers", {}).get(expected_page_id, {})
                        return expected_page_id, answers
                    else:
                        logger.warning(
                            f"Branch mismatch at step {current_step}: "
                            f"expected FP for {expected_page_id} but got {current_fingerprint[:8]}…"
                        )
                        return None, None

        # No strict branch path: just use fingerprint lookup
        page_data = fingerprint_map.get(current_fingerprint)
        if page_data:
            page_id = page_data.get("page_id", f"page_{current_step:03d}")
            answers = pattern.get("answers", {}).get(page_id, {})
            return page_id, answers

        return None, None

    async def _adaptive_branch_recovery(
        self,
        page,
        actual_fingerprint: str,
        expected_fingerprint: str,
        pattern: dict,
        fingerprint_map: dict,
    ) -> bool:
        """
        Attempt to recover when we land on an unexpected branch mid-run.

        Strategy: If a matching pattern exists for the actual branch, use random
        answers to complete. Otherwise take random answers to avoid abandonment.

        Args:
            page: Active Playwright page.
            actual_fingerprint: Fingerprint of the page we're actually on.
            expected_fingerprint: Fingerprint that was expected by the pattern.
            pattern: The running pattern dict.
            fingerprint_map: Full fingerprint → page_data lookup.

        Returns:
            True if recovery succeeded and run should continue, False to abort.
        """
        from app.services.mapper_service import MapperService
        logger.warning(
            f"Branch divergence: expected {expected_fingerprint[:8]}…, "
            f"got {actual_fingerprint[:8]}…. Attempting recovery."
        )
        actual_page = fingerprint_map.get(actual_fingerprint)
        if actual_page:
            logger.info(f"Actual page found in map: {actual_page.get('page_id')} — filling randomly")
            return True  # Let the main loop fill with random_option fallback

        logger.error("Actual page not in map at all — run cannot continue safely")
        return False

    async def run_batch(
        self,
        survey_map: dict,
        pattern: dict,
        uid_list: list[str],
        run_count: int,
        batch_id: str,
        concurrency: int = 1,
    ):
        """
        Execute multiple survey runs, writing results incrementally.

        Args:
            survey_map: Loaded survey map dict.
            pattern: Loaded pattern dict.
            uid_list: Pool of UIDs to draw from.
            run_count: Total number of runs to execute.
            batch_id: Unique batch identifier.
            concurrency: Number of parallel runs (1=sequential, 2-3=parallel).
        """
        self._stop_flags[batch_id] = False
        results: list[RunResult] = []
        results_path = self.results_dir / f"{batch_id}.json"
        self.results_dir.mkdir(parents=True, exist_ok=True)

        uid_strategy = pattern.get("uid_strategy", "sequential")
        uids = self._prepare_uid_sequence(uid_list, run_count, uid_strategy)

        self._emit(
            "run_progress",
            {
                "batch_id": batch_id,
                "status": "batch_started",
                "message": f"Batch {batch_id}: {run_count} runs starting",
                "total": run_count,
            },
        )

        for i in range(0, run_count, max(1, concurrency)):
            if self._stop_flags.get(batch_id):
                logger.info(f"Batch {batch_id} stopped after {i} runs")
                break

            chunk_uids = uids[i: i + max(1, concurrency)]
            run_coroutines = [
                self.run_single(
                    survey_map, pattern,
                    uid=chunk_uids[j] if j < len(chunk_uids) else uids[0],
                    run_id=str(uuid.uuid4())[:8],
                    batch_id=batch_id,
                )
                for j in range(len(chunk_uids))
            ]

            if concurrency > 1:
                chunk_results = await asyncio.gather(*run_coroutines)
            else:
                chunk_results = [await run_coroutines[0]]

            results.extend(chunk_results)

            # Persist results incrementally
            with open(results_path, "w", encoding="utf-8") as fh:
                json.dump(
                    [r.__dict__ for r in results], fh, ensure_ascii=False, indent=2
                )

            succeeded = sum(1 for r in results if r.success)
            self._emit(
                "run_progress",
                {
                    "batch_id": batch_id,
                    "completed": len(results),
                    "total": run_count,
                    "succeeded": succeeded,
                    "failed": len(results) - succeeded,
                    "status": "batch_in_progress",
                },
            )

        total_duration = sum(r.duration_seconds for r in results)
        self._emit(
            "batch_complete",
            {
                "batch_id": batch_id,
                "summary": {
                    "total": len(results),
                    "succeeded": sum(1 for r in results if r.success),
                    "failed": sum(1 for r in results if not r.success),
                    "duration_seconds": round(total_duration, 1),
                },
            },
        )

        if batch_id in self._stop_flags:
            del self._stop_flags[batch_id]

    def stop_batch(self, batch_id: str):
        """Signal a running batch to stop after the current run completes."""
        self._stop_flags[batch_id] = True
        logger.info(f"Stop signal sent to batch: {batch_id}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit(self, event: str, data: dict):
        """Emit a SocketIO event, suppressing any emission errors."""
        try:
            self.socketio.emit(event, data)
        except Exception as exc:
            logger.debug(f"SocketIO emit failed: {exc}")

    @staticmethod
    def _is_complete_page(url: str) -> bool:
        """Return True if the URL contains survey-completion signals."""
        url_lower = url.lower()
        return any(sig in url_lower for sig in COMPLETE_PAGE_SIGNALS)

    @staticmethod
    def _prepare_uid_sequence(
        uid_pool: list[str], run_count: int, strategy: str
    ) -> list[str]:
        """Build the ordered list of UIDs for a batch run."""
        if not uid_pool:
            return [str(uuid.uuid4())[:8] for _ in range(run_count)]
        if strategy == "sequential":
            uids = []
            for i in range(run_count):
                uids.append(uid_pool[i % len(uid_pool)])
            return uids
        # random
        return [random.choice(uid_pool) for _ in range(run_count)]
