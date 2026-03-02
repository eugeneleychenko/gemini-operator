"""
Playwright browser controller.

Manages a single browser session: navigate, click, type, scroll, screenshot.
Coordinates received from Gemini are normalized [0,1] and scaled to viewport.
"""

import asyncio
import base64
import logging
from contextlib import asynccontextmanager
from typing import Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    async_playwright,
    TimeoutError as PlaywrightTimeout,
)

logger = logging.getLogger(__name__)

# Viewport dimensions used consistently throughout
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 900

# How long to wait for navigation/load events (ms)
NAVIGATION_TIMEOUT = 30_000
ACTION_TIMEOUT = 10_000


class BrowserController:
    """
    Wraps a Playwright browser for the agent loop.

    Usage:
        async with BrowserController() as ctrl:
            await ctrl.navigate("https://google.com")
            screenshot = await ctrl.screenshot()
            await ctrl.click_normalized(0.5, 0.3)
    """

    def __init__(self, headless: bool = True):
        self.headless = headless
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self):
        """Launch Playwright + Chromium."""
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        self._context = await self._browser.new_context(
            viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        self._page = await self._context.new_page()
        self._page.set_default_timeout(ACTION_TIMEOUT)
        logger.info("Browser started (headless=%s)", self.headless)

    async def stop(self):
        """Gracefully shut down Playwright."""
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("Browser stopped")

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    async def navigate(self, url: str) -> str:
        """Navigate to a URL. Returns the final URL after redirects."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        logger.info("Navigating to %s", url)
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT)
            await self._wait_for_load()
        except PlaywrightTimeout:
            logger.warning("Navigation timeout for %s — proceeding anyway", url)
        return self._page.url

    async def _wait_for_load(self, extra_ms: int = 1000):
        """Wait for network idle + extra settle time."""
        try:
            await self._page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(extra_ms / 1000)

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def screenshot(self) -> bytes:
        """Capture full-page screenshot and return raw PNG bytes."""
        png_bytes = await self._page.screenshot(
            type="png",
            full_page=False,   # viewport only — more predictable for coordinates
        )
        logger.debug("Screenshot captured (%d bytes)", len(png_bytes))
        return png_bytes

    async def screenshot_b64(self) -> str:
        """Capture screenshot and return base64-encoded PNG string."""
        return base64.b64encode(await self.screenshot()).decode()

    # ------------------------------------------------------------------
    # Click
    # ------------------------------------------------------------------

    async def click_normalized(self, norm_x: float, norm_y: float) -> bool:
        """
        Click at normalized coordinates (0–1 relative to viewport).

        Args:
            norm_x: Horizontal position (0 = left, 1 = right).
            norm_y: Vertical position (0 = top, 1 = bottom).

        Returns:
            True on success, False on error.
        """
        px = int(norm_x * VIEWPORT_WIDTH)
        py = int(norm_y * VIEWPORT_HEIGHT)
        return await self.click_pixel(px, py)

    async def click_pixel(self, x: int, y: int) -> bool:
        """Click at absolute pixel coordinates."""
        logger.info("Click at pixel (%d, %d)", x, y)
        try:
            await self._page.mouse.click(x, y)
            await self._wait_for_load(500)
            return True
        except Exception as e:
            logger.warning("Click failed at (%d, %d): %s", x, y, e)
            return False

    # ------------------------------------------------------------------
    # Keyboard / typing
    # ------------------------------------------------------------------

    async def type_text(self, text: str, delay_ms: int = 50) -> bool:
        """Type text into the currently focused element."""
        logger.info("Typing: %r", text[:50])
        try:
            await self._page.keyboard.type(text, delay=delay_ms)
            return True
        except Exception as e:
            logger.warning("Type failed: %s", e)
            return False

    async def press_key(self, key: str) -> bool:
        """Press a keyboard key (e.g. 'Enter', 'Tab', 'Escape')."""
        logger.info("Key press: %s", key)
        try:
            await self._page.keyboard.press(key)
            await self._wait_for_load(500)
            return True
        except Exception as e:
            logger.warning("Key press failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Scroll
    # ------------------------------------------------------------------

    async def scroll(self, direction: str = "down", amount: int = 300) -> bool:
        """
        Scroll the page.

        Args:
            direction: "up" or "down".
            amount: Pixels to scroll.
        """
        delta = amount if direction == "down" else -amount
        logger.info("Scroll %s %dpx", direction, amount)
        try:
            await self._page.mouse.wheel(0, delta)
            await asyncio.sleep(0.3)
            return True
        except Exception as e:
            logger.warning("Scroll failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Wait / state
    # ------------------------------------------------------------------

    async def wait(self, ms: int = 1000):
        """Wait for a specified number of milliseconds."""
        await asyncio.sleep(ms / 1000)

    async def current_url(self) -> str:
        """Return the current page URL."""
        return self._page.url

    async def page_title(self) -> str:
        """Return the current page title."""
        return await self._page.title()

    async def evaluate(self, js: str):
        """Evaluate arbitrary JavaScript and return the result."""
        return await self._page.evaluate(js)
