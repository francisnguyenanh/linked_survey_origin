"""
PatternExtractor — Generates Pattern dicts from all paths in a SurveyGraph.

Each root→terminal path becomes one Pattern:
  - Trigger answers on edges  → "fixed" strategy
  - Non-trigger questions     → "random_option" strategy
  - Text questions            → "random_from_list" strategy (dummy pool)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .survey_graph import SurveyGraph

logger = logging.getLogger(__name__)

_DUMMY_TEXT_POOL = [
    "テスト回答", "サンプル", "回答テスト", "テスト記入", "仮入力",
]


class PatternExtractor:
    """
    Reads a completed SurveyGraph and generates one Pattern dict per
    root-to-terminal path.
    """

    def __init__(
        self,
        survey_graph: SurveyGraph,
        survey_id: str,
        uid_pool: list[str] | None = None,
    ) -> None:
        self.graph = survey_graph
        self.survey_id = survey_id
        self.uid_pool = uid_pool or []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def extract_all_patterns(self) -> list[dict]:
        """
        Return a list of pattern dicts (schema v1.1), one per unique path.
        """
        paths = self.graph.get_all_paths_to_terminal()
        if not paths:
            # No terminal pages found — generate one pattern per leaf node
            paths = self._fallback_paths()

        patterns = []
        for i, path in enumerate(paths):
            try:
                pattern = self._path_to_pattern(path, pattern_index=i)
                patterns.append(pattern)
                logger.debug("Extracted pattern %03d: %d pages", i, len(path))
            except Exception as exc:
                logger.warning("Failed to extract pattern %d: %s", i, exc)

        logger.info("PatternExtractor: %d patterns from %d paths", len(patterns), len(paths))
        return patterns

    # ------------------------------------------------------------------
    # Path → Pattern conversion
    # ------------------------------------------------------------------

    def _path_to_pattern(self, path: list[str], pattern_index: int) -> dict:
        """Convert a single path (list of node_ids) into a Pattern dict."""
        answers: dict[str, dict] = {}
        name_parts: list[str] = []

        g = self.graph.G

        for i, page_id in enumerate(path):
            node_data = g.nodes[page_id]
            questions: list[dict] = node_data.get("questions", [])

            # Determine which q_ids on this page were trigger answers
            # by looking at the outgoing edge to the next page
            trigger_q_ids: set[str] = set()
            trigger_answers: dict[str, str] = {}
            if i < len(path) - 1:
                next_page_id = path[i + 1]
                if g.has_edge(page_id, next_page_id):
                    edge_data = g.edges[page_id, next_page_id]
                    trigger_answers = edge_data.get("trigger_answers", {})
                    trigger_q_ids = set(trigger_answers.keys())
                    # Only keep the first 3 trigger values in the pattern name
                    if len(name_parts) < 3:
                        for q_id, val in list(trigger_answers.items())[:2]:
                            name_parts.append(f"{q_id[-4:]}={val}")

            page_answers: dict[str, dict] = {}
            for q in questions:
                if q.get("honeypot"):
                    continue
                q_id = q["q_id"]
                q_type = q.get("q_type", "text")
                options = q.get("options", [])

                if q_id in trigger_q_ids:
                    # Fixed strategy: use the value that caused this branch
                    page_answers[q_id] = {
                        "strategy": "fixed",
                        "value": trigger_answers[q_id],
                        "values": None,
                    }
                elif q_type in ("radio", "select", "checkbox"):
                    page_answers[q_id] = {
                        "strategy": "random_option",
                        "value": None,
                        "values": None,
                        "exclude_indices": [],
                    }
                else:
                    # text / textarea
                    page_answers[q_id] = {
                        "strategy": "random_from_list",
                        "value": None,
                        "values": list(_DUMMY_TEXT_POOL),
                    }

            if page_answers:
                answers[page_id] = page_answers

        # Compute timing based on path length (10–30 s per page)
        page_count = len(path)
        pattern_name = f"auto_pattern_{pattern_index:03d}"
        if name_parts:
            pattern_name += "__" + "_".join(name_parts)

        now = datetime.now(timezone.utc).isoformat()

        return {
            "schema_version": "1.1",
            "pattern_id": f"auto_{pattern_index:03d}",
            "pattern_name": pattern_name,
            "description": f"Auto-generated pattern #{pattern_index} via DFS ({page_count} pages)",
            "linked_survey_id": self.survey_id,
            "created_at": now,
            "uid_pool": list(self.uid_pool),
            "uid_strategy": "random",
            "answers": answers,
            "timing": {
                "min_total_seconds": page_count * 10,
                "max_total_seconds": page_count * 30,
                "page_delay_min": 3.0,
                "page_delay_max": 8.0,
                "typing_delay_per_char_ms": [50, 150],
            },
            "branch_path": path,
            "branch_ids_used": [],
            "auto_generated": True,
            "auto_generated_from_mapping": True,
            "requires_branch_match": True,
        }

    # ------------------------------------------------------------------
    # Fallback
    # ------------------------------------------------------------------

    def _fallback_paths(self) -> list[list[str]]:
        """If no terminal nodes exist, return paths to all leaf nodes."""
        g = self.graph.G
        if not self.graph.root_node_id:
            return []
        leaves = [n for n in g.nodes if g.out_degree(n) == 0]
        paths = []
        for leaf in leaves:
            try:
                import networkx as nx
                for p in nx.all_simple_paths(g, self.graph.root_node_id, leaf, cutoff=25):
                    paths.append(p)
            except Exception:
                pass
        return paths or [[self.graph.root_node_id]] if self.graph.root_node_id else []
