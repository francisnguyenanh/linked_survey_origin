"""
HybridMapper — Real-UID survey mapper combining Back-button and Restart strategies.

Strategy A (Back button available):
  For each trigger combination on a page: fill → Next → DFS → Back
  If back fails mid-way, fall through to Strategy B for remaining combos.

Strategy B (No back button):
  combo[0]  → continue with current UID (no extra cost)
  combo[1+] → _next_uid() + new browser context + fast replay to this page

UIDs are real (not garbage).  Terminal pages are never submitted, so used UIDs
remain available for actual survey execution after mapping.

UID cost:  1  +  Σ(N - 1)  for every no-back page with N trigger combos
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import uuid
from itertools import product as cartesian_product
from pathlib import Path
from typing import Optional

import networkx as nx

from .pattern_extractor import PatternExtractor
from .safety_guard import SafetyGuard
from .survey_graph import SurveyGraph

logger = logging.getLogger(__name__)

# Hard limits
MAX_DEPTH = 30
MAX_COMBOS = 50


class HybridMapper:
    """
    Map a survey by navigating with real UIDs, using the browser back button
    when available and fresh UIDs with fast graph-replay when it is not.
    """

    def __init__(
        self,
        browser_service,
        uid_pool: list[str],
        socketio=None,
        headless: bool = True,
    ):
        from app.services.browser_service import BrowserService  # local import

        self.browser: BrowserService = browser_service
        self.uid_pool: list[str] = list(uid_pool)
        self.uid_index: int = 0
        self.socketio = socketio
        self.headless = headless

        self.graph = SurveyGraph()
        self._visited: set[str] = set()          # fingerprints already mapped
        self._survey_url: Optional[str] = None
        self._survey_id: Optional[str] = None
        self._save_dir: Optional[Path] = None

        # Active context / page (the "primary" navigation context)
        self._current_context = None
        self._current_page = None

        # All Playwright contexts opened (never closed — available for execution)
        self._open_contexts: list = []

        # Safety guard instance (reused across calls)
        self._guard = SafetyGuard()

    # ══════════════════════════════════════════════════════════════════
    # Public entry point
    # ══════════════════════════════════════════════════════════════════

    async def map_survey(
        self,
        survey_url: str,
        survey_id: str,
        save_dir: Path,
    ) -> SurveyGraph:
        """
        Navigate *survey_url* starting with uid_pool[0], explore all branches,
        save graph+patterns, and return the completed SurveyGraph.
        """
        self._survey_url = survey_url
        self._survey_id = survey_id
        self._save_dir = Path(save_dir)
        self._save_dir.mkdir(parents=True, exist_ok=True)

        # Open the very first context
        self._current_context, self._current_page = await self._open_new_context()

        uid_url = self._inject_uid(survey_url, self._current_uid())
        await self._current_page.goto(uid_url, wait_until="networkidle")

        # Run DFS from root
        page_data = await self._scan_page(self._current_page)
        fingerprint = self._get_fingerprint(page_data)
        root_id = self._register_node(fingerprint, page_data, depth=0)
        self.graph.root_node_id = root_id

        await self._dfs(self._current_page, root_id, {}, depth=0)

        # Persist and extract patterns
        self._save_graph()
        patterns = PatternExtractor(self.graph).extract_all_patterns()

        self._emit("mapping_complete", {
            "pages": self.graph.get_stats().get("total_pages", 0),
            "branches": self.graph.get_stats().get("total_branches", 0),
            "patterns": len(patterns),
            "uids_used": self.get_used_uids(),
        })

        return self.graph

    # ══════════════════════════════════════════════════════════════════
    # DFS orchestrator
    # ══════════════════════════════════════════════════════════════════

    async def _dfs(
        self,
        page,
        parent_id: str,
        trigger_answers: dict,
        depth: int,
    ) -> None:
        """Core DFS loop.  Decides Strategy A vs B per page."""
        if depth >= MAX_DEPTH:
            self._emit("mapping_warning", {"msg": f"Max depth {MAX_DEPTH} reached", "page_id": parent_id})
            return

        page_data = await self._scan_page(page)
        fingerprint = self._get_fingerprint(page_data)

        if await self._is_terminal(page):
            self.graph.mark_terminal(parent_id)
            self._emit("mapping_terminal", {"depth": depth})
            return

        if fingerprint in self._visited and fingerprint != self._get_fingerprint(
            await self._scan_page(page)
        ):
            # Already fully explored — just add edge and return
            existing_id = self.graph.get_page_id_by_fingerprint(fingerprint)
            if existing_id and existing_id != parent_id:
                self.graph.add_branch_edge(parent_id, existing_id, trigger_answers)
            return

        self._visited.add(fingerprint)
        self._emit("mapping_new_page", {"page_id": parent_id, "depth": depth})

        triggers, data_qs = self._classify_questions(page_data)

        if not triggers:
            # No trigger questions — straight path, just advance
            await self._fill_form(page, page_data, self._merge_answers(page_data, {}))
            advanced = await self._click_next(page)
            if advanced:
                await page.wait_for_load_state("networkidle")
                child_data = await self._scan_page(page)
                child_fp = self._get_fingerprint(child_data)
                child_id = self._register_node(child_fp, child_data, depth + 1)
                self.graph.add_branch_edge(parent_id, child_id, {})
                await self._dfs(page, child_id, {}, depth + 1)
            return

        combos = self._make_combos(page_data, triggers)
        has_back = await self._has_back_button(page)

        if has_back:
            await self._explore_with_back(page, parent_id, page_data, fingerprint, combos, depth)
        else:
            await self._explore_with_restart(page, parent_id, page_data, fingerprint, combos, depth)

    # ══════════════════════════════════════════════════════════════════
    # Strategy A — back-button exploration
    # ══════════════════════════════════════════════════════════════════

    async def _explore_with_back(
        self,
        page,
        page_id: str,
        page_data: dict,
        fingerprint: str,
        combos: list[dict],
        depth: int,
    ) -> None:
        """Fill each trigger combo, advance, DFS, then hit back."""
        for idx, combo in enumerate(combos):
            self._emit("mapping_trying", {
                "strategy": "back_button",
                "combo": combo,
                "index": idx,
                "total": len(combos),
            })

            merged = self._merge_answers(page_data, combo)
            await self._fill_form(page, page_data, merged)
            advanced = await self._click_next(page)
            if not advanced:
                continue

            await page.wait_for_load_state("networkidle")
            child_data = await self._scan_page(page)
            child_fp = self._get_fingerprint(child_data)
            child_id = self._register_node(child_fp, child_data, depth + 1)
            self.graph.add_branch_edge(page_id, child_id, combo)

            # DFS down the child branch
            await self._dfs(page, child_id, combo, depth + 1)

            # Navigate back
            backed = await self._click_back(page)
            if not backed:
                self._emit("mapping_back_failed", {"page_id": page_id, "combo_index": idx})
                # Fall through to restart strategy for remaining combos
                remaining = combos[idx + 1:]
                if remaining:
                    await self._explore_remaining_with_restart(
                        page, page_id, page_data, remaining, depth
                    )
                return

            await page.wait_for_load_state("networkidle")

    # ══════════════════════════════════════════════════════════════════
    # Strategy B — restart with fresh UIDs
    # ══════════════════════════════════════════════════════════════════

    async def _explore_with_restart(
        self,
        page,
        page_id: str,
        page_data: dict,
        fingerprint: str,
        combos: list[dict],
        depth: int,
    ) -> None:
        """
        combo[0]  → use current context (no extra UID)
        combo[1+] → new UID context + fast replay to this page, then DFS
        """
        for idx, combo in enumerate(combos):
            if idx == 0:
                # Continue on current context
                self._emit("mapping_trying", {
                    "strategy": "continue_current_uid",
                    "combo": combo,
                    "index": idx,
                    "total": len(combos),
                    "uid": self._current_uid(),
                })
                merged = self._merge_answers(page_data, combo)
                await self._fill_form(page, page_data, merged)
                advanced = await self._click_next(page)
                if not advanced:
                    continue

                await page.wait_for_load_state("networkidle")
                child_data = await self._scan_page(page)
                child_fp = self._get_fingerprint(child_data)
                child_id = self._register_node(child_fp, child_data, depth + 1)
                self.graph.add_branch_edge(page_id, child_id, combo)
                await self._dfs(page, child_id, combo, depth + 1)
            else:
                # Open fresh context with next UID
                next_uid = self._next_uid()
                if next_uid is None:
                    self._emit("mapping_warning", {
                        "msg": f"UID pool exhausted — skipping combo {idx}/{len(combos)-1}",
                        "page_id": page_id,
                    })
                    continue

                self._emit("mapping_trying", {
                    "strategy": "new_uid_restart",
                    "combo": combo,
                    "index": idx,
                    "total": len(combos),
                    "uid": next_uid,
                })

                new_ctx, new_page = await self._open_new_context()
                uid_url = self._inject_uid(self._survey_url, next_uid)
                await new_page.goto(uid_url, wait_until="networkidle")

                reached = await self._fast_replay_to(new_page, page_id)
                if not reached:
                    self._emit("mapping_warning", {
                        "msg": f"Fast replay failed for combo {idx}",
                        "page_id": page_id,
                    })
                    continue

                # Now new_page is at page_id — apply this combo and DFS
                fresh_data = await self._scan_page(new_page)
                merged = self._merge_answers(fresh_data, combo)
                await self._fill_form(new_page, fresh_data, merged)
                advanced = await self._click_next(new_page)
                if not advanced:
                    continue

                await new_page.wait_for_load_state("networkidle")
                child_data = await self._scan_page(new_page)
                child_fp = self._get_fingerprint(child_data)
                child_id = self._register_node(child_fp, child_data, depth + 1)
                self.graph.add_branch_edge(page_id, child_id, combo)

                # Update primary page reference and run DFS
                self._current_page = new_page
                await self._dfs(new_page, child_id, combo, depth + 1)

    async def _explore_remaining_with_restart(
        self,
        page,
        page_id: str,
        page_data: dict,
        remaining_combos: list[dict],
        depth: int,
    ) -> None:
        """Called when back failed — handle leftover combos via restart strategy."""
        for idx, combo in enumerate(remaining_combos):
            next_uid = self._next_uid()
            if next_uid is None:
                self._emit("mapping_warning", {
                    "msg": "UID pool exhausted during back-fail recovery",
                    "page_id": page_id,
                })
                return

            self._emit("mapping_trying", {
                "strategy": "new_uid_restart",
                "combo": combo,
                "index": idx,
                "total": len(remaining_combos),
                "uid": next_uid,
            })

            new_ctx, new_page = await self._open_new_context()
            uid_url = self._inject_uid(self._survey_url, next_uid)
            await new_page.goto(uid_url, wait_until="networkidle")

            reached = await self._fast_replay_to(new_page, page_id)
            if not reached:
                self._emit("mapping_warning", {
                    "msg": f"Fast replay failed (back-fail recovery) combo {idx}",
                    "page_id": page_id,
                })
                continue

            fresh_data = await self._scan_page(new_page)
            merged = self._merge_answers(fresh_data, combo)
            await self._fill_form(new_page, fresh_data, merged)
            advanced = await self._click_next(new_page)
            if not advanced:
                continue

            await new_page.wait_for_load_state("networkidle")
            child_data = await self._scan_page(new_page)
            child_fp = self._get_fingerprint(child_data)
            child_id = self._register_node(child_fp, child_data, depth + 1)
            self.graph.add_branch_edge(page_id, child_id, combo)

            self._current_page = new_page
            await self._dfs(new_page, child_id, combo, depth + 1)

    # ══════════════════════════════════════════════════════════════════
    # Fast replay
    # ══════════════════════════════════════════════════════════════════

    async def _fast_replay_to(self, page, target_page_id: str) -> bool:
        """
        Replay the shortest path from root to *target_page_id* using stored
        graph answers.  Returns True if the target fingerprint is confirmed.
        """
        try:
            path_nodes = nx.shortest_path(
                self.graph.G, self.graph.root_node_id, target_page_id
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            logger.warning("_fast_replay_to: no path to %s", target_page_id)
            return False

        # path_nodes = [root, n1, n2, ..., target]
        # We need the edges to get the answers stored on each hop
        for i in range(len(path_nodes) - 1):
            src = path_nodes[i]
            dst = path_nodes[i + 1]
            edge_data = self.graph.G.edges[src, dst]
            stored_answers: dict = edge_data.get("trigger_answers", {})

            # Fill and advance
            page_data = await self._scan_page(page)
            await self._fill_form(page, page_data, stored_answers)
            advanced = await self._click_next(page)
            if not advanced:
                logger.warning("_fast_replay_to: click_next returned False at step %d", i)
                return False

            await asyncio.sleep(random.uniform(1.0, 2.0))
            await page.wait_for_load_state("networkidle")

        # Verify we're at the expected page
        current_data = await self._scan_page(page)
        current_fp = self._get_fingerprint(current_data)
        expected_fp = self.graph.G.nodes[target_page_id].get("fingerprint", "")
        if current_fp != expected_fp:
            logger.warning(
                "_fast_replay_to: fingerprint mismatch — expected %s got %s",
                expected_fp, current_fp,
            )
            return False

        return True

    # ══════════════════════════════════════════════════════════════════
    # Page-level helpers
    # ══════════════════════════════════════════════════════════════════

    async def _scan_page(self, page) -> dict:
        """Delegate to MapperService.scan_current_page() and normalise."""
        from app.services.mapper_service import MapperService
        mapper = MapperService(self.browser)
        result = await mapper.scan_current_page(page)
        if hasattr(result, "__dict__"):
            import dataclasses
            return dataclasses.asdict(result) if dataclasses.is_dataclass(result) else vars(result)
        return result if isinstance(result, dict) else {}

    async def _is_terminal(self, page) -> bool:
        """Return True if this page is a survey terminal (submit / completion)."""
        return await self._guard.is_terminal_page(page)

    async def _has_back_button(self, page) -> bool:
        """Return True if a visible back-navigation button/link exists."""
        try:
            result = await page.evaluate("""
                () => {
                    const texts = ['戻る', 'Back', '前へ', 'もどる', '前の'];
                    for (const t of texts) {
                        const els = Array.from(
                            document.querySelectorAll('a, button, input[type=button], input[type=submit]')
                        );
                        for (const el of els) {
                            const txt = (el.textContent || el.value || '').trim();
                            if (txt.includes(t)) {
                                const style = window.getComputedStyle(el);
                                const rect = el.getBoundingClientRect();
                                if (style.display !== 'none' && style.visibility !== 'hidden'
                                        && rect.width > 0 && rect.height > 0) {
                                    return true;
                                }
                            }
                        }
                    }
                    return false;
                }
            """)
            return bool(result)
        except Exception:
            return False

    async def _fill_form(self, page, page_data: dict, answers: dict) -> None:
        """Fill form fields using the q_id → value mapping in *answers*."""
        for q_id, value in answers.items():
            if value is None:
                continue
            try:
                await page.evaluate(
                    """([q_id, value]) => {
                        const radio = document.querySelector(
                            `input[type=radio][value="${value}"][name="${q_id}"]`
                        ) || document.querySelector(
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
                            `input[name="${q_id}"], textarea[name="${q_id}"],` +
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
                logger.debug("_fill_form skip %s: %s", q_id, exc)

    async def _click_next(self, page) -> bool:
        """Click the 'Next' button.  Returns True if a button was found."""
        next_texts = ["次へ", "次のページ", "進む", "続ける", "Next", "Continue", "次"]
        for text in next_texts:
            try:
                btn = page.get_by_role("button", name=re.compile(text, re.IGNORECASE))
                if await btn.count() > 0:
                    await btn.first.click()
                    return True
            except Exception:
                pass
        # Fallback: input[type=submit]
        try:
            clicked = await page.evaluate("""
                () => {
                    const s = document.querySelector('input[type=submit]');
                    if (s) { s.click(); return true; }
                    return false;
                }
            """)
            return bool(clicked)
        except Exception:
            return False

    async def _click_back(self, page) -> bool:
        """Click the 'Back' button.  Returns True if a button was found."""
        back_texts = ["戻る", "Back", "前へ", "もどる", "前の"]
        try:
            result = await page.evaluate("""
                (backTexts) => {
                    const els = Array.from(
                        document.querySelectorAll('a, button, input[type=button], input[type=submit]')
                    );
                    for (const el of els) {
                        const txt = (el.textContent || el.value || '').trim();
                        if (backTexts.some(t => txt.includes(t))) {
                            const style = window.getComputedStyle(el);
                            const rect = el.getBoundingClientRect();
                            if (style.display !== 'none' && style.visibility !== 'hidden'
                                    && rect.width > 0 && rect.height > 0) {
                                el.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }
            """, back_texts)
            return bool(result)
        except Exception:
            return False

    # ══════════════════════════════════════════════════════════════════
    # Question classification and combo generation
    # ══════════════════════════════════════════════════════════════════

    def _classify_questions(self, page_data: dict) -> tuple[list[str], list[str]]:
        """
        Split questions into trigger (cause branching) and data (free answers).

        Trigger heuristic: radio or select with 2–8 options.
        """
        questions = page_data.get("questions", [])
        triggers: list[str] = []
        data_qs: list[str] = []

        for q in questions:
            q_id = q.get("q_id") or q.get("name") or q.get("id", "")
            q_type = (q.get("type") or q.get("input_type") or "").lower()
            options = q.get("options", [])
            n_opts = len(options)

            if q_type in ("radio", "select") and 2 <= n_opts <= 8:
                triggers.append(q_id)
            else:
                data_qs.append(q_id)

        return triggers, data_qs

    def _make_combos(self, page_data: dict, trigger_q_ids: list[str]) -> list[dict]:
        """
        Return a capped list of all trigger-option combinations.

        Each combo is {q_id: option_value, ...}.
        """
        questions = {
            (q.get("q_id") or q.get("name") or q.get("id", "")): q
            for q in page_data.get("questions", [])
        }

        option_lists: list[list] = []
        for q_id in trigger_q_ids:
            q = questions.get(q_id)
            if q is None:
                continue
            opts = [
                (opt.get("value") or opt) if isinstance(opt, dict) else opt
                for opt in q.get("options", [])
            ]
            if opts:
                option_lists.append([(q_id, v) for v in opts])

        if not option_lists:
            return [{}]

        combos: list[dict] = []
        for raw_combo in cartesian_product(*option_lists):
            if len(combos) >= MAX_COMBOS:
                break
            combos.append(dict(raw_combo))

        return combos or [{}]

    def _merge_answers(self, page_data: dict, trigger_combo: dict) -> dict:
        """
        Build a complete answer dict: trigger_combo values + _default_value()
        for questions not in the combo.
        """
        answers: dict = {}
        for q in page_data.get("questions", []):
            q_id = q.get("q_id") or q.get("name") or q.get("id", "")
            if q_id in trigger_combo:
                answers[q_id] = trigger_combo[q_id]
            else:
                answers[q_id] = self._default_value(q)
        return answers

    def _default_value(self, question: dict):
        """Pick a safe default for a non-trigger question."""
        q_type = (question.get("type") or question.get("input_type") or "").lower()
        options = question.get("options", [])

        if q_type in ("radio", "checkbox", "select") and options:
            first = options[0]
            return first.get("value") if isinstance(first, dict) else first

        if q_type in ("text", "textarea", "email"):
            return "テスト"

        if q_type == "number":
            return "20"

        return None

    # ══════════════════════════════════════════════════════════════════
    # UID management
    # ══════════════════════════════════════════════════════════════════

    def _current_uid(self) -> str:
        """Return the uid currently active (may be None if pool exhausted)."""
        if self.uid_index < len(self.uid_pool):
            return self.uid_pool[self.uid_index]
        return self.uid_pool[-1] if self.uid_pool else ""

    def _next_uid(self) -> Optional[str]:
        """Advance to the next UID in the pool and return it, or None."""
        self.uid_index += 1
        if self.uid_index < len(self.uid_pool):
            return self.uid_pool[self.uid_index]
        return None

    def get_used_uids(self) -> list[str]:
        """UIDs that have been navigated so far."""
        return self.uid_pool[: self.uid_index + 1]

    def get_unused_uids(self) -> list[str]:
        """UIDs in the pool that have not yet been touched."""
        return self.uid_pool[self.uid_index + 1:]

    # ══════════════════════════════════════════════════════════════════
    # Node / graph helpers
    # ══════════════════════════════════════════════════════════════════

    def _register_node(self, fingerprint: str, page_data: dict, depth: int) -> str:
        """Add page to graph if not already there; return its page_id."""
        existing_id = self.graph.get_page_id_by_fingerprint(fingerprint)
        if existing_id:
            return existing_id

        page_id = f"page_d{depth}_{fingerprint[:8]}"
        self.graph.add_page_node(
            page_id=page_id,
            fingerprint=fingerprint,
            questions=page_data.get("questions", []),
            depth=depth,
            raw_data=page_data,
        )
        return page_id

    def _get_fingerprint(self, page_data: dict) -> str:
        """
        Build a stable fingerprint from the question IDs + option counts present
        on the page.  Same algorithm as SurveyGraph / DFSExplorer.
        """
        import hashlib
        questions = page_data.get("questions", [])
        parts = []
        for q in questions:
            q_id = q.get("q_id") or q.get("name") or q.get("id", "")
            n_opts = len(q.get("options", []))
            parts.append(f"{q_id}:{n_opts}")
        key = "|".join(parts)
        return hashlib.md5(key.encode()).hexdigest()

    def _get_path_to(self, target_page_id: str) -> list[tuple[str, dict]]:
        """
        Return [(page_id, full_answers), ...] for the shortest path from root
        to *target_page_id*.
        """
        try:
            nodes = nx.shortest_path(
                self.graph.G, self.graph.root_node_id, target_page_id
            )
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return []

        path: list[tuple[str, dict]] = []
        for i in range(len(nodes) - 1):
            edge_data = self.graph.G.edges[nodes[i], nodes[i + 1]]
            path.append((nodes[i], edge_data.get("trigger_answers", {})))
        return path

    def _save_graph(self) -> None:
        if self._save_dir and self._survey_id:
            graph_path = self._save_dir / f"{self._survey_id}.graph.json"
            self.graph.save(graph_path)

    # ══════════════════════════════════════════════════════════════════
    # Browser helpers
    # ══════════════════════════════════════════════════════════════════

    async def _open_new_context(self):
        """Open a new Playwright browser context via BrowserService and track it."""
        ctx, page = await self.browser.create_context()
        self._open_contexts.append(ctx)
        return ctx, page

    @staticmethod
    def _inject_uid(survey_url: str, uid: str) -> str:
        if "uid=" in survey_url:
            return re.sub(r"uid=[^&]*", f"uid={uid}", survey_url)
        sep = "&" if "?" in survey_url else "?"
        return f"{survey_url}{sep}uid={uid}"

    # ══════════════════════════════════════════════════════════════════
    # SocketIO helper
    # ══════════════════════════════════════════════════════════════════

    def _emit(self, event: str, data: dict) -> None:
        if self.socketio:
            try:
                self.socketio.emit(event, data)
            except Exception as exc:
                logger.debug("_emit %s failed: %s", event, exc)
