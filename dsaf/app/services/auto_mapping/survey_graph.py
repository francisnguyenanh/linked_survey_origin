"""
SurveyGraph — NetworkX directed graph model for DSAF Auto-Mapping.

Node  = a page state identified by fingerprint (SHA-256)
Edge  = an answer combination that transitions from one page to another
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import networkx as nx

logger = logging.getLogger(__name__)


class SurveyGraph:
    """
    Directed graph representing all discovered pages and transitions.

    Node attributes:
        fingerprint  (str)  — SHA-256 of sorted normalized labels
        page_type    (str)  — 'questions' | 'login' | 'confirmation' | 'complete'
        questions    (list) — raw question dicts from MapperService
        url_pattern  (str)  — partial URL observed on this page
        visit_count  (int)  — how many times reached during DFS
        is_terminal  (bool) — True if this is the final/thank-you page
        depth        (int)  — first depth at which the page was seen

    Edge attributes:
        trigger_answers (dict)  — {q_id: value} that led here
        explored_count  (int)   — number of times this exact transition was confirmed
    """

    def __init__(self) -> None:
        self.G: nx.DiGraph = nx.DiGraph()
        self.root_node_id: str | None = None
        # fingerprint → page_id fast lookup
        self._fp_index: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Node management
    # ------------------------------------------------------------------

    def has_fingerprint(self, fingerprint: str) -> bool:
        return fingerprint in self._fp_index

    def get_page_id_by_fingerprint(self, fingerprint: str) -> str | None:
        return self._fp_index.get(fingerprint)

    def add_page_node(
        self,
        page_id: str,
        fingerprint: str,
        page_data: dict,
        depth: int = 0,
    ) -> None:
        self.G.add_node(
            page_id,
            fingerprint=fingerprint,
            page_type=page_data.get("page_type", "questions"),
            questions=page_data.get("questions", []),
            url_pattern=page_data.get("url_pattern", ""),
            visit_count=1,
            is_terminal=False,
            depth=depth,
        )
        self._fp_index[fingerprint] = page_id
        logger.debug("Added node %s (depth=%d)", page_id, depth)

    def mark_terminal(self, page_id: str) -> None:
        if page_id in self.G:
            self.G.nodes[page_id]["is_terminal"] = True

    def increment_visit(self, page_id: str) -> None:
        if page_id in self.G:
            self.G.nodes[page_id]["visit_count"] = (
                self.G.nodes[page_id].get("visit_count", 0) + 1
            )

    # ------------------------------------------------------------------
    # Edge management
    # ------------------------------------------------------------------

    def add_branch_edge(
        self,
        from_page_id: str,
        to_page_id: str,
        trigger_answers: dict,
    ) -> None:
        if self.G.has_edge(from_page_id, to_page_id):
            self.G.edges[from_page_id, to_page_id]["explored_count"] += 1
        else:
            self.G.add_edge(
                from_page_id,
                to_page_id,
                trigger_answers=trigger_answers,
                explored_count=1,
            )
        logger.debug(
            "Edge %s → %s  triggers=%s",
            from_page_id,
            to_page_id,
            trigger_answers,
        )

    # ------------------------------------------------------------------
    # Path queries
    # ------------------------------------------------------------------

    def get_all_paths_to_terminal(self) -> list[list[str]]:
        """Return every simple path from root to any terminal node."""
        if not self.root_node_id or self.root_node_id not in self.G:
            return []
        terminals = [
            n for n, d in self.G.nodes(data=True) if d.get("is_terminal")
        ]
        all_paths: list[list[str]] = []
        for terminal in terminals:
            try:
                for path in nx.all_simple_paths(
                    self.G, self.root_node_id, terminal, cutoff=30
                ):
                    all_paths.append(path)
            except nx.NetworkXError:
                pass
        return all_paths

    def get_stats(self) -> dict:
        terminals = [n for n, d in self.G.nodes(data=True) if d.get("is_terminal")]
        return {
            "total_pages": self.G.number_of_nodes(),
            "total_edges": self.G.number_of_edges(),
            "terminal_pages": len(terminals),
            "paths_to_terminal": len(self.get_all_paths_to_terminal()),
        }

    def to_text_tree(self) -> str:
        """Produce an ASCII tree representation of the graph."""
        if not self.root_node_id:
            return "(empty graph)"
        lines: list[str] = []

        def _walk(node_id: str, prefix: str, visited: set[str]) -> None:
            if node_id in visited:
                lines.append(f"{prefix}↺ {node_id} (cycle)")
                return
            visited.add(node_id)
            d = self.G.nodes[node_id]
            q_count = len(d.get("questions", []))
            tag = " [TERMINAL]" if d.get("is_terminal") else ""
            lines.append(f"{prefix}■ {node_id}  ({q_count}q){tag}")
            children = list(self.G.successors(node_id))
            for i, child in enumerate(children):
                edge_d = self.G.edges[node_id, child]
                triggers = edge_d.get("trigger_answers", {})
                t_str = ", ".join(f"{k}={v}" for k, v in list(triggers.items())[:3])
                connector = "└── " if i == len(children) - 1 else "├── "
                child_prefix = prefix + ("    " if i == len(children) - 1 else "│   ")
                lines.append(f"{prefix}{connector}[{t_str}]")
                _walk(child, child_prefix, visited)

        _walk(self.root_node_id, "", set())
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_json(self) -> dict:
        data = nx.node_link_data(self.G)
        data["root_node_id"] = self.root_node_id
        return data

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_json(), fh, ensure_ascii=False, indent=2)
        logger.info("SurveyGraph saved to %s", path)

    @classmethod
    def from_json(cls, data: dict) -> "SurveyGraph":
        sg = cls()
        sg.G = nx.node_link_graph(data)
        sg.root_node_id = data.get("root_node_id")
        # Rebuild fingerprint index
        for node_id, attrs in sg.G.nodes(data=True):
            fp = attrs.get("fingerprint")
            if fp:
                sg._fp_index[fp] = node_id
        return sg

    @classmethod
    def load(cls, path: Path) -> "SurveyGraph":
        with open(path, encoding="utf-8") as fh:
            return cls.from_json(json.load(fh))
