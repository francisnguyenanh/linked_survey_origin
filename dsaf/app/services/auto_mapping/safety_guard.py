"""
SafetyGuard — Prevents real data submission and detects terminal pages.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

logger = logging.getLogger(__name__)

# Text patterns that appear on survey completion / thank-you pages
_TERMINAL_URL_SIGNALS = [
    "thanks", "complete", "finish", "end", "done", "kekka",
    "thankyou", "thank_you", "kanryo", "gochisei",
]

_TERMINAL_TEXT_SIGNALS = [
    "ありがとう", "ありがとうございました",
    "回答が完了", "ご協力ありがとう",
    "完了しました", "回答完了", "アンケートが終了",
    "アンケートは終了", "調査は終了",
    "survey complete", "thank you for", "survey is now closed",
]

# Button / form text that represents the *final* submission (not "Next page")
_FINAL_SUBMIT_SIGNALS = [
    "送信する", "回答を送信", "アンケートを送信", "最終送信",
    "確認して送信", "この内容で送信",
    "submit", "finish survey", "complete survey",
]

# Standard "next page" navigation texts — these are NOT final submissions
_NEXT_PAGE_SIGNALS = [
    "次へ", "次のページ", "進む", "続ける",
    "next", "continue", "proceed",
]


class SafetyGuard:
    """
    Two-layer protection for auto-mapping:
    1. Detect terminal/completion pages and refuse to click Submit.
    2. Optionally intercept all POST requests on the terminal page to prevent
       form data from being sent to the survey server.
    """

    def __init__(self, safe_uid_pool: list[str] | None = None) -> None:
        self.safe_uid_pool: list[str] = safe_uid_pool or []

    # ------------------------------------------------------------------
    # Terminal page detection
    # ------------------------------------------------------------------

    async def is_terminal_page(self, page: "Page") -> bool:
        """
        Return True if the current page is a survey completion/thank-you page.

        Checks (any one hit → True):
        1. URL contains a terminal signal keyword
        2. Page <title> contains a terminal signal
        3. Visible body text contains a terminal signal phrase
        4. There is a *final* submit button (and no "next page" button)
        """
        try:
            url = page.url.lower()
            for sig in _TERMINAL_URL_SIGNALS:
                if sig in url:
                    logger.debug("Terminal: URL contains '%s'", sig)
                    return True

            # Check title and body text via JS evaluation
            result: dict = await page.evaluate(
                """() => {
                    const title = (document.title || '').toLowerCase();
                    const body = (document.body ? document.body.innerText : '').toLowerCase();
                    const buttons = Array.from(document.querySelectorAll(
                        'button, input[type=submit], input[type=button], a.btn'
                    )).map(el => (el.innerText || el.value || '').toLowerCase().trim());
                    return { title, body_snippet: body.slice(0, 2000), buttons };
                }"""
            )

            title: str = result.get("title", "")
            body: str = result.get("body_snippet", "")
            buttons: list[str] = result.get("buttons", [])

            combined = title + " " + body
            for sig in _TERMINAL_TEXT_SIGNALS:
                if sig.lower() in combined:
                    logger.debug("Terminal: body/title contains '%s'", sig)
                    return True

            # Check if any button looks like a final submission
            has_final_submit = any(
                any(fs.lower() in btn for fs in _FINAL_SUBMIT_SIGNALS)
                for btn in buttons
            )
            has_next_page = any(
                any(ns.lower() in btn for ns in _NEXT_PAGE_SIGNALS)
                for btn in buttons
            )

            if has_final_submit and not has_next_page:
                logger.debug("Terminal: final-submit button found without next-page button")
                return True

        except Exception as exc:
            logger.warning("is_terminal_page evaluation error: %s", exc)

        return False

    # ------------------------------------------------------------------
    # POST interception
    # ------------------------------------------------------------------

    async def intercept_final_submit(self, page: "Page") -> None:
        """
        Install a route handler that blocks all POST requests on the current page.
        Call this just before reaching a terminal page in DFS so even accidental
        clicks cannot send real data to the survey server.
        """
        async def _block_post(route):
            if route.request.method.upper() == "POST":
                logger.warning(
                    "SafetyGuard: blocked POST to %s", route.request.url
                )
                await route.abort()
            else:
                await route.continue_()

        try:
            await page.route("**/*", _block_post)
            logger.info("SafetyGuard: POST interception active on %s", page.url)
        except Exception as exc:
            logger.warning("intercept_final_submit failed: %s", exc)

    # ------------------------------------------------------------------
    # UID validation
    # ------------------------------------------------------------------

    def is_safe_uid(self, uid: str) -> bool:
        """Return True if this UID is in the designated safe (mapping) pool."""
        return uid in self.safe_uid_pool

    def validate_safe_uid(self, uid: str) -> None:
        """Raise ValueError if the UID is not in the safe pool."""
        if self.safe_uid_pool and not self.is_safe_uid(uid):
            raise ValueError(
                f"UID '{uid}' is not in the safe_uid_pool. "
                "Auto-mapping must use dedicated mapping UIDs to avoid "
                "polluting real survey data."
            )
