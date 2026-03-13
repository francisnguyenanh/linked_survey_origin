"""
MapperService — Page scanner and fingerprinter for survey mapping.
BranchingMapperService — Extends MapperService with incremental branch discovery.

Works in INTERACTIVE mode: the user navigates the survey in a visible browser while
the service records each page's structure and tracks branch divergence.
"""

import asyncio
import hashlib
import itertools
import json
import logging
import re
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from playwright.async_api import Page

from app.services.browser_service import BrowserService

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Japanese UI constants
# ---------------------------------------------------------------------------
NEXT_BUTTON_TEXTS_JA: list[str] = [
    "次へ", "次に進む", "進む", "回答する", "送信", "確認",
    "回答を送信", "次のページ", "OK",
]

COMPLETE_PAGE_SIGNALS: list[str] = [
    "ありがとう", "完了", "終了", "アンケートへのご協力",
    "回答が完了", "thanks", "complete", "finish",
]


# ---------------------------------------------------------------------------
# rsch.jp-specific helpers
# ---------------------------------------------------------------------------

async def handle_rsch_login(page: Page, uid: str, cmpid: str):
    """
    Handle the rsch.jp login page.

    rsch.jp login pages typically pre-fill the UID via URL params with no password.
    If a visible UID text field exists, this function will fill it.

    Args:
        page: Active Playwright page already loaded at the login URL.
        uid: Respondent UID string.
        cmpid: Campaign ID string.
    """
    # Check for a visible UID input and fill if present
    uid_input = await page.query_selector("input[name='uid']:not([type='hidden'])")
    if uid_input:
        await uid_input.fill(uid)
        await asyncio.sleep(0.3)

    # Find and click proceed button
    for text in NEXT_BUTTON_TEXTS_JA:
        btn = await page.query_selector(f"text={text}")
        if btn:
            await btn.click()
            await page.wait_for_load_state("domcontentloaded")
            return

    # Fallback: any submit button
    submit = await page.query_selector("input[type='submit'], button[type='submit']")
    if submit:
        await submit.click()
        await page.wait_for_load_state("domcontentloaded")


async def preserve_hidden_fields(page: Page) -> list[dict]:
    """
    Extract all hidden input fields for debugging purposes.

    CRITICAL: rsch.jp uses hidden fields for session tracking.
    These must never be modified. Playwright's click()-based submission
    handles them automatically.

    Args:
        page: Active Playwright page.

    Returns:
        List of dicts with 'name' and 'value' keys for each hidden field.
    """
    return await page.evaluate("""
        () => {
            return Array.from(document.querySelectorAll("input[type='hidden']"))
                .map(el => ({ name: el.name, value: el.value }));
        }
    """)


# ---------------------------------------------------------------------------
# Answer auto-detection via POST interception
# ---------------------------------------------------------------------------

async def auto_detect_answers_from_dom(page: Page, page_data: dict) -> dict:
    """
    Intercept a form POST submission to capture the answers selected by the user.

    Sets up a Playwright request interceptor that captures the POST body
    when the form is submitted. Matches form field names to q_ids from page_data.

    Args:
        page: Active Playwright page (before user clicks submit).
        page_data: The current page's scan result containing question definitions.

    Returns:
        Dict mapping q_id -> selected option value, or empty dict if detection failed.
    """
    captured: dict = {}
    post_data_holder: list[str] = []

    def on_request(request):
        if request.method == "POST":
            post_body = request.post_data
            if post_body:
                post_data_holder.append(post_body)

    page.on("request", on_request)

    # Give the caller time to let the user click submit
    await asyncio.sleep(0.1)

    # Parse captured POST body
    if post_data_holder:
        from urllib.parse import parse_qs
        raw = parse_qs(post_data_holder[-1])
        for question in page_data.get("questions", []):
            q_id = question.get("q_id")
            # Try matching by field name stored in fallback_selector heuristic
            for field_name, values in raw.items():
                if values:
                    captured[q_id] = values[0]
                    break

    page.remove_listener("request", on_request)
    return captured


# ---------------------------------------------------------------------------
# MapperService
# ---------------------------------------------------------------------------

class MapperService:
    """
    Scans survey pages and builds a survey map JSON incrementally.

    Works in interactive mode: the user navigates the survey manually while
    this service captures the structure of each page.
    """

    def __init__(self, browser_service: BrowserService):
        """
        Initialise MapperService.

        Args:
            browser_service: Configured BrowserService instance.
        """
        self.browser_service = browser_service

    async def scan_current_page(self, page: Page) -> dict:
        """
        Extract all form elements from the currently loaded page.

        Algorithm:
        1. Find all <label> elements and extract their text.
        2. For each label, find its associated input via:
           a. label[for] → getElementById
           b. label wrapping an input
           c. Proximity: nearest input after label in DOM order
        3. Detect input type: radio | checkbox | select | textarea | text | hidden
        4. For radio/checkbox: collect all options sharing the same name attribute.
        5. For select: collect all <option> elements.
        6. Detect honeypot fields (hidden, display:none, off-viewport).

        Args:
            page: Active Playwright page.

        Returns:
            Dict matching the page schema from Section 2.1.
        """
        questions_raw = await page.evaluate("""
            () => {
                function isHoneypot(el) {
                    if (el.type === 'hidden') return true;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return true;
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) return true;
                    if (rect.top < -100 || rect.left < -100) return true;
                    return false;
                }

                function inputsForLabel(label) {
                    const forAttr = label.getAttribute('for');
                    if (forAttr) {
                        const el = document.getElementById(forAttr);
                        if (el) return [el];
                    }
                    const inner = label.querySelectorAll('input, select, textarea');
                    if (inner.length) return Array.from(inner);
                    // Proximity: next sibling inputs
                    const siblings = [];
                    let node = label.nextElementSibling;
                    while (node && siblings.length < 3) {
                        if (['INPUT','SELECT','TEXTAREA'].includes(node.tagName)) {
                            siblings.push(node);
                        }
                        node = node.nextElementSibling;
                    }
                    return siblings;
                }

                const labels = Array.from(document.querySelectorAll('label'));
                const processed = new Set();
                const questions = [];
                let qIdx = 0;

                labels.forEach(label => {
                    const labelText = (label.textContent || '').trim();
                    if (!labelText) return;
                    const inputs = inputsForLabel(label);
                    if (!inputs.length) return;

                    const primary = inputs[0];
                    const inputId = primary.name || primary.id || `input_${qIdx}`;
                    if (processed.has(inputId) && primary.type !== 'radio' && primary.type !== 'checkbox') return;
                    processed.add(inputId);

                    let qType = primary.tagName.toLowerCase();
                    if (primary.tagName === 'INPUT') qType = primary.type || 'text';
                    if (primary.tagName === 'TEXTAREA') qType = 'textarea';

                    let options = [];
                    if (qType === 'radio' || qType === 'checkbox') {
                        const name = primary.name;
                        const allInputs = name
                            ? Array.from(document.querySelectorAll(`input[name="${name}"]`))
                            : inputs;
                        allInputs.forEach((inp, i) => {
                            const lbl = inp.labels && inp.labels[0]
                                ? (inp.labels[0].textContent || '').trim()
                                : inp.value;
                            options.push({ option_index: i, option_text: lbl, option_value: inp.value });
                        });
                    } else if (primary.tagName === 'SELECT') {
                        Array.from(primary.options).forEach((opt, i) => {
                            options.push({
                                option_index: i,
                                option_text: (opt.text || '').trim(),
                                option_value: opt.value
                            });
                        });
                    }

                    const honey = isHoneypot(primary);
                    const required = primary.hasAttribute('required') ||
                                     primary.getAttribute('aria-required') === 'true';

                    questions.push({
                        q_index: qIdx++,
                        label_text: labelText,
                        q_type: qType,
                        options: options,
                        is_required: required,
                        honeypot: honey,
                        input_name: primary.name || '',
                        input_id: primary.id || ''
                    });
                });

                return questions;
            }
        """)

        page_url = page.url
        questions = []
        for raw_q in questions_raw:
            q_idx = raw_q["q_index"]
            q_id = f"q_{q_idx + 1:03d}"
            label = raw_q["label_text"]
            normalized = self._normalize_text(label)
            options = [
                {
                    "option_index": o["option_index"],
                    "option_text": o["option_text"],
                    "option_value": o["option_value"],
                }
                for o in raw_q.get("options", [])
            ]
            selector = (
                f"input[name='{raw_q['input_name']}']"
                if raw_q.get("input_name")
                else f"#{raw_q['input_id']}"
                if raw_q.get("input_id")
                else f"input:nth-of-type({q_idx + 1})"
            )
            questions.append({
                "q_id": q_id,
                "q_index": q_idx,
                "label_text": label,
                "label_normalized": normalized,
                "q_type": raw_q["q_type"],
                "options": options,
                "is_required": raw_q["is_required"],
                "selector_strategy": "label_text",
                "fallback_selector": selector,
                "honeypot": raw_q["honeypot"],
            })

        fingerprint = self.compute_fingerprint(questions)
        navigation = await self._detect_navigation_element(page)

        return {
            "url": page_url,
            "page_fingerprint": fingerprint,
            "questions": questions,
            "navigation": navigation,
        }

    def compute_fingerprint(self, questions: list) -> str:
        """
        Produce a stable SHA-256 fingerprint for a page based on its question labels.

        Algorithm: SHA-256 of sorted, NFKC-normalized, lowercased question label texts.

        Args:
            questions: List of question dicts containing 'label_normalized'.

        Returns:
            Hex-encoded SHA-256 digest string.
        """
        labels = sorted([q.get("label_normalized", "") for q in questions])
        combined = "|".join(labels)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:32]

    async def _detect_navigation_element(self, page: Page) -> dict:
        """
        Find the primary 'Next / Submit' navigation element.

        Search priority:
        1. input[type='submit']
        2. button[type='submit']
        3. button or <a> containing Japanese next-page texts

        Args:
            page: Active Playwright page.

        Returns:
            Dict with 'submit_selector', 'submit_button_text', and 'method' keys.
        """
        # Priority 1 & 2: explicit submit elements
        for selector in ("input[type='submit']", "button[type='submit']"):
            el = await page.query_selector(selector)
            if el:
                text = await el.get_attribute("value") or await el.inner_text() or "次へ"
                return {
                    "submit_button_text": text.strip(),
                    "submit_selector": selector,
                    "method": "POST",
                }

        # Priority 3: Japanese text buttons
        for btn_text in NEXT_BUTTON_TEXTS_JA:
            el = await page.query_selector(f"button:has-text('{btn_text}')")
            if el:
                return {
                    "submit_button_text": btn_text,
                    "submit_selector": f"button:has-text('{btn_text}')",
                    "method": "POST",
                }
            el = await page.query_selector(f"a:has-text('{btn_text}')")
            if el:
                return {
                    "submit_button_text": btn_text,
                    "submit_selector": f"a:has-text('{btn_text}')",
                    "method": "GET",
                }

        return {
            "submit_button_text": "次へ",
            "submit_selector": "input[type='submit'], button[type='submit']",
            "method": "POST",
        }

    @staticmethod
    def _normalize_text(text: str) -> str:
        """
        Normalize a string for fingerprint-stable comparison.

        Steps: NFKC normalization (full-width→half-width), lowercase,
        remove punctuation, collapse whitespace.

        Args:
            text: Raw label text.

        Returns:
            Normalized string.
        """
        normalized = unicodedata.normalize("NFKC", text)
        normalized = normalized.lower()
        normalized = re.sub(r"[^\w\s]", "", normalized)
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return normalized

    async def save_map(self, survey_map: dict, filepath: str):
        """
        Serialize and save a survey map dict as pretty-printed JSON.

        Args:
            survey_map: Complete survey map dict.
            filepath: Absolute or relative path to write the JSON file.
        """
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(survey_map, fh, ensure_ascii=False, indent=2)
        logger.info(f"Survey map saved: {filepath}")


# ---------------------------------------------------------------------------
# BranchingMapperService
# ---------------------------------------------------------------------------

class BranchingMapperService(MapperService):
    """
    Extends MapperService with incremental branch discovery.

    The same Section (same page fingerprint) can be visited multiple times
    with different answers to reveal new downstream branches.
    This service tracks ALL visits and merges new discoveries into the map JSON.
    """

    def __init__(self, browser_service: BrowserService, survey_map: dict):
        """
        Initialise BranchingMapperService.

        Args:
            browser_service: Configured BrowserService instance.
            survey_map: Existing (possibly empty) survey map dict to extend.
        """
        super().__init__(browser_service)
        self.survey_map = survey_map
        self.current_session_id: Optional[str] = None
        self.current_session_path: list[str] = []
        self.current_session_answers: dict = {}

        # Ensure branch_tree structure exists
        if "branch_tree" not in self.survey_map:
            self.survey_map["branch_tree"] = {"root_page_id": None, "nodes": {}}
        if "discovery_sessions" not in self.survey_map:
            self.survey_map["discovery_sessions"] = []
        if "coverage_stats" not in self.survey_map:
            self.survey_map["coverage_stats"] = {}

    # ── SESSION LIFECYCLE ────────────────────────────────────────────────────

    def start_discovery_session(self) -> str:
        """
        Start a new discovery session (one full run from login to end/abort).

        Returns:
            Unique session_id string.
        """
        survey_id = self.survey_map.get("survey_id", "unknown")
        ts = int(datetime.now(timezone.utc).timestamp())
        self.current_session_id = f"sess_{survey_id}_{ts}"
        self.current_session_path = []
        self.current_session_answers = {}
        logger.info(f"Discovery session started: {self.current_session_id}")
        return self.current_session_id

    def end_discovery_session(self, result: str):
        """
        Finalise the current discovery session and persist the record.

        Args:
            result: One of "new_branch_discovered" | "existing_branch_confirmed" | "aborted".
        """
        if not self.current_session_id:
            return

        new_page_ids = [
            p for p in self.current_session_path
            if p not in [
                s["pages_visited"][0]
                for s in self.survey_map["discovery_sessions"]
                if s["pages_visited"]
            ]
        ]

        session_record = {
            "session_id": self.current_session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "pages_visited": list(self.current_session_path),
            "answers_given": dict(self.current_session_answers),
            "result": result,
            "new_page_ids": new_page_ids,
        }
        self.survey_map["discovery_sessions"].append(session_record)
        self.survey_map["coverage_stats"] = self.compute_coverage_stats()
        logger.info(
            f"Session {self.current_session_id} ended: {result}, "
            f"new pages: {len(new_page_ids)}"
        )
        self.current_session_id = None

    # ── PAGE RECORDING WITH BRANCH AWARENESS ────────────────────────────────

    async def record_page_with_branch_check(
        self,
        page: Page,
        answers_given_on_previous_page: Optional[dict] = None,
    ) -> dict:
        """
        Scan current page and check whether it's new, known, or in conflict.

        Cases:
        - CASE A (new page): Add node to branch_tree, create branch from parent.
        - CASE B (known page): Increment visit count; add branch if new trigger answers.
        - CASE C (conflict): Labels changed on a known fingerprint — prompts user action.

        Args:
            page: Active Playwright page.
            answers_given_on_previous_page: Answers recorded on the previous page before
                                             clicking Next. Used as branch trigger_answers.

        Returns:
            Dict with keys: status ("new"|"known"|"conflict"), page_id, page_data,
            is_new_branch, unexplored_options, suggested_next_attempts.
        """
        page_data = await self.scan_current_page(page)
        fingerprint = page_data["page_fingerprint"]
        nodes = self.survey_map["branch_tree"].get("nodes", {})

        # Look up this fingerprint in existing nodes
        existing_node = None
        for node in nodes.values():
            if node.get("fingerprint") == fingerprint:
                existing_node = node
                break

        parent_page_id = self.current_session_path[-1] if self.current_session_path else None

        if existing_node is None:
            # CASE A: First time seeing this page
            node_count = len(nodes)
            page_id = f"page_{node_count + 1:03d}"
            new_node = {
                "page_id": page_id,
                "fingerprint": fingerprint,
                "discovered_count": 1,
                "parent_branch_ids": [],
                "outgoing_branches": [],
                "page_data": page_data,
            }
            nodes[page_id] = new_node
            self.survey_map["branch_tree"]["nodes"] = nodes

            if not self.survey_map["branch_tree"].get("root_page_id"):
                self.survey_map["branch_tree"]["root_page_id"] = page_id

            is_new_branch = False
            if parent_page_id and answers_given_on_previous_page:
                branch_id = self.merge_new_branch(
                    parent_page_id, answers_given_on_previous_page, page_data
                )
                new_node["parent_branch_ids"].append(branch_id)
                is_new_branch = True

            self.current_session_path.append(page_id)
            unexplored = self.get_unexplored_options(page_id)

            logger.info(f"New page discovered: {page_id} (fingerprint: {fingerprint[:8]}…)")
            return {
                "status": "new",
                "page_id": page_id,
                "page_data": page_data,
                "is_new_branch": is_new_branch,
                "unexplored_options": unexplored,
                "suggested_next_attempts": unexplored[:3],
            }

        else:
            # CASE B or C: Page seen before
            existing_questions = existing_node.get("page_data", {}).get("questions", [])
            new_questions = page_data.get("questions", [])
            existing_labels = sorted([q.get("label_normalized", "") for q in existing_questions])
            new_labels = sorted([q.get("label_normalized", "") for q in new_questions])

            page_id = existing_node["page_id"]
            self.current_session_path.append(page_id)

            if existing_labels != new_labels and existing_labels and new_labels:
                # CASE C: Conflict
                logger.warning(
                    f"Fingerprint conflict on page {page_id}: labels changed. "
                    f"Old count={len(existing_labels)}, new count={len(new_labels)}"
                )
                return {
                    "status": "conflict",
                    "page_id": page_id,
                    "existing": existing_node.get("page_data", {}),
                    "new": page_data,
                    "is_new_branch": False,
                    "unexplored_options": [],
                    "suggested_next_attempts": [],
                }

            # CASE B: Known page — update count and check for new branch
            existing_node["discovered_count"] = existing_node.get("discovered_count", 0) + 1
            is_new_branch = False

            if parent_page_id and answers_given_on_previous_page:
                known_triggers = [
                    b.get("trigger_answers")
                    for b in existing_node.get("outgoing_branches", [])
                ]
                if answers_given_on_previous_page not in known_triggers:
                    branch_id = self.merge_new_branch(
                        parent_page_id, answers_given_on_previous_page, page_data
                    )
                    existing_node.setdefault("parent_branch_ids", []).append(branch_id)
                    is_new_branch = True

            unexplored = self.get_unexplored_options(page_id)
            return {
                "status": "known",
                "page_id": page_id,
                "page_data": existing_node.get("page_data", page_data),
                "is_new_branch": is_new_branch,
                "unexplored_options": unexplored,
                "suggested_next_attempts": unexplored[:3],
            }

    def record_answers_for_current_page(self, page_id: str, answers: dict):
        """
        Record the answers a user selected on a given page before clicking Next.

        These answers become the trigger_answers for any branch discovered on the
        following page.

        Args:
            page_id: ID of the page where the answers were given.
            answers: Dict mapping q_id -> selected option value.
        """
        self.current_session_answers[page_id] = answers
        logger.debug(f"Recorded answers for {page_id}: {answers}")

    # ── BRANCH MANAGEMENT ────────────────────────────────────────────────────

    def merge_new_branch(
        self,
        parent_page_id: str,
        trigger_answers: dict,
        child_page_data: dict,
        user_label: str = "",
    ) -> str:
        """
        Add a newly discovered branch to the branch_tree.

        Args:
            parent_page_id: Page ID of the parent node.
            trigger_answers: Answer dict that caused the branch.
            child_page_data: Scanned page data for the child page.
            user_label: Optional human-readable label for this branch.

        Returns:
            The new branch_id string.
        """
        nodes = self.survey_map["branch_tree"]["nodes"]
        parent_node = nodes.get(parent_page_id)
        if not parent_node:
            logger.warning(f"Parent node {parent_page_id} not found in branch_tree")
            return ""

        branch_n = len(parent_node.get("outgoing_branches", [])) + 1
        branch_id = f"branch_{parent_page_id}_{branch_n}"

        # Find or create child page_id
        child_fingerprint = child_page_data.get("page_fingerprint", "")
        child_page_id = None
        for node_id, node in nodes.items():
            if node.get("fingerprint") == child_fingerprint:
                child_page_id = node_id
                break
        if not child_page_id:
            child_page_id = f"page_{len(nodes) + 1:03d}"

        branch_record = {
            "branch_id": branch_id,
            "trigger_answers": trigger_answers,
            "leads_to_page_fingerprint": child_fingerprint,
            "leads_to_page_id": child_page_id,
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "pattern_hint": user_label,
        }

        parent_node.setdefault("outgoing_branches", []).append(branch_record)
        self.survey_map["coverage_stats"] = self.compute_coverage_stats()
        logger.info(f"New branch merged: {branch_id} ({parent_page_id} → {child_page_id})")
        return branch_id

    def update_existing_page(
        self,
        page_id: str,
        new_page_data: dict,
        merge_strategy: str = "merge_questions",
    ):
        """
        Update an existing page node when revisited.

        Args:
            page_id: Target page ID to update.
            new_page_data: Freshly scanned page data.
            merge_strategy: One of:
                "replace_questions" — overwrite questions entirely.
                "merge_questions"   — add only new questions (safe default).
                "keep_existing"     — don't touch questions, update metadata only.
        """
        node = self.survey_map["branch_tree"]["nodes"].get(page_id)
        if not node:
            logger.warning(f"update_existing_page: page_id {page_id} not found")
            return

        node["discovered_count"] = node.get("discovered_count", 0) + 1

        if merge_strategy == "replace_questions":
            node["page_data"] = new_page_data
        elif merge_strategy == "merge_questions":
            existing_labels = {
                q["label_normalized"]
                for q in node.get("page_data", {}).get("questions", [])
            }
            new_qs = [
                q for q in new_page_data.get("questions", [])
                if q.get("label_normalized") not in existing_labels
            ]
            node.setdefault("page_data", {}).setdefault("questions", []).extend(new_qs)
        # "keep_existing" — nothing to do beyond incrementing count

    def promote_branch_to_pattern(
        self,
        branch_path: list[str],
        pattern_name: str,
        auto_fill_answers: bool = True,
    ) -> dict:
        """
        Convert a discovered branch path into a Pattern JSON dict.

        Args:
            branch_path: Ordered list of page_ids constituting the branch.
            pattern_name: Human-readable name for the new pattern.
            auto_fill_answers: If True, pre-fill answers from recorded trigger_answers.

        Returns:
            Pattern dict ready to save via PatternService.
        """
        from slugify import slugify

        pattern_id = f"pattern_{slugify(pattern_name, separator='_')}"
        answers: dict = {}
        branch_ids_used: list[str] = []
        nodes = self.survey_map["branch_tree"]["nodes"]

        for page_id in branch_path:
            node = nodes.get(page_id, {})
            page_answers: dict = {}

            if auto_fill_answers:
                recorded = self.current_session_answers.get(page_id, {})
                for q_id, value in recorded.items():
                    page_answers[q_id] = {"strategy": "fixed", "value": value}

            answers[page_id] = page_answers

            for branch in node.get("outgoing_branches", []):
                if branch.get("leads_to_page_id") in branch_path:
                    branch_ids_used.append(branch["branch_id"])

        return {
            "schema_version": "1.1",
            "pattern_id": pattern_id,
            "pattern_name": pattern_name,
            "description": f"Auto-generated from branch path: {' → '.join(branch_path)}",
            "linked_survey_id": self.survey_map.get("survey_id", ""),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "uid_pool": [],
            "uid_strategy": "sequential",
            "answers": answers,
            "timing": {
                "min_total_seconds": 90,
                "max_total_seconds": 240,
                "page_delay_min": 3.0,
                "page_delay_max": 8.0,
                "typing_delay_per_char_ms": [50, 150],
            },
            "branch_path": branch_path,
            "branch_ids_used": branch_ids_used,
            "auto_generated_from_mapping": True,
            "requires_branch_match": True,
        }

    # ── ANALYSIS HELPERS ─────────────────────────────────────────────────────

    def get_unexplored_options(self, page_id: str) -> list[dict]:
        """
        Return answer combinations for a page that have not yet been explored.

        Only considers questions with 2–5 options (likely branch-triggering).

        Args:
            page_id: Target page ID.

        Returns:
            List of unexplored answer-combination dicts, e.g. [{"q_001": "2", ...}].
        """
        node = self.survey_map["branch_tree"]["nodes"].get(page_id)
        if not node:
            return []

        questions = node.get("page_data", {}).get("questions", [])
        branching_questions = [
            q for q in questions
            if 2 <= len(q.get("options", [])) <= 5 and not q.get("honeypot")
        ]
        if not branching_questions:
            return []

        option_sets = [
            [(q["q_id"], opt["option_value"]) for opt in q["options"]]
            for q in branching_questions
        ]
        all_combos = [dict(combo) for combo in itertools.product(*option_sets)]

        known_triggers = [
            b.get("trigger_answers", {})
            for b in node.get("outgoing_branches", [])
        ]

        def combo_matches_known(combo: dict) -> bool:
            for known in known_triggers:
                if all(combo.get(k) == v for k, v in known.items()):
                    return True
            return False

        return [c for c in all_combos if not combo_matches_known(c)]

    def compute_coverage_stats(self) -> dict:
        """
        Compute overall branch discovery coverage statistics.

        Returns:
            Dict with total_pages_discovered, total_branches_discovered,
            pages_with_unexplored_options, estimated_coverage_pct,
            branch_tree_depth, deepest_path.
        """
        nodes = self.survey_map["branch_tree"].get("nodes", {})
        total_pages = len(nodes)
        total_branches = sum(
            len(n.get("outgoing_branches", [])) for n in nodes.values()
        )
        unexplored_pages = [
            pid for pid in nodes
            if self.get_unexplored_options(pid)
        ]

        # BFS to find deepest path
        root_id = self.survey_map["branch_tree"].get("root_page_id")
        deepest_path: list[str] = []
        max_depth = 0
        if root_id and root_id in nodes:
            queue: list[tuple[str, list[str]]] = [(root_id, [root_id])]
            visited_bfs: set[str] = set()
            while queue:
                current, path = queue.pop(0)
                if current in visited_bfs:
                    continue
                visited_bfs.add(current)
                if len(path) > max_depth:
                    max_depth = len(path)
                    deepest_path = list(path)
                for branch in nodes[current].get("outgoing_branches", []):
                    child = branch.get("leads_to_page_id")
                    if child and child in nodes and child not in visited_bfs:
                        queue.append((child, path + [child]))

        # Estimate coverage: explored / total possible branches
        total_options = sum(
            len(self.get_unexplored_options(pid)) + len(nodes[pid].get("outgoing_branches", []))
            for pid in nodes
        )
        explored_options = sum(
            len(nodes[pid].get("outgoing_branches", [])) for pid in nodes
        )
        coverage_pct = (
            round((explored_options / total_options) * 100, 1)
            if total_options > 0
            else 0.0
        )

        return {
            "total_pages_discovered": total_pages,
            "total_branches_discovered": total_branches,
            "pages_with_unexplored_options": unexplored_pages,
            "estimated_coverage_pct": coverage_pct,
            "branch_tree_depth": max_depth,
            "deepest_path": deepest_path,
        }

    def export_branch_tree_summary(self) -> str:
        """
        Generate a human-readable tree summary of all discovered branches.

        Returns:
            Multi-line string suitable for display in UI or logs.
        """
        nodes = self.survey_map["branch_tree"].get("nodes", {})
        root_id = self.survey_map["branch_tree"].get("root_page_id")
        survey_id = self.survey_map.get("survey_id", "unknown")
        lines = [f"Survey Map: {survey_id}"]

        def render_node(page_id: str, prefix: str, visited: set):
            if page_id in visited or page_id not in nodes:
                return
            visited.add(page_id)
            node = nodes[page_id]
            count = node.get("discovered_count", 0)
            unexplored = len(self.get_unexplored_options(page_id))
            flag = " [UNEXPLORED]" if unexplored > 0 else ""
            lines.append(f"{prefix}{page_id} [{count} visit{'s' if count != 1 else ''}]{flag}")
            branches = node.get("outgoing_branches", [])
            for i, branch in enumerate(branches):
                is_last = i == len(branches) - 1
                connector = "└──" if is_last else "├──"
                child_prefix = "    " if is_last else "│   "
                trigger_str = ", ".join(f"{k}={v}" for k, v in branch.get("trigger_answers", {}).items())
                child_id = branch.get("leads_to_page_id", "?")
                lines.append(f"{prefix}{connector}[{trigger_str}]──> {child_id}")
                render_node(child_id, prefix + child_prefix, visited)

        if root_id:
            render_node(root_id, "├── ", set())

        stats = self.survey_map.get("coverage_stats", {})
        pct = stats.get("estimated_coverage_pct", 0)
        total_b = stats.get("total_branches_discovered", 0)
        lines.append(f"Coverage: {pct}% ({total_b} branches explored)")
        return "\n".join(lines)
