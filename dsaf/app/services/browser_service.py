"""
Playwright Browser Manager with anti-detection hardening.

All Playwright operations are async. Each execution run MUST use a fresh
browser context — never reuse contexts across loop iterations.
"""

import asyncio
import logging
import random
import time
from typing import Optional, Tuple

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
)

from app.exceptions import BrowserContextError, ProxyBlockedError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Real Japanese browser User-Agent pool (Chrome 120-124, Firefox, Edge, Safari)
# ---------------------------------------------------------------------------
JAPANESE_USER_AGENTS: list[str] = [
    # Chrome 124 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 124 Windows 11 (specific build)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.6367.82 Safari/537.36",
    # Chrome 123 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    # Chrome 123 Windows 11 (specific build)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.122 Safari/537.36",
    # Chrome 122 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.112 Safari/537.36",
    # Chrome 121 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome 120 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.130 Safari/537.36",
    # Chrome 120 Windows 10 (variant build)
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.216 Safari/537.36",
    # Chrome 124 macOS Sonoma
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Chrome 123 macOS Ventura
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.6312.86 Safari/537.36",
    # Chrome 122 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    # Chrome 121 macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    # Chrome 120 macOS (specific build)
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.216 Safari/537.36",
    # Firefox 125 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    # Firefox 124 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    # Firefox 123 Windows 11
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    # Firefox 122 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0",
    # Firefox 121 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    # Edge 124 Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
    # Edge 123 Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    # Safari 17 macOS Sonoma
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    # Safari 16 macOS Ventura
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_3) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.4 Safari/605.1.15",
]

# ---------------------------------------------------------------------------
# Anti-detection stealth script — injected into every new page context
# ---------------------------------------------------------------------------
STEALTH_SCRIPT = """\
() => {
    // 1. Remove navigator.webdriver flag
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true
    });

    // 2. Fake navigator.plugins with 3 realistic plugins
    const makePlugin = (name, desc, filename, mimeTypes) => {
        const plugin = Object.create(Plugin.prototype);
        Object.defineProperties(plugin, {
            name: { value: name, enumerable: true },
            description: { value: desc, enumerable: true },
            filename: { value: filename, enumerable: true },
            length: { value: mimeTypes.length, enumerable: true }
        });
        mimeTypes.forEach((mt, i) => {
            const mime = Object.create(MimeType.prototype);
            Object.defineProperties(mime, {
                type: { value: mt.type, enumerable: true },
                description: { value: mt.description, enumerable: true },
                suffixes: { value: mt.suffixes, enumerable: true }
            });
            plugin[i] = mime;
        });
        return plugin;
    };
    const fakePlugins = [
        makePlugin('Chrome PDF Plugin', 'Portable Document Format', 'internal-pdf-viewer',
            [{ type: 'application/x-google-chrome-pdf', description: 'Portable Document Format', suffixes: 'pdf' }]),
        makePlugin('Chrome PDF Viewer', '', 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
            [{ type: 'application/pdf', description: '', suffixes: 'pdf' }]),
        makePlugin('Native Client', '', 'internal-nacl-plugin', [
            { type: 'application/x-nacl', description: 'Native Client Executable', suffixes: '' },
            { type: 'application/x-pnacl', description: 'Portable Native Client Executable', suffixes: '' }
        ])
    ];
    const pluginArray = Object.create(PluginArray.prototype);
    fakePlugins.forEach((p, i) => { pluginArray[i] = p; });
    Object.defineProperty(pluginArray, 'length', { value: fakePlugins.length, enumerable: true });
    Object.defineProperty(navigator, 'plugins', { get: () => pluginArray, configurable: true });

    // 3. Set Japanese language preferences
    Object.defineProperty(navigator, 'languages', {
        get: () => ['ja', 'ja-JP', 'en-US'],
        configurable: true
    });

    // 4. Define window.chrome.runtime to appear as real Chrome
    if (!window.chrome) {
        window.chrome = {
            runtime: {
                PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux' },
                PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' },
                RequestUpdateCheckStatus: {
                    THROTTLED: 'throttled', NO_UPDATE: 'no_update', UPDATE_AVAILABLE: 'update_available'
                },
                OnInstalledReason: {
                    INSTALL: 'install', UPDATE: 'update', CHROME_UPDATE: 'chrome_update',
                    SHARED_MODULE_UPDATE: 'shared_module_update'
                },
                OnRestartRequiredReason: { APP_UPDATE: 'app_update', OS_UPDATE: 'os_update', PERIODIC: 'periodic' }
            },
            app: { isInstalled: false },
            csi: () => {},
            loadTimes: () => {}
        };
    }

    // 5. Override Notification.permission to 'default' (not 'denied' as in headless)
    if (typeof Notification !== 'undefined') {
        Object.defineProperty(Notification, 'permission', {
            get: () => 'default',
            configurable: true
        });
    }

    // 6. Canvas fingerprint noise — ±1 pixel per channel on getImageData
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(x, y, w, h) {
        const imageData = origGetImageData.call(this, x, y, w, h);
        for (let i = 0; i < imageData.data.length; i += 4) {
            imageData.data[i]   = Math.min(255, Math.max(0, imageData.data[i]   + (Math.random() > 0.5 ? 1 : -1)));
            imageData.data[i+1] = Math.min(255, Math.max(0, imageData.data[i+1] + (Math.random() > 0.5 ? 1 : -1)));
            imageData.data[i+2] = Math.min(255, Math.max(0, imageData.data[i+2] + (Math.random() > 0.5 ? 1 : -1)));
        }
        return imageData;
    };

    // 7. Remove Playwright-specific global markers
    delete window.__playwright;
    delete window.__pw_manual;
}
"""


class TimingHelper:
    """Static helper for all randomized timing operations."""

    @staticmethod
    def page_think_time(config: dict) -> float:
        """
        Return a random float between config page_delay_min and page_delay_max.

        Args:
            config: Pattern timing dict with page_delay_min / page_delay_max keys.

        Returns:
            Seconds to sleep before clicking the next-page button.
        """
        return random.uniform(
            config.get("page_delay_min", 3.0),
            config.get("page_delay_max", 8.0),
        )

    @staticmethod
    def typing_delay() -> float:
        """
        Return a per-keystroke delay in seconds (50-200 ms + Gaussian noise).

        Returns:
            Seconds to sleep between individual keystrokes.
        """
        base_ms = random.uniform(50, 200)
        noise_ms = random.gauss(0, 20)
        return max(0.02, (base_ms + noise_ms) / 1000)

    @staticmethod
    def mouse_movement_delay() -> float:
        """Return a short delay (0.1–0.3 s) simulating mouse-movement time."""
        return random.uniform(0.1, 0.3)

    @staticmethod
    async def ensure_minimum_duration(start_time: float, min_seconds: int):
        """
        If elapsed time since start_time is less than min_seconds, sleep the
        remaining difference plus a random 5–15 s buffer.

        Args:
            start_time: Unix timestamp recorded at the start of the run.
            min_seconds: Minimum survey completion time required by the platform.
        """
        elapsed = time.monotonic() - start_time
        if elapsed < min_seconds:
            remaining = min_seconds - elapsed + random.uniform(5, 15)
            logger.debug(f"Enforcing minimum duration: sleeping {remaining:.1f}s")
            await asyncio.sleep(remaining)


class BrowserService:
    """
    Manages Playwright browser instances with anti-detection hardening.

    Usage pattern::

        service = BrowserService(headless=True)
        context, page = await service.create_context()
        try:
            await service.navigate_with_retry(page, url)
            # ... interact with page ...
        finally:
            await context.close()
        await service.close_all()

    Each run MUST use a fresh browser context via create_context().
    NEVER reuse contexts across loop iterations.
    """

    def __init__(self, headless: bool = True, proxy_url: Optional[str] = None):
        """
        Initialise BrowserService.

        Args:
            headless: False during mapping (user sees browser), True during execution.
            proxy_url: Proxy in format "http://user:pass@host:port" or None.
        """
        self.headless = headless
        self.proxy_url = proxy_url
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._contexts: list[BrowserContext] = []

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_browser(self):
        """Lazily start Playwright and launch Chromium."""
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        if self._browser is None or not self._browser.is_connected():
            launch_args = [
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ]
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=launch_args,
            )
            logger.debug(f"Browser launched (headless={self.headless})")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def create_context(self) -> Tuple[BrowserContext, Page]:
        """
        Create a new ISOLATED browser context with anti-detection hardening.

        Each call produces a completely fresh context with:
        - Random User-Agent from the Japanese UA pool
        - Random viewport (1280–1920 × 768–1080)
        - Locale: ja-JP, Timezone: Asia/Tokyo
        - Stealth init script (navigator.webdriver removed, plugins faked, etc.)
        - No shared cookies / localStorage
        - Optional proxy if proxy_url was supplied

        Returns:
            (BrowserContext, Page) tuple ready for immediate use.

        Raises:
            BrowserContextError: If context creation fails.
        """
        await self._ensure_browser()

        user_agent = random.choice(JAPANESE_USER_AGENTS)
        viewport_w = random.randint(1280, 1920)
        viewport_h = random.randint(768, 1080)

        context_options: dict = {
            "user_agent": user_agent,
            "viewport": {"width": viewport_w, "height": viewport_h},
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
            "java_script_enabled": True,
            "ignore_https_errors": False,
            "extra_http_headers": {
                "Accept-Language": "ja,ja-JP;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        }

        if self.proxy_url:
            context_options["proxy"] = {"server": self.proxy_url}

        try:
            context = await self._browser.new_context(**context_options)
            await context.add_init_script(STEALTH_SCRIPT)
            page = await context.new_page()
            self._contexts.append(context)
            logger.debug(f"New context: UA={user_agent[:55]}…, viewport={viewport_w}×{viewport_h}")
            return context, page
        except Exception as exc:
            raise BrowserContextError(f"Failed to create browser context: {exc}") from exc

    async def navigate_with_retry(self, page: Page, url: str, retries: int = 3) -> bool:
        """
        Navigate to a URL with exponential-backoff retry.

        Args:
            page: Active Playwright page.
            url: Destination URL.
            retries: Maximum number of attempts.

        Returns:
            True on success, False if all retries are exhausted.

        Raises:
            ProxyBlockedError: Immediately on HTTP 403 or 429.
        """
        for attempt in range(retries):
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                if response:
                    if response.status in (403, 429):
                        raise ProxyBlockedError(f"Blocked: HTTP {response.status} on {url}")
                    if response.status >= 400:
                        logger.warning(f"HTTP {response.status} on attempt {attempt + 1} for {url}")
                        if attempt < retries - 1:
                            await asyncio.sleep(2 ** attempt + random.uniform(0.5, 1.5))
                            continue
                        return False
                await asyncio.sleep(random.uniform(0.5, 1.5))
                return True
            except ProxyBlockedError:
                raise
            except Exception as exc:
                logger.warning(f"Navigation attempt {attempt + 1} failed: {exc}")
                if attempt < retries - 1:
                    await asyncio.sleep(2 ** attempt + random.uniform(0.5, 2.0))
                else:
                    logger.error(f"All {retries} navigation attempts exhausted for: {url}")
                    return False
        return False

    async def human_click(self, page: Page, selector: str):
        """
        Simulate a human-like mouse click with random offset and timing.

        Steps:
        1. Resolve element and get bounding box.
        2. Move mouse to centre + small random offset.
        3. Pause 100–300 ms (pre-click hesitation).
        4. Click.
        5. Pause 200–500 ms (post-click reaction).

        Args:
            page: Active Playwright page.
            selector: CSS selector identifying the target element.

        Raises:
            BrowserContextError: If element cannot be found.
        """
        try:
            element = await page.wait_for_selector(selector, timeout=10_000)
        except Exception as exc:
            raise BrowserContextError(f"Element not found for selector '{selector}': {exc}") from exc

        bbox = await element.bounding_box()
        if bbox:
            x = bbox["x"] + bbox["width"] / 2 + random.uniform(-5, 5)
            y = bbox["y"] + bbox["height"] / 2 + random.uniform(-3, 3)
            await page.mouse.move(x, y, steps=random.randint(5, 15))

        await asyncio.sleep(random.uniform(0.1, 0.3))
        await element.click()
        await asyncio.sleep(random.uniform(0.2, 0.5))

    async def human_type(
        self,
        page: Page,
        selector: str,
        text: str,
        delay_range_ms: Optional[list[int]] = None,
    ):
        """
        Type text character-by-character with random per-keystroke delays.
        Introduces an occasional typo+correction (10 % chance per word).

        Args:
            page: Active Playwright page.
            selector: CSS selector of the target text input/textarea.
            text: The text to type.
            delay_range_ms: [min_ms, max_ms] for keystroke timing. Defaults to [50, 150].
        """
        if delay_range_ms is None:
            delay_range_ms = [50, 150]

        await page.click(selector)
        await asyncio.sleep(random.uniform(0.1, 0.3))

        words = text.split(" ")
        for word_idx, word in enumerate(words):
            # Occasional typo simulation
            if random.random() < 0.10 and len(word) > 2:
                typo_pos = random.randint(1, len(word) - 1)
                noise_char = random.choice("abcdefghijklmnopqrstuvwxyz")
                typo_word = word[:typo_pos] + noise_char + word[typo_pos:]
                for ch in typo_word:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(random.uniform(delay_range_ms[0], delay_range_ms[1]) / 1000)
                # Pause then backspace to correct
                await asyncio.sleep(random.uniform(0.2, 0.5))
                for _ in range(len(typo_word) - typo_pos):
                    await page.keyboard.press("Backspace")
                    await asyncio.sleep(random.uniform(0.05, 0.15))
                for ch in word[typo_pos:]:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(random.uniform(delay_range_ms[0], delay_range_ms[1]) / 1000)
            else:
                for ch in word:
                    await page.keyboard.type(ch)
                    await asyncio.sleep(random.uniform(delay_range_ms[0], delay_range_ms[1]) / 1000)

            if word_idx < len(words) - 1:
                await page.keyboard.type(" ")
                await asyncio.sleep(random.uniform(0.04, 0.12))

    async def close_all(self):
        """Gracefully close all open contexts and shut down the browser."""
        for ctx in self._contexts:
            try:
                await ctx.close()
            except Exception as exc:
                logger.warning(f"Error closing browser context: {exc}")
        self._contexts.clear()

        if self._browser:
            try:
                await self._browser.close()
            except Exception as exc:
                logger.warning(f"Error closing browser: {exc}")
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception as exc:
                logger.warning(f"Error stopping Playwright: {exc}")
            self._playwright = None
