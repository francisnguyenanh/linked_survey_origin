"""
Shadow Mode — Passive observation while a real user navigates the survey.

Classes:
  ShadowObserver       — Attaches read-only hooks to a Playwright page,
                         fingerprints each new page, and updates SurveyGraph.
  ShadowMappingSession — Opens a visible browser, attaches ShadowObserver,
                         and waits until the session ends.
  AssistantOverlay     — Injects a translucent hint-panel into the survey page
                         so the user can see real-time coverage/suggestions.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .survey_graph import SurveyGraph

logger = logging.getLogger(__name__)

# POST body keys that are navigation meta-fields, not real answers
_SKIP_KEYS = {
    "next", "back", "token", "csrf", "_token", "sid", "page",
    "submit", "btn", "button",
}


# ══════════════════════════════════════════════════════════════════════
# ShadowObserver
# ══════════════════════════════════════════════════════════════════════

class ShadowObserver:
    """
    Passively observe a Playwright page while the user navigates the survey.

    Hooks used (read-only — no request blocking):
      page.on("request")  → capture POST body (answer data)
      page.on("response") → detect when a new HTML page loads
    """

    def __init__(
        self,
        survey_graph: SurveyGraph,
        socketio=None,
        save_path=None,
        assisted: bool = False,
    ):
        self.graph = survey_graph
        self.socketio = socketio
        self._save_path = save_path
        self.assisted = assisted

        self._current_page_id: Optional[str] = None
        self._pending_answers: dict = {}
        self._pending_action: str = "next"
        self._session_path: list[tuple[str, dict]] = []   # (page_id, answers)
        self._visited: set[str] = set()

        # AssistantOverlay instance — injected lazily when page is available
        self._overlay: Optional[AssistantOverlay] = None
        self._page = None   # strong ref to prevent GC of the active page

    # ── Public ─────────────────────────────────────────────────────────

    async def attach(self, page) -> None:
        """
        Attach read-only event hooks to *page*.
        Call this once after page.goto() has loaded the first survey page.
        """
        self._page = page
        page.on("request",  self._on_request)
        page.on("response", self._on_response)

        if self.assisted:
            self._overlay = AssistantOverlay()
            await self._overlay.inject(page)

        # Scan first page immediately
        await self._process_new_page(page)

    def get_session_pattern(self) -> dict:
        """
        Build a pattern dict from the current session path.
        Call after the user submits or the session is stopped.
        """
        if not self._session_path:
            return {}

        answers: dict = {}
        for page_id, page_answers in self._session_path:
            if page_answers:
                answers[page_id] = {
                    q_id: {"strategy": "fixed", "value": val}
                    for q_id, val in page_answers.items()
                }

        path_ids = [p for p, _ in self._session_path]
        return {
            "schema_version": "1.1",
            "pattern_id": f"manual_{int(time.time())}",
            "pattern_name": f"Manual session {datetime.now().strftime('%Y%m%d_%H%M')}",
            "source": "manual_shadow",
            "branch_path": path_ids,
            "answers": answers,
            "timing": {
                "min_total_seconds": 90,
                "max_total_seconds": 300,
                "page_delay_min": 3.0,
                "page_delay_max": 8.0,
                "typing_delay_per_char_ms": [50, 150],
            },
        }

    # ── Event hooks ────────────────────────────────────────────────────

    def _on_request(self, request) -> None:
        """Synchronous event handler — schedule async work in the loop."""
        if request.method != "POST":
            return
        asyncio.ensure_future(self._capture_post(request))

    def _on_response(self, response) -> None:
        """Synchronous event handler — schedule async work in the loop."""
        asyncio.ensure_future(self._handle_response(response))

    async def _capture_post(self, request) -> None:
        """Parse the POST body and store answers + action."""
        try:
            post_data: str = request.post_data or ""
            if not post_data:
                return

            parsed = parse_qs(post_data, keep_blank_values=True)

            # Determine navigation action
            action = "next"
            if any(k in parsed for k in ("back", "戻る")):
                action = "back"
            elif any(k in parsed for k in ("submit", "finish", "send", "確認して送信")):
                action = "submit"
            else:
                # Check by value — some surveys use value="back"
                for vals in parsed.values():
                    if any("戻" in v or v.lower() == "back" for v in vals):
                        action = "back"
                        break

            answers = {
                k: v[0]
                for k, v in parsed.items()
                if k.lower() not in _SKIP_KEYS
            }

            self._pending_answers = answers
            self._pending_action = action

            self._emit("shadow_post_captured", {
                "action": action,
                "field_count": len(answers),
                "answers_preview": dict(list(answers.items())[:3]),
            })

        except Exception as exc:
            self._emit("shadow_warning", {"msg": f"POST capture error: {exc}"})

    async def _handle_response(self, response) -> None:
        """Detect when an HTML page loads after the user navigates."""
        if response.status not in (200, 302):
            return
        content_type = response.headers.get("content-type", "")
        if "text/html" not in content_type:
            return
        try:
            page = response.frame.page
            await page.wait_for_load_state("networkidle", timeout=10_000)
            await self._process_new_page(page)
        except Exception:
            pass

    # ── Core page processor ────────────────────────────────────────────

    async def _process_new_page(self, page) -> None:
        """
        Fingerprint the current page, classify it (new / known / terminal),
        update the graph, and emit the appropriate SocketIO event.
        """
        try:
            page_data = await self._scan_page(page)
        except Exception as exc:
            self._emit("shadow_warning", {"msg": f"scan_page error: {exc}"})
            return

        fingerprint = self._make_fingerprint(page_data)
        answers = self._pending_answers
        action = self._pending_action

        # Reset pending state
        self._pending_answers = {}
        self._pending_action = "next"

        # ── Terminal check ───────────────────────────────────────────
        if await self._is_terminal(page):
            terminal_pid = self._register_node(fingerprint, page_data, depth=len(self._session_path))
            if self._current_page_id and answers:
                self.graph.add_branch_edge(self._current_page_id, terminal_pid, answers)
            self.graph.mark_terminal(terminal_pid)

            self._emit("shadow_terminal_warning", {
                "msg": "⚠ Đây là trang Submit cuối cùng!",
                "advice": (
                    "Sau khi Submit, UID này sẽ không dùng lại được. "
                    "Hãy chắc chắn đây là lần chạy thật."
                ),
            })

            if self._overlay:
                try:
                    await self._overlay.show_terminal_warning(page)
                except Exception:
                    pass
            return

        # ── Classify page ────────────────────────────────────────────
        known_id = self.graph.get_page_id_by_fingerprint(fingerprint)
        depth = len(self._session_path)

        if known_id is None:
            # Brand-new page
            page_id = self._register_node(fingerprint, page_data, depth=depth)
            self._visited.add(fingerprint)

            if self._current_page_id and answers:
                self.graph.add_branch_edge(self._current_page_id, page_id, answers)
            if not self.graph.root_node_id:
                self.graph.root_node_id = page_id

            self._current_page_id = page_id
            self._session_path.append((page_id, answers))

            self._emit("shadow_new_page", {
                "page_id": page_id,
                "question_count": len(page_data.get("questions", [])),
                "msg": f"✅ Trang mới phát hiện! ({len(self._visited)} trang tổng)",
            })

        else:
            # Already-known page — check for new edge
            page_id = known_id
            if self._current_page_id and answers:
                existing_targets = [
                    v for _, v in self.graph.G.out_edges(self._current_page_id)
                ]
                if page_id not in existing_targets:
                    self.graph.add_branch_edge(self._current_page_id, page_id, answers)
                    self._emit("shadow_new_branch", {
                        "from": self._current_page_id,
                        "to": page_id,
                        "trigger_answers": answers,
                        "msg": "🔀 Nhánh mới phát hiện!",
                    })
                else:
                    self._emit("shadow_known_page", {
                        "page_id": page_id,
                        "msg": "Trang đã biết, đang đi theo path đã map",
                    })

            self._current_page_id = page_id
            self._session_path.append((page_id, answers))

        # ── Suggestions ──────────────────────────────────────────────
        suggestions = self._get_unexplored_suggestions(page_id, page_data)
        if suggestions:
            self._emit("shadow_suggestion", {
                "page_id": page_id,
                "unexplored": suggestions,
                "msg": f"💡 Trang này còn {len(suggestions)} nhánh chưa thử",
            })

        # ── Coverage update ──────────────────────────────────────────
        coverage = self._compute_coverage()
        self._emit("shadow_coverage_update", coverage)

        # ── Overlay update ───────────────────────────────────────────
        if self._overlay:
            try:
                await self._overlay.update(
                    page,
                    status=f"{'Trang mới' if known_id is None else 'Trang đã biết'}: {page_id}",
                    coverage=coverage["coverage_pct"],
                    suggestions=suggestions[:3] if suggestions else None,
                )
            except Exception:
                pass

        self._save_graph()

    # ── Helpers ────────────────────────────────────────────────────────

    async def _scan_page(self, page) -> dict:
        from app.services.mapper_service import MapperService
        from app.services.browser_service import BrowserService
        # MapperService only needs browser_service to satisfy __init__
        # We can pass None safely since scan_current_page doesn't use it
        mapper = MapperService.__new__(MapperService)
        result = await mapper.scan_current_page(page)
        if hasattr(result, "__dict__"):
            import dataclasses
            return dataclasses.asdict(result) if dataclasses.is_dataclass(result) else vars(result)
        return result if isinstance(result, dict) else {}

    async def _is_terminal(self, page) -> bool:
        from .safety_guard import SafetyGuard
        return await SafetyGuard().is_terminal_page(page)

    def _make_fingerprint(self, page_data: dict) -> str:
        questions = page_data.get("questions", [])
        parts = []
        for q in questions:
            q_id = q.get("q_id") or q.get("name") or q.get("id", "")
            n_opts = len(q.get("options", []))
            parts.append(f"{q_id}:{n_opts}")
        return hashlib.md5("|".join(parts).encode()).hexdigest()

    def _register_node(self, fingerprint: str, page_data: dict, depth: int = 0) -> str:
        existing = self.graph.get_page_id_by_fingerprint(fingerprint)
        if existing:
            self.graph.increment_visit(existing)
            return existing
        page_id = f"page_d{depth}_{fingerprint[:8]}"
        self.graph.add_page_node(
            page_id=page_id,
            fingerprint=fingerprint,
            page_data=page_data,
            depth=depth,
        )
        return page_id

    def _fp_to_id(self, fingerprint: str) -> Optional[str]:
        return self.graph.get_page_id_by_fingerprint(fingerprint)

    def _get_unexplored_suggestions(self, page_id: str, page_data: dict) -> list[dict]:
        """Return option values on this page that have no outgoing edge yet."""
        existing_edges = [
            d.get("trigger_answers", {})
            for _, _, d in self.graph.G.out_edges(page_id, data=True)
        ]
        tried: dict[str, set] = {}
        for edge_answers in existing_edges:
            for q_id, val in edge_answers.items():
                tried.setdefault(q_id, set()).add(val)

        unexplored: list[dict] = []
        for q in page_data.get("questions", []):
            q_type = (q.get("q_type") or q.get("type") or "").lower()
            if q_type not in ("radio", "select"):
                continue
            q_id = q.get("q_id") or q.get("name") or q.get("id", "")
            tried_vals = tried.get(q_id, set())
            for opt in q.get("options", []):
                val = opt.get("value") or opt.get("option_value") if isinstance(opt, dict) else opt
                text = opt.get("text") or opt.get("option_text", str(val)) if isinstance(opt, dict) else str(opt)
                if val and val not in tried_vals:
                    unexplored.append({
                        "q_id": q_id,
                        "label": (q.get("label_text") or q.get("label") or q_id)[:30],
                        "option_value": val,
                        "option_text": text,
                    })
        return unexplored

    def _compute_coverage(self) -> dict:
        total_nodes = self.graph.G.number_of_nodes()
        explored_edges = self.graph.G.number_of_edges()

        estimated = max(explored_edges, 1)
        for _, data in self.graph.G.nodes(data=True):
            for q in data.get("questions", []):
                if (q.get("q_type") or q.get("type") or "").lower() in ("radio", "select"):
                    n_opts = len(q.get("options", []))
                    if n_opts > 1:
                        estimated += n_opts - 1

        return {
            "total_pages": total_nodes,
            "explored_edges": explored_edges,
            "coverage_pct": min(100, round(explored_edges / estimated * 100)),
        }

    def _save_graph(self) -> None:
        if self._save_path:
            try:
                self.graph.save(self._save_path)
            except Exception as exc:
                logger.debug("_save_graph error: %s", exc)

    def _emit(self, event: str, data: dict) -> None:
        if self.socketio:
            try:
                self.socketio.emit(event, data)
            except Exception as exc:
                logger.debug("_emit %s: %s", event, exc)


# ══════════════════════════════════════════════════════════════════════
# ShadowMappingSession
# ══════════════════════════════════════════════════════════════════════

class ShadowMappingSession:
    """
    Open a visible Chromium window for the user, attach ShadowObserver,
    and wait until the session ends (terminal detected or stop() called).
    """

    def __init__(
        self,
        survey_graph: SurveyGraph,
        socketio=None,
        save_path=None,
        assisted: bool = False,
    ):
        self.graph = survey_graph
        self.socketio = socketio
        self._save_path = save_path
        self.assisted = assisted

        self.observer: Optional[ShadowObserver] = None
        self._done: Optional[asyncio.Event] = None
        self._browser = None     # BrowserService (headless=False)
        self._context = None
        self._page = None

    async def start(self, survey_url: str, uid: str) -> dict:
        """
        Open a visible browser, navigate to the survey, and wait.
        Returns a session summary once stop() is called or terminal detected.
        """
        from app.services.browser_service import BrowserService

        # Use a visible browser — user will interact directly
        self._browser = BrowserService(headless=False)
        self._context, self._page = await self._browser.create_context()

        url = self._inject_uid(survey_url, uid)
        await self._page.goto(url, wait_until="networkidle", timeout=30_000)

        # Create and attach observer
        self.observer = ShadowObserver(
            survey_graph=self.graph,
            socketio=self.socketio,
            save_path=self._save_path,
            assisted=self.assisted,
        )
        await self.observer.attach(self._page)

        self._emit("shadow_session_started", {
            "uid": uid,
            "url": url,
            "msg": "Browser đã mở. Hãy làm khảo sát như bình thường.",
        })

        # Patch observer to auto-stop session on terminal
        _original_process = self.observer._process_new_page

        async def _patched_process(page):
            await _original_process(page)
            if self.observer._current_page_id:
                node_data = self.graph.G.nodes.get(
                    self.observer._current_page_id, {}
                )
                if node_data.get("is_terminal") and self._done and not self._done.is_set():
                    self._done.set()

        self.observer._process_new_page = _patched_process

        self._done = asyncio.Event()
        await self._done.wait()

        pattern = self.observer.get_session_pattern()
        coverage = self.observer._compute_coverage()

        return {
            "pattern": pattern,
            "pages_found": self.graph.G.number_of_nodes(),
            "coverage": coverage,
            "session_path_length": len(self.observer._session_path),
        }

    async def stop(self) -> None:
        """End the session programmatically (from the dashboard Stop button)."""
        if self._done and not self._done.is_set():
            self._done.set()

    async def close_browser(self) -> None:
        """Close the visible browser window."""
        if self._browser:
            try:
                await self._browser.close_all()
            except Exception:
                pass

    def get_live_status(self) -> dict:
        """Return a snapshot for the /shadow/live polling endpoint."""
        if not self.observer:
            return {"pages_found": 0, "current_page": None, "coverage_pct": 0,
                    "unexplored_suggestions": [], "session_path_length": 0}
        cov = self.observer._compute_coverage()
        # Get suggestions for current page
        suggestions: list[dict] = []
        if self.observer._current_page_id:
            node_data = self.graph.G.nodes.get(self.observer._current_page_id, {})
            page_data = {"questions": node_data.get("questions", [])}
            suggestions = self.observer._get_unexplored_suggestions(
                self.observer._current_page_id, page_data
            )
        return {
            "pages_found": cov["total_pages"],
            "current_page": self.observer._current_page_id,
            "coverage_pct": cov["coverage_pct"],
            "unexplored_suggestions": suggestions,
            "session_path_length": len(self.observer._session_path),
        }

    @staticmethod
    def _inject_uid(url: str, uid: str) -> str:
        p = urlparse(url)
        params = parse_qs(p.query)
        params["uid"] = [uid]
        return urlunparse(p._replace(query=urlencode({k: v[0] for k, v in params.items()})))

    def _emit(self, event: str, data: dict) -> None:
        if self.socketio:
            try:
                self.socketio.emit(event, data)
            except Exception as exc:
                logger.debug("_emit %s: %s", event, exc)


# ══════════════════════════════════════════════════════════════════════
# AssistantOverlay
# ══════════════════════════════════════════════════════════════════════

class AssistantOverlay:
    """
    Inject a translucent hint-panel into the survey page via page.evaluate().
    The overlay floats at top-right, shows coverage and unexplored options.
    """

    OVERLAY_JS = r"""
    (() => {
        if (document.getElementById('__dsaf_overlay__')) return;
        const overlay = document.createElement('div');
        overlay.id = '__dsaf_overlay__';
        overlay.style.cssText = [
            'position:fixed', 'top:12px', 'right:12px', 'width:260px',
            'background:rgba(20,20,30,0.92)', 'color:#e8e8e8',
            'border:1px solid #534AB7', 'border-radius:10px',
            'padding:12px 14px', 'font-family:monospace', 'font-size:12px',
            'z-index:999999', 'box-shadow:0 4px 20px rgba(0,0,0,0.5)',
            'transition:all 0.3s'
        ].join(';');
        overlay.innerHTML =
            '<div style="font-weight:bold;color:#A79EF5;margin-bottom:8px;">🔍 DSAF Observer</div>' +
            '<div id="__dsaf_status__">Đang quan sát...</div>' +
            '<div id="__dsaf_coverage__" style="margin-top:6px;color:#7EC8A4"></div>' +
            '<div id="__dsaf_suggestions__" style="margin-top:6px;color:#F5C56A"></div>' +
            '<div id="__dsaf_warning__" style="margin-top:6px;color:#F28B82;font-weight:bold"></div>';
        document.body.appendChild(overlay);
        window.__dsaf_update__ = (data) => {
            document.getElementById('__dsaf_status__').innerHTML = data.status || '';
            document.getElementById('__dsaf_coverage__').innerHTML =
                data.coverage ? '📊 Coverage: ' + data.coverage + '%' : '';
            document.getElementById('__dsaf_suggestions__').innerHTML =
                data.suggestions && data.suggestions.length
                    ? '💡 Chưa thử: ' + data.suggestions.join(', ')
                    : '';
            document.getElementById('__dsaf_warning__').innerHTML = data.warning || '';
        };
    })();
    """

    async def inject(self, page) -> None:
        """Inject the overlay into the current page."""
        try:
            await page.evaluate(self.OVERLAY_JS)
        except Exception as exc:
            logger.debug("overlay inject: %s", exc)

    async def update(
        self,
        page,
        status: str = "",
        coverage: int = 0,
        suggestions: Optional[list] = None,
        warning: str = "",
    ) -> None:
        """Push new data to the overlay (safe no-op if overlay not present)."""
        payload = json.dumps({
            "status": status,
            "coverage": coverage,
            "suggestions": [s["option_text"] for s in (suggestions or [])][:3],
            "warning": warning,
        })
        try:
            await page.evaluate(f"""
                if (window.__dsaf_update__) {{
                    window.__dsaf_update__({payload});
                }}
            """)
        except Exception as exc:
            logger.debug("overlay update: %s", exc)

    async def show_terminal_warning(self, page) -> None:
        """Flash a red warning bar in the overlay about the Submit page."""
        await self.update(page, warning="⚠ SẮP SUBMIT! UID sẽ hết sau bước này.")
