"""
DFSExplorer — Recursive depth-first search across a survey's page tree.

Each call to _dfs_node():
  1. Opens a fresh Playwright browser context (clean session, safe UID)
  2. Replays the known path to reach the target page
  3. Scans + fingerprints the current page
  4. Detects cycles (fingerprint already seen)
  5. Checks SafetyGuard for terminal pages
  6. Runs TriggerAnalyzer to find branching questions
  7. Generates cartesian product of trigger options
  8. Recurses for each option combination

Hard limits:
  MAX_DEPTH       = 20  (survey page depth)
  MAX_BRANCHES    = 200 (total DFS nodes explored)
"""

from __future__ import annotations

import asyncio
import logging
import random
from itertools import product as cartesian_product
from typing import Any, Callable

from app.services.browser_service import BrowserService, TimingHelper
from .survey_graph import SurveyGraph
from .safety_guard import SafetyGuard
from .trigger_analyzer import TriggerAnalyzer
from .rate_limit_manager import RateLimitManager

logger = logging.getLogger(__name__)

MAX_DEPTH = 20
MAX_BRANCHES = 200

# Re-used dummy text for text/textarea questions that don't cause branching
_DUMMY_TEXT = "テスト回答"


class ReplayError(RuntimeError):
    """Raised when replaying a known path fails (navigation timeout, unexpected page)."""


class DFSExplorer:
    """
    Depth-first explorer for survey branching trees.

    Progress callback signature:
        on_progress(event: str, data: dict) -> None
        Events: "new_page", "revisit", "terminal", "branch_start", "branch_done", "error"
    """

    def __init__(
        self,
        browser_service: BrowserService,
        trigger_analyzer: TriggerAnalyzer,
        safety_guard: SafetyGuard,
        rate_limit_manager: RateLimitManager,
        on_progress: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.browser = browser_service
        self.analyzer = trigger_analyzer
        self.safety = safety_guard
        self.rate_limiter = rate_limit_manager
        self.on_progress = on_progress or (lambda e, d: None)

        # Shared state across recursive calls
        self._survey_graph: SurveyGraph = SurveyGraph()
        self._branches_explored: int = 0
        self._stop_flag: bool = False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def explore(self, survey_url: str, safe_uid: str) -> SurveyGraph:
        """
        Start DFS from the survey's first page.

        Returns a fully-built SurveyGraph after all paths have been explored
        (or the hard limits have been reached).
        """
        self._survey_graph = SurveyGraph()
        self._branches_explored = 0
        self._stop_flag = False

        await self._dfs_node(
            survey_url=survey_url,
            safe_uid=safe_uid,
            path_so_far=[],
            depth=0,
        )

        return self._survey_graph

    def stop(self) -> None:
        """Request a graceful stop after the current branch finishes."""
        self._stop_flag = True

    @property
    def branches_explored(self) -> int:
        return self._branches_explored

    # ------------------------------------------------------------------
    # Core recursive DFS
    # ------------------------------------------------------------------

    async def _dfs_node(
        self,
        survey_url: str,
        safe_uid: str,
        path_so_far: list[tuple[str, dict]],
        depth: int,
    ) -> None:
        """
        Process one node in the survey tree.

        path_so_far: [(page_id, trigger_answers_used), ...]
            Each tuple represents a page already visited and the answers used
            to move from that page to the next.
        """
        if depth > MAX_DEPTH or self._branches_explored >= MAX_BRANCHES or self._stop_flag:
            logger.debug(
                "DFS stopping: depth=%d branches=%d stop=%s",
                depth, self._branches_explored, self._stop_flag,
            )
            return

        self._branches_explored += 1
        g = self._survey_graph

        context, page = await self.browser.create_context()
        try:
            # ── Step 1: Navigate to survey start with safe UID ────────
            uid_url = self._inject_uid(survey_url, safe_uid)
            await page.goto(uid_url, wait_until="networkidle", timeout=30_000)
            await asyncio.sleep(random.uniform(1.0, 2.0))

            # ── Step 2: Replay path ───────────────────────────────────
            if path_so_far:
                try:
                    await self._replay_path(page, path_so_far)
                except ReplayError as exc:
                    logger.warning("Replay failed at depth=%d: %s", depth, exc)
                    self.on_progress("error", {"depth": depth, "message": str(exc)})
                    return

            # ── Step 3: Scan current page ─────────────────────────────
            page_data = await self._scan_page(page)
            fingerprint = page_data.get("page_fingerprint", "")

            # ── Step 4: Cycle / revisit detection ─────────────────────
            if g.has_fingerprint(fingerprint):
                known_id = g.get_page_id_by_fingerprint(fingerprint)
                g.increment_visit(known_id)
                if path_so_far:
                    parent_id, trigger_answers = path_so_far[-1]
                    g.add_branch_edge(parent_id, known_id, trigger_answers)
                self.on_progress("revisit", {"page_id": known_id, "depth": depth})
                logger.debug("Revisit: %s at depth %d", known_id, depth)
                return

            # ── Step 5: Register new page node ────────────────────────
            page_id = self._generate_page_id(fingerprint, depth)
            g.add_page_node(page_id, fingerprint, page_data, depth=depth)

            if g.root_node_id is None and depth == 0:
                g.root_node_id = page_id

            if path_so_far:
                parent_id, trigger_answers = path_so_far[-1]
                g.add_branch_edge(parent_id, page_id, trigger_answers)

            self.on_progress("new_page", {
                "page_id": page_id,
                "depth": depth,
                "fingerprint": fingerprint[:12],
                "question_count": len(page_data.get("questions", [])),
            })

            # ── Step 6: Terminal check ────────────────────────────────
            if await self.safety.is_terminal_page(page):
                g.mark_terminal(page_id)
                await self.safety.intercept_final_submit(page)
                self.on_progress("terminal", {"page_id": page_id, "depth": depth})
                logger.info("Terminal page: %s (depth=%d)", page_id, depth)
                return

            # ── Step 7: Analyze triggers ──────────────────────────────
            prior_steps = [
                {"page_id": pid, "answers": ans}
                for pid, ans in path_so_far
            ]
            trigger_info = await self.analyzer.analyze_page(
                survey_url, prior_steps, page_data
            )

            # ── Step 8: Build option combos ───────────────────────────
            default_answers = self._make_default_answers(page_data)

            if trigger_info["trigger_questions"]:
                q_ids = trigger_info["trigger_questions"]
                option_lists = [trigger_info["trigger_option_matrix"][q] for q in q_ids]
                raw_combos = list(cartesian_product(*option_lists))[:MAX_BRANCHES]
                combos: list[dict] = []
                for raw in raw_combos:
                    combo = dict(default_answers)
                    combo.update(dict(zip(q_ids, raw)))
                    combos.append(combo)
            else:
                combos = [default_answers]

            # ── Step 9: Recurse for each combo ────────────────────────
            for combo in combos:
                if self._stop_flag or self._branches_explored >= MAX_BRANCHES:
                    break
                await self.rate_limiter.wait_before_branch()
                self.on_progress("branch_start", {
                    "from_page": page_id,
                    "trigger_answers": {k: v for k, v in combo.items() if k in trigger_info["trigger_questions"]},
                    "depth": depth + 1,
                })
                new_path = path_so_far + [(page_id, combo)]
                await self._dfs_node(survey_url, safe_uid, new_path, depth + 1)
                self.on_progress("branch_done", {"from_page": page_id, "depth": depth})

        except Exception as exc:
            logger.exception("_dfs_node error at depth=%d: %s", depth, exc)
            self.on_progress("error", {"depth": depth, "message": str(exc)})
        finally:
            try:
                await context.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Path replay
    # ------------------------------------------------------------------

    async def _replay_path(
        self,
        page,
        path: list[tuple[str, dict]],
    ) -> None:
        """
        Re-execute a sequence of (page_id, answers) pairs on an already-loaded
        survey page, clicking through to reach the latest position.

        Raises ReplayError if any step fails to advance.
        """
        for step_idx, (_, answers) in enumerate(path):
            await self._fill_answers_on_page(page, answers)

            # Click Next
            await self._click_next(page)

            try:
                await page.wait_for_load_state("networkidle", timeout=12_000)
            except Exception as exc:
                raise ReplayError(
                    f"Timeout advancing at step {step_idx}: {exc}"
                ) from exc

            await asyncio.sleep(random.uniform(1.0, 2.5))

    # ------------------------------------------------------------------
    # Page scanning
    # ------------------------------------------------------------------

    async def _scan_page(self, page) -> dict:
        """
        Extract page data using the same JS evaluation as MapperService.
        Returns a dict acceptable by SurveyGraph.add_page_node().
        """
        from app.services.mapper_service import MapperService
        mapper = MapperService(self.browser)
        result = await mapper.scan_current_page(page)
        # scan_current_page returns a SurveyPage dataclass or dict; normalise
        if hasattr(result, "__dict__"):
            import dataclasses
            return dataclasses.asdict(result) if dataclasses.is_dataclass(result) else vars(result)
        return result

    # ------------------------------------------------------------------
    # Form filling helpers
    # ------------------------------------------------------------------

    async def _fill_answers_on_page(self, page, answers: dict) -> None:
        """Fill the form on the current browser page according to answers dict."""
        for q_id, value in answers.items():
            if value is None:
                continue
            try:
                await page.evaluate(
                    """([q_id, value]) => {
                        const radio = document.querySelector(
                            `input[type=radio][value="${value}"][name="${q_id}"],`+
                            `input[type=radio][value="${value}"]`
                        );
                        if (radio) { radio.click(); return; }

                        const sel = document.querySelector(
                            `select[name="${q_id}"], select[id="${q_id}"]`
                        );
                        if (sel) {
                            sel.value = value;
                            sel.dispatchEvent(new Event('change', {bubbles:true}));
                            return;
                        }

                        const txt = document.querySelector(
                            `input[name="${q_id}"], textarea[name="${q_id}"],`+
                            `input[id="${q_id}"], textarea[id="${q_id}"]`
                        );
                        if (txt) {
                            txt.value = value;
                            txt.dispatchEvent(new Event('input', {bubbles:true}));
                            txt.dispatchEvent(new Event('change', {bubbles:true}));
                        }
                    }""",
                    [q_id, str(value)],
                )
            except Exception as exc:
                logger.debug("_fill_answers_on_page skip %s: %s", q_id, exc)

    async def _click_next(self, page) -> None:
        next_texts = ["次へ", "次のページ", "進む", "続ける", "Next", "Continue"]
        for text in next_texts:
            try:
                btn = page.get_by_role("button", name=text)
                if await btn.count() > 0:
                    await btn.first.click()
                    return
            except Exception:
                pass
        try:
            await page.evaluate(
                "() => { const s = document.querySelector('input[type=submit]'); if(s) s.click(); }"
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _inject_uid(survey_url: str, uid: str) -> str:
        import re
        if "uid=" in survey_url:
            return re.sub(r"uid=[^&]*", f"uid={uid}", survey_url)
        sep = "&" if "?" in survey_url else "?"
        return f"{survey_url}{sep}uid={uid}"

    @staticmethod
    def _generate_page_id(fingerprint: str, depth: int) -> str:
        return f"page_d{depth}_{fingerprint[:8]}"

    def _make_default_answers(self, page_data: dict) -> dict:
        answers: dict[str, str] = {}
        for q in page_data.get("questions", []):
            if q.get("honeypot"):
                continue
            q_type = q.get("q_type", "")
            options = q.get("options", [])
            if q_type in ("radio", "select", "checkbox") and options:
                answers[q["q_id"]] = options[0]["option_value"]
            elif q_type in ("text", "textarea"):
                answers[q["q_id"]] = _DUMMY_TEXT
        return answers
