"""
AutoMappingEngine — Orchestrator for the full auto-mapping pipeline.

Pipeline:
  1. Validate safe_uid
  2. Build TriggerAnalyzer, DFSExplorer, SafetyGuard, RateLimitManager
  3. Run DFSExplorer.explore()
  4. Save SurveyGraph to data/maps/{survey_id}.graph.json
  5. Run PatternExtractor.extract_all_patterns()
  6. Save each generated pattern to data/patterns/
  7. Emit summary via SocketIO

SocketIO events emitted:
  "mapping_progress"   { job_id, branch_count, depth, page_id, status, message }
  "mapping_new_branch" { job_id, from_page, to_page, trigger_answers }
  "mapping_complete"   { job_id, total_pages, total_edges, patterns_generated,
                         duration_seconds, coverage_estimate_pct }
  "mapping_error"      { job_id, message }
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from app.services.browser_service import BrowserService
from app.services.proxy_service import ProxyService
from .dfs_explorer import DFSExplorer, MAX_DEPTH, MAX_BRANCHES
from .pattern_extractor import PatternExtractor
from .rate_limit_manager import RateLimitManager
from .safety_guard import SafetyGuard
from .survey_graph import SurveyGraph
from .trigger_analyzer import TriggerAnalyzer

logger = logging.getLogger(__name__)


class AutoMappingEngine:
    """
    Top-level orchestrator that ties all auto-mapping components together.
    """

    def __init__(
        self,
        maps_dir: Path,
        patterns_dir: Path,
        proxy_service: ProxyService | None = None,
        socketio=None,
        headless: bool = True,
    ) -> None:
        self.maps_dir = Path(maps_dir)
        self.patterns_dir = Path(patterns_dir)
        self.proxy_service = proxy_service
        self.socketio = socketio
        self.headless = headless

        self._stop_flags: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(
        self,
        job_id: str,
        survey_url: str,
        safe_uid: str,
        survey_id: str,
        max_depth: int = MAX_DEPTH,
        max_branches: int = MAX_BRANCHES,
        uid_pool_for_patterns: list[str] | None = None,
    ) -> dict:
        """
        Execute the full auto-mapping pipeline for the given survey URL.

        Returns a summary dict:
          {
            "survey_id": str,
            "total_pages": int,
            "total_edges": int,
            "patterns_generated": int,
            "graph_file": str,
            "pattern_files": list[str],
            "coverage_estimate_pct": float,
            "duration_seconds": float,
          }
        """
        start_ts = time.monotonic()
        self._stop_flags[job_id] = False

        self._emit("mapping_progress", {
            "job_id": job_id,
            "status": "initializing",
            "message": f"Starting auto-mapping for {survey_id}",
        })

        # ── Build components ──────────────────────────────────────────
        browser = BrowserService(headless=self.headless)
        safety = SafetyGuard(safe_uid_pool=[safe_uid])
        rate_limiter = RateLimitManager(
            proxy_service=self.proxy_service,
            base_min=5.0,
            base_max=15.0,
        )
        analyzer = TriggerAnalyzer(browser, safe_uid)

        def on_progress(event: str, data: dict) -> None:
            data["job_id"] = job_id
            if event == "new_page":
                self._emit("mapping_progress", {**data, "status": "new_page"})
            elif event == "terminal":
                self._emit("mapping_progress", {**data, "status": "terminal"})
            elif event == "revisit":
                self._emit("mapping_progress", {**data, "status": "revisit"})
            elif event == "branch_start":
                self._emit("mapping_new_branch", data)
            elif event == "error":
                self._emit("mapping_progress", {**data, "status": "error"})

        explorer = DFSExplorer(
            browser_service=browser,
            trigger_analyzer=analyzer,
            safety_guard=safety,
            rate_limit_manager=rate_limiter,
            on_progress=on_progress,
        )

        # Respect stop requests
        async def _stop_watcher():
            while not self._stop_flags.get(job_id):
                await asyncio.sleep(1)
            explorer.stop()

        watcher_task = asyncio.create_task(_stop_watcher())

        # ── DFS exploration ───────────────────────────────────────────
        try:
            survey_graph = await explorer.explore(survey_url, safe_uid)
        finally:
            watcher_task.cancel()
            try:
                await watcher_task
            except asyncio.CancelledError:
                pass
            await browser.close_all()

        stats = survey_graph.get_stats()
        logger.info("DFS complete: %s", stats)

        # ── Save graph ────────────────────────────────────────────────
        self.maps_dir.mkdir(parents=True, exist_ok=True)
        graph_path = self.maps_dir / f"{survey_id}.graph.json"
        survey_graph.save(graph_path)
        logger.info("Graph saved to %s", graph_path)

        # Also save a minimal survey map JSON compatible with existing MapperService format
        map_path = self.maps_dir / f"{survey_id}.map.json"
        self._save_compat_map(survey_id, survey_url, survey_graph, map_path)

        # ── Extract patterns ──────────────────────────────────────────
        extractor = PatternExtractor(
            survey_graph,
            survey_id=survey_id,
            uid_pool=uid_pool_for_patterns or [],
        )
        patterns = extractor.extract_all_patterns()
        pattern_files: list[str] = []

        self.patterns_dir.mkdir(parents=True, exist_ok=True)
        for pattern in patterns:
            pid = pattern["pattern_id"]
            p_path = self.patterns_dir / f"{pid}.json"
            with open(p_path, "w", encoding="utf-8") as fh:
                json.dump(pattern, fh, ensure_ascii=False, indent=2)
            pattern_files.append(str(p_path))

        duration = time.monotonic() - start_ts
        paths_count = len(survey_graph.get_all_paths_to_terminal())
        total_combos = max(paths_count, 1)
        coverage_pct = round(
            min(100.0, (stats["terminal_pages"] / max(total_combos, 1)) * 100), 1
        )

        summary = {
            "survey_id": survey_id,
            "total_pages": stats["total_pages"],
            "total_edges": stats["total_edges"],
            "patterns_generated": len(patterns),
            "graph_file": str(graph_path),
            "pattern_files": pattern_files,
            "coverage_estimate_pct": coverage_pct,
            "duration_seconds": round(duration, 1),
            "branches_explored": explorer.branches_explored,
        }

        self._emit("mapping_complete", {**summary, "job_id": job_id})
        logger.info("AutoMappingEngine complete: %s", summary)
        return summary

    def stop(self, job_id: str) -> bool:
        """Signal a running job to stop gracefully."""
        if job_id in self._stop_flags:
            self._stop_flags[job_id] = True
            return True
        return False

    # ------------------------------------------------------------------
    # Time estimation (before running)
    # ------------------------------------------------------------------

    def estimate_time(self, trigger_option_matrix: dict) -> dict:
        """
        Pre-run estimation based on known trigger option counts.

        Returns:
            {
              "estimated_branches": int,
              "estimated_minutes": float,
              "warning": str | None,
            }
        """
        total = 1
        for opts in trigger_option_matrix.values():
            total *= len(opts)

        # ~1.5 min per branch (replay + analysis + rate-limit delay)
        estimated_minutes = total * 1.5
        warning = None
        if total > 100:
            warning = (
                f"Estimated {total} branches (~{estimated_minutes:.0f} min). "
                "Consider reducing max_branches or the number of trigger options."
            )

        return {
            "estimated_branches": total,
            "estimated_minutes": round(estimated_minutes, 1),
            "warning": warning,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _emit(self, event: str, data: dict) -> None:
        if self.socketio:
            try:
                self.socketio.emit(event, data)
            except Exception as exc:
                logger.debug("SocketIO emit failed (%s): %s", event, exc)

    def _save_compat_map(
        self,
        survey_id: str,
        survey_url: str,
        survey_graph: SurveyGraph,
        map_path: Path,
    ) -> None:
        """
        Save a flat survey map JSON (v1.1) compatible with PatternService / ExecutorService.
        Pages are derived from graph nodes in BFS order from root.
        """
        from datetime import datetime, timezone

        pages = []
        g = survey_graph.G

        if survey_graph.root_node_id:
            try:
                import networkx as nx
                bfs_order = list(nx.bfs_tree(g, survey_graph.root_node_id).nodes())
            except Exception:
                bfs_order = list(g.nodes())
        else:
            bfs_order = list(g.nodes())

        for idx, node_id in enumerate(bfs_order):
            node_data = g.nodes[node_id]
            pages.append({
                "page_id": node_id,
                "page_index": idx,
                "url_pattern": node_data.get("url_pattern", ""),
                "page_fingerprint": node_data.get("fingerprint", ""),
                "page_type": node_data.get("page_type", "questions"),
                "questions": node_data.get("questions", []),
                "navigation": {
                    "submit_button_text": "次へ",
                    "submit_selector": "input[type=submit]",
                    "method": "submit",
                },
                "branching_hints": [],
                "is_terminal": node_data.get("is_terminal", False),
            })

        compat_map = {
            "schema_version": "1.1",
            "survey_id": survey_id,
            "base_url": survey_url,
            "url_params": {"uid": "{uid_placeholder}"},
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pages": pages,
            "branch_tree": {
                "root_page_id": survey_graph.root_node_id,
                "nodes": {
                    node_id: {
                        "page_id": node_id,
                        "fingerprint": g.nodes[node_id].get("fingerprint", ""),
                        "outgoing_branches": [
                            {
                                "to_page_id": succ,
                                "trigger_answers": g.edges[node_id, succ].get("trigger_answers", {}),
                            }
                            for succ in g.successors(node_id)
                        ],
                    }
                    for node_id in g.nodes()
                },
            },
            "discovery_sessions": [],
            "coverage_stats": survey_graph.get_stats(),
        }

        with open(map_path, "w", encoding="utf-8") as fh:
            json.dump(compat_map, fh, ensure_ascii=False, indent=2)
        logger.info("Compat map saved to %s", map_path)
