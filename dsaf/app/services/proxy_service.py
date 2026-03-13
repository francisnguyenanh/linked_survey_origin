"""
Proxy pool manager with health tracking and round-robin selection.

Supports HTTP and SOCKS5 proxies in the format:
    protocol://user:pass@host:port
"""

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_COOLDOWN_SECONDS = 1800  # 30 minutes before a failed proxy is retried


class ProxyService:
    """
    Manages a pool of proxies with health tracking.

    Supports HTTP and SOCKS5 proxies.
    Input format: list of "protocol://user:pass@host:port" strings.
    """

    def __init__(self, proxy_list: list[str]):
        """
        Initialise with a list of proxy URL strings.

        Args:
            proxy_list: List of proxy URLs, e.g. ["http://user:pass@1.2.3.4:8080"].
        """
        self._proxies: list[str] = [p.strip() for p in proxy_list if p.strip()]
        self._current_index: int = 0
        # Map of proxy_url -> unix timestamp when it was marked failed
        self._failed_at: dict[str, float] = {}
        self._request_counts: dict[str, int] = {p: 0 for p in self._proxies}
        self._fail_counts: dict[str, int] = {p: 0 for p in self._proxies}
        logger.info(f"ProxyService initialised with {len(self._proxies)} proxies")

    def get_next_proxy(self) -> Optional[str]:
        """
        Return the next available proxy using round-robin selection.
        Skips proxies currently in cool-down.

        Returns:
            Proxy URL string, or None if no proxies are configured/available.
        """
        if not self._proxies:
            return None

        now = time.monotonic()
        attempts = 0
        total = len(self._proxies)

        while attempts < total:
            proxy = self._proxies[self._current_index % total]
            self._current_index = (self._current_index + 1) % total
            attempts += 1

            failed_at = self._failed_at.get(proxy)
            if failed_at and (now - failed_at) < _COOLDOWN_SECONDS:
                logger.debug(f"Skipping proxy in cool-down: {_mask_proxy(proxy)}")
                continue

            # Remove from failed list if cool-down has expired
            if proxy in self._failed_at:
                del self._failed_at[proxy]

            self._request_counts[proxy] = self._request_counts.get(proxy, 0) + 1
            return proxy

        logger.warning("All proxies are currently in cool-down. Returning None.")
        return None

    def mark_failed(self, proxy_url: str):
        """
        Mark a proxy as failed and remove it from rotation for _COOLDOWN_SECONDS.

        Args:
            proxy_url: The proxy URL that failed.
        """
        self._failed_at[proxy_url] = time.monotonic()
        self._fail_counts[proxy_url] = self._fail_counts.get(proxy_url, 0) + 1
        logger.warning(
            f"Proxy marked as failed (cooldown {_COOLDOWN_SECONDS // 60}min): "
            f"{_mask_proxy(proxy_url)} (total fails: {self._fail_counts[proxy_url]})"
        )

    def get_stats(self) -> dict:
        """
        Return health statistics for the entire proxy pool.

        Returns:
            Dict containing total, active, failed counts and per-proxy details.
        """
        now = time.monotonic()
        active = []
        failed = []

        for proxy in self._proxies:
            failed_at = self._failed_at.get(proxy)
            if failed_at and (now - failed_at) < _COOLDOWN_SECONDS:
                failed.append(proxy)
            else:
                active.append(proxy)

        return {
            "total": len(self._proxies),
            "active": len(active),
            "in_cooldown": len(failed),
            "proxies": [
                {
                    "url": _mask_proxy(p),
                    "requests": self._request_counts.get(p, 0),
                    "failures": self._fail_counts.get(p, 0),
                    "status": "cooldown" if p in failed else "active",
                }
                for p in self._proxies
            ],
        }


def _mask_proxy(proxy_url: str) -> str:
    """Mask credentials in a proxy URL for safe logging."""
    try:
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(proxy_url)
        if parsed.password:
            masked = parsed._replace(netloc=f"{parsed.username}:***@{parsed.hostname}:{parsed.port}")
            return urlunparse(masked)
    except Exception:
        pass
    return proxy_url
