"""
Agent loop: observe → reason → act → verify.

The agent runs until the task is complete, it's stuck, or it hits the step limit.
Human-in-the-loop: before any "sensitive" (commit) action, the loop pauses and
emits a WAITING_CONFIRMATION status so the API caller can approve/reject.
"""

import asyncio
import logging
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import AsyncIterator, Callable, Optional

from actions import ActionExecutor, format_action_for_history
from browser import BrowserController
from gemini_vision import GeminiVisionClient
from models import (
    Action,
    ActionResult,
    ActionType,
    AgentStep,
    ScreenAnalysis,
    Task,
    TaskStatus,
)

logger = logging.getLogger(__name__)

# If the agent takes the same action type 3 times in a row → stuck
STUCK_REPEAT_THRESHOLD = 3


class AgentLoop:
    """
    Orchestrates the observe → reason → act → verify loop.

    The loop is designed to run inside an asyncio event loop.
    It emits AgentStep objects via an async generator so the server
    can stream progress to connected WebSocket clients.
    """

    def __init__(
        self,
        task: Task,
        gemini: GeminiVisionClient,
        browser: BrowserController,
        on_step: Optional[Callable[[AgentStep], None]] = None,
    ):
        self.task = task
        self.gemini = gemini
        self.browser = browser
        self.executor = ActionExecutor(browser)
        self.on_step = on_step  # optional callback for real-time updates

        self._action_history: deque[str] = deque(maxlen=10)
        self._pending_confirmation: Optional[Action] = None
        self._confirmation_event = asyncio.Event()
        self._confirmation_approved: Optional[bool] = None
        self._current_analysis: Optional[ScreenAnalysis] = None

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> Task:
        """
        Run the agent loop until completion, failure, or step limit.

        Returns:
            Updated Task object with final status.
        """
        self.task.status = TaskStatus.RUNNING
        logger.info("[Agent] Starting task: %s", self.task.description)

        try:
            # Navigate to starting URL
            await self.browser.navigate(self.task.start_url)

            for step_num in range(1, self.task.max_steps + 1):
                self.task.current_step = step_num
                logger.info("[Agent] Step %d/%d", step_num, self.task.max_steps)

                step = await self._execute_step(step_num)
                self.task.steps.append(step.model_dump())

                if self.on_step:
                    self.on_step(step)

                # Check terminal conditions
                if step.action and step.action.action_type == ActionType.COMPLETE:
                    self.task.status = TaskStatus.COMPLETE
                    self.task.result = step.action.reasoning or "Task completed successfully."
                    break

                if step.action and step.action.action_type == ActionType.ABORT:
                    self.task.status = TaskStatus.FAILED
                    self.task.error = step.action.reasoning or "Agent gave up."
                    break

                if self._current_analysis and self._current_analysis.is_task_complete:
                    self.task.status = TaskStatus.COMPLETE
                    self.task.result = "Task completed (detected by vision analysis)."
                    break

                if self._is_stuck():
                    self.task.status = TaskStatus.FAILED
                    self.task.error = "Agent appears to be stuck in a loop."
                    break

            else:
                # Reached max steps
                self.task.status = TaskStatus.FAILED
                self.task.error = f"Exceeded maximum steps ({self.task.max_steps})."

        except asyncio.CancelledError:
            self.task.status = TaskStatus.ABORTED
            self.task.error = "Task was cancelled."
        except Exception as e:
            logger.exception("[Agent] Unhandled error")
            self.task.status = TaskStatus.FAILED
            self.task.error = str(e)

        logger.info("[Agent] Task finished: %s", self.task.status)
        return self.task

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    async def _execute_step(self, step_num: int) -> AgentStep:
        """Run a single observe → reason → (confirm?) → act cycle."""
        timestamp = datetime.now(timezone.utc).isoformat()

        # 1. OBSERVE: capture screenshot
        screenshot_bytes = await self.browser.screenshot()
        screenshot_b64 = None
        import base64
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        current_url = await self.browser.current_url()

        # 2. ANALYZE: send to Gemini vision
        analysis = await self.gemini.analyze_screenshot(
            screenshot_bytes,
            self.task.description,
            current_url,
        )
        self._current_analysis = analysis
        logger.info(
            "[Agent] Page: %s | Progress: %s | Suggested: %s",
            analysis.page_description[:60],
            analysis.task_progress,
            analysis.suggested_next_action,
        )

        # 3. REASON: decide next action
        raw_action = await self.gemini.decide_next_action(
            analysis,
            self.task.description,
            list(self._action_history),
        )
        action = self._parse_action(raw_action)
        logger.info("[Agent] Action decided: %s | sensitive=%s", action.action_type, action.is_sensitive)

        # 4. CONFIRM if sensitive
        if action.is_sensitive:
            logger.info("[Agent] Sensitive action — waiting for human confirmation")
            self.task.status = TaskStatus.WAITING_CONFIRMATION
            self._pending_confirmation = action
            self._confirmation_event.clear()
            self._confirmation_approved = None

            # Wait up to 5 minutes for confirmation
            try:
                await asyncio.wait_for(self._confirmation_event.wait(), timeout=300)
            except asyncio.TimeoutError:
                logger.warning("[Agent] Confirmation timeout — aborting sensitive action")
                action = Action(
                    action_type=ActionType.ABORT,
                    reasoning="Confirmation timed out for sensitive action.",
                )
            else:
                if not self._confirmation_approved:
                    action = Action(
                        action_type=ActionType.ABORT,
                        reasoning="User rejected the sensitive action.",
                    )

            self.task.status = TaskStatus.RUNNING
            self._pending_confirmation = None

        # 5. ACT: execute the action
        result = await self.executor.execute(action, analysis)
        self._action_history.append(format_action_for_history(action, result))

        return AgentStep(
            step_number=step_num,
            screenshot_b64=screenshot_b64,
            analysis=analysis,
            action=action,
            action_result=result,
            timestamp=timestamp,
        )

    # ------------------------------------------------------------------
    # Human-in-the-loop confirmation
    # ------------------------------------------------------------------

    def confirm_action(self, approved: bool):
        """
        Called externally (by the API) to approve or reject a pending sensitive action.
        """
        self._confirmation_approved = approved
        self._confirmation_event.set()

    @property
    def pending_confirmation(self) -> Optional[Action]:
        """Return the action waiting for confirmation, if any."""
        return self._pending_confirmation

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_action(raw: dict) -> Action:
        """Build an Action model from Gemini's raw JSON dict."""
        try:
            return Action(**raw)
        except Exception as e:
            logger.warning("Failed to parse action dict: %s | dict: %s", e, raw)
            return Action(
                action_type=ActionType.WAIT,
                wait_ms=1000,
                reasoning="Fallback wait due to action parse failure.",
            )

    def _is_stuck(self) -> bool:
        """
        Detect repetitive patterns in the action history that suggest the agent
        is stuck in a loop.
        """
        if len(self._action_history) < STUCK_REPEAT_THRESHOLD:
            return False

        recent = list(self._action_history)[-STUCK_REPEAT_THRESHOLD:]
        # All recent actions are identical → stuck
        return len(set(recent)) == 1
