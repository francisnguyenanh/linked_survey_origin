"""
RateLimitManager — Controls inter-branch delays and proxy rotation
during auto-mapping to avoid triggering rate limits or IP blocks.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.services.proxy_service import ProxyService

logger = logging.getLogger(__name__)


class RateLimitManager:
    """
    Manages pacing between DFS branches.

    Delay strategy:
    - Base:         5 – 15 s between branches
    - Every 3rd:  + 30 – 60 s (simulate a natural "reading break")
    - Every 10th: rotate proxy (if available)
    - Night hours (22:00–08:00 JST): halve delays (fewer concurrent users)
    """

    # JST offset in hours (UTC+9)
    _JST_OFFSET = 9

    def __init__(
        self,
        proxy_service: "ProxyService | None" = None,
        base_min: float = 5.0,
        base_max: float = 15.0,
    ) -> None:
        self.proxy_service = proxy_service
        self.base_min = base_min
        self.base_max = base_max
        self._branch_count = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def wait_before_branch(self) -> None:
        """
        Called before starting each new DFS branch.
        Sleeps for a randomized duration and rotates proxy periodically.
        """
        self._branch_count += 1
        delay = self._calculate_delay()
        logger.debug(
            "RateLimitManager: branch #%d, sleeping %.1fs",
            self._branch_count,
            delay,
        )
        await asyncio.sleep(delay)

        # Rotate proxy after every 10 branches
        if self._branch_count % 10 == 0 and self.proxy_service:
            try:
                self.proxy_service.rotate() if hasattr(self.proxy_service, "rotate") else None
                logger.info("RateLimitManager: proxy rotated after %d branches", self._branch_count)
            except Exception as exc:
                logger.warning("Proxy rotation error: %s", exc)

    def get_current_proxy(self) -> str | None:
        """Return the next proxy URL from the pool, or None."""
        if self.proxy_service:
            try:
                return self.proxy_service.get_next_proxy()
            except Exception:
                pass
        return None

    def reset(self) -> None:
        """Reset branch counter (e.g. for a new mapping job)."""
        self._branch_count = 0

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _calculate_delay(self) -> float:
        """Compute the actual sleep duration for the current branch."""
        multiplier = 0.5 if self._is_night_jst() else 1.0
        base = random.uniform(self.base_min, self.base_max) * multiplier

        # Every 3rd branch: add a longer "reading break"
        if self._branch_count % 3 == 0:
            base += random.uniform(30, 60) * multiplier

        return base

    @staticmethod
    def _is_night_jst() -> bool:
        """Return True if current UTC time corresponds to 22:00–08:00 JST."""
        hour_jst = (datetime.now(timezone.utc).hour + RateLimitManager._JST_OFFSET) % 24
        return hour_jst >= 22 or hour_jst < 8
