"""
Action executor — translates Action models into Playwright browser commands.

The agent loop calls execute_action() with a validated Action model and gets
back an ActionResult with success/failure status and a fresh screenshot.
"""

import logging
from datetime import datetime, timezone

from browser import BrowserController
from models import Action, ActionResult, ActionType, ScreenAnalysis, UIElement

logger = logging.getLogger(__name__)


class ActionExecutor:
    """
    Bridges the agent's Action decisions to the browser controller.

    Handles coordinate mapping: Gemini returns normalized [0,1] coords;
    we pass them directly to BrowserController which scales to viewport pixels.
    """

    def __init__(self, browser: BrowserController):
        self.browser = browser

    async def execute(
        self,
        action: Action,
        current_analysis: ScreenAnalysis | None = None,
    ) -> ActionResult:
        """
        Execute an action and return the result with a fresh screenshot.

        Args:
            action: The action to execute.
            current_analysis: Latest screen analysis (used to resolve element_id → coords).

        Returns:
            ActionResult with success flag, optional error, and post-action screenshot.
        """
        logger.info("Executing action: %s | reasoning: %s", action.action_type, action.reasoning)

        try:
            success, error = await self._dispatch(action, current_analysis)
        except Exception as e:
            logger.exception("Unhandled error during action execution")
            success, error = False, str(e)

        # Always capture a fresh screenshot after the action
        try:
            screenshot_b64 = await self.browser.screenshot_b64()
        except Exception:
            screenshot_b64 = None

        current_url = await self.browser.current_url()

        return ActionResult(
            success=success,
            action=action,
            error=error,
            screenshot_b64=screenshot_b64,
            new_url=current_url,
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def _dispatch(
        self,
        action: Action,
        analysis: ScreenAnalysis | None,
    ) -> tuple[bool, str | None]:
        """Route action to the correct handler. Returns (success, error)."""

        match action.action_type:
            case ActionType.NAVIGATE:
                return await self._navigate(action)
            case ActionType.CLICK:
                return await self._click(action, analysis)
            case ActionType.TYPE:
                return await self._type(action)
            case ActionType.SCROLL:
                return await self._scroll(action)
            case ActionType.WAIT:
                return await self._wait(action)
            case ActionType.SCREENSHOT:
                return True, None   # screenshot is always taken above
            case ActionType.COMPLETE:
                logger.info("Task marked complete: %s", action.reasoning)
                return True, None
            case ActionType.ABORT:
                logger.warning("Task aborted: %s", action.reasoning)
                return False, f"Agent aborted: {action.reasoning}"
            case ActionType.CONFIRM:
                # Confirm actions are intercepted BEFORE calling execute()
                # If we reach here, it means confirmation was approved
                return True, None
            case _:
                return False, f"Unknown action type: {action.action_type}"

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    async def _navigate(self, action: Action) -> tuple[bool, str | None]:
        if not action.url:
            return False, "navigate action missing URL"
        await self.browser.navigate(action.url)
        return True, None

    async def _click(
        self,
        action: Action,
        analysis: ScreenAnalysis | None,
    ) -> tuple[bool, str | None]:
        # Prefer element_id lookup from analysis, fall back to raw coords
        norm_x, norm_y = self._resolve_coords(action, analysis)
        if norm_x is None:
            return False, "click action missing coordinates and could not resolve element_id"

        success = await self.browser.click_normalized(norm_x, norm_y)
        if not success:
            return False, f"click at ({norm_x:.3f}, {norm_y:.3f}) failed"
        return True, None

    async def _type(self, action: Action) -> tuple[bool, str | None]:
        if not action.text:
            return False, "type action missing text"
        success = await self.browser.type_text(action.text)
        if not success:
            return False, "type text failed"
        # Press Enter if text looks like a search query (no newline intent)
        # The agent can explicitly include \n in the text if desired
        return True, None

    async def _scroll(self, action: Action) -> tuple[bool, str | None]:
        direction = action.direction or "down"
        amount = action.amount or 300
        success = await self.browser.scroll(direction, amount)
        return (True, None) if success else (False, "scroll failed")

    async def _wait(self, action: Action) -> tuple[bool, str | None]:
        ms = action.wait_ms or 1000
        await self.browser.wait(ms)
        return True, None

    # ------------------------------------------------------------------
    # Coordinate resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_coords(
        action: Action,
        analysis: ScreenAnalysis | None,
    ) -> tuple[float | None, float | None]:
        """
        Return normalized (x, y) for a click action.

        Priority:
        1. element_id → look up element in analysis → use its bounding-box center
        2. Direct x/y from action
        """
        if action.element_id and analysis:
            elem = next(
                (e for e in analysis.elements if e.id == action.element_id),
                None,
            )
            if elem:
                return elem.bounding_box.center_x, elem.bounding_box.center_y

        if action.x is not None and action.y is not None:
            return action.x, action.y

        return None, None


# ------------------------------------------------------------------
# Action history formatter (used in agent loop for Gemini context)
# ------------------------------------------------------------------

def format_action_for_history(action: Action, result: ActionResult) -> str:
    """Return a compact string description of an action + its outcome."""
    status = "✓" if result.success else "✗"
    match action.action_type:
        case ActionType.NAVIGATE:
            return f"{status} navigate({action.url})"
        case ActionType.CLICK:
            coord = f"elem={action.element_id}" if action.element_id else f"({action.x:.2f},{action.y:.2f})"
            return f"{status} click({coord})"
        case ActionType.TYPE:
            return f"{status} type({action.text!r})"
        case ActionType.SCROLL:
            return f"{status} scroll({action.direction}, {action.amount}px)"
        case ActionType.WAIT:
            return f"{status} wait({action.wait_ms}ms)"
        case _:
            return f"{status} {action.action_type.value}"
