"""
Gemini vision client — screenshot → structured screen analysis.

Uses the `google-genai` SDK (NOT the deprecated google-generativeai).
Model: gemini-2.5-flash (vision capable, fast, cheap).
"""

import base64
import json
import logging
import re
from pathlib import Path

from google import genai
from google.genai import types

from models import BoundingBox, ScreenAnalysis, UIElement

logger = logging.getLogger(__name__)

# Gemini model to use for vision tasks
VISION_MODEL = "gemini-2.5-flash-preview-04-17"

# System prompt for screen analysis
ANALYSIS_SYSTEM_PROMPT = """You are a precise UI analysis assistant. Your job is to examine screenshots of web pages and identify interactive UI elements and the current page state.

When given a screenshot, respond with a JSON object following this exact schema:
{
  "page_title": "visible page title or heading",
  "page_description": "1-2 sentence description of the page",
  "url": "URL shown in the browser if visible, else empty string",
  "elements": [
    {
      "id": "elem_1",
      "element_type": "button|input|link|image|text|select|checkbox|radio|textarea",
      "label": "visible text or aria label",
      "description": "brief description of purpose",
      "bounding_box": {
        "x": 0.1,
        "y": 0.2,
        "width": 0.3,
        "height": 0.05
      },
      "is_interactive": true,
      "confidence": 0.95
    }
  ],
  "task_progress": "brief assessment of task completion progress",
  "suggested_next_action": "natural-language description of the recommended next action",
  "is_task_complete": false,
  "is_stuck": false
}

IMPORTANT:
- bounding_box values are normalized to [0.0, 1.0] relative to the screenshot dimensions
- x, y are the TOP-LEFT corner of the element
- Only include elements that are visible and within the viewport
- Prioritize interactive elements (buttons, inputs, links)
- is_task_complete should be true only when the task is clearly finished
- is_stuck should be true if you see a CAPTCHA, error loop, or have no viable action
- Return ONLY valid JSON, no markdown fences, no explanation text
"""

REASONING_SYSTEM_PROMPT = """You are an autonomous web agent. Given the current screen state and a task description, decide the single best next action to take.

Respond with a JSON object:
{
  "action_type": "navigate|click|type|scroll|wait|complete|abort",
  "url": "URL to navigate to (navigate only)",
  "element_id": "id from screen analysis (click/type)",
  "x": 0.5,
  "y": 0.3,
  "text": "text to type (type only)",
  "direction": "up|down (scroll only)",
  "amount": 300,
  "wait_ms": 1000,
  "reasoning": "why you chose this action",
  "is_sensitive": false
}

Mark is_sensitive=true for actions that submit forms, place orders, delete data, or make purchases.
Use element_id when a matching element was identified in the analysis, else use x/y coordinates.
Return ONLY valid JSON.
"""


class GeminiVisionClient:
    """Wraps Gemini vision API for screenshot analysis and agent reasoning."""

    def __init__(self, api_key: str):
        self.client = genai.Client(api_key=api_key)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def analyze_screenshot(
        self,
        screenshot_bytes: bytes,
        task_description: str,
        current_url: str = "",
    ) -> ScreenAnalysis:
        """
        Send a screenshot to Gemini and get back a structured ScreenAnalysis.

        Args:
            screenshot_bytes: Raw PNG bytes from Playwright.
            task_description: What the user wants to accomplish.
            current_url: Current browser URL (for context).

        Returns:
            Parsed ScreenAnalysis object.
        """
        user_prompt = (
            f"Task: {task_description}\n"
            f"Current URL: {current_url or 'unknown'}\n\n"
            "Analyze this screenshot and return the JSON analysis."
        )

        response_text = await self._call_vision(
            system=ANALYSIS_SYSTEM_PROMPT,
            user_text=user_prompt,
            image_bytes=screenshot_bytes,
        )

        return self._parse_screen_analysis(response_text)

    async def decide_next_action(
        self,
        analysis: ScreenAnalysis,
        task_description: str,
        step_history: list[str],
    ) -> dict:
        """
        Given the current screen analysis, decide what action to take next.

        Returns raw dict (caller should construct Action model).
        """
        history_str = "\n".join(step_history[-5:]) if step_history else "None"

        user_prompt = (
            f"Task: {task_description}\n\n"
            f"Current page: {analysis.page_description}\n"
            f"URL: {analysis.url}\n"
            f"Task progress: {analysis.task_progress}\n"
            f"Suggested next: {analysis.suggested_next_action}\n\n"
            f"Available elements:\n{self._format_elements(analysis.elements)}\n\n"
            f"Recent action history:\n{history_str}\n\n"
            "What is the single best next action? Return JSON."
        )

        response_text = await self._call_text(
            system=REASONING_SYSTEM_PROMPT,
            user_text=user_prompt,
        )

        return self._parse_json(response_text)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _call_vision(
        self,
        system: str,
        user_text: str,
        image_bytes: bytes,
    ) -> str:
        """Call Gemini with an image + text prompt."""
        image_b64 = base64.b64encode(image_bytes).decode()

        response = self.client.models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            inline_data=types.Blob(
                                mime_type="image/png",
                                data=image_b64,
                            )
                        ),
                        types.Part(text=user_text),
                    ],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )

        return response.text or ""

    async def _call_text(self, system: str, user_text: str) -> str:
        """Call Gemini with text-only prompt (for reasoning step)."""
        response = self.client.models.generate_content(
            model=VISION_MODEL,
            contents=[
                types.Content(
                    role="user",
                    parts=[types.Part(text=user_text)],
                )
            ],
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.1,
                response_mime_type="application/json",
            ),
        )

        return response.text or ""

    def _parse_screen_analysis(self, raw: str) -> ScreenAnalysis:
        """Parse Gemini's JSON response into a ScreenAnalysis."""
        try:
            data = self._parse_json(raw)
            # Coerce elements
            elements = []
            for i, elem_data in enumerate(data.get("elements", [])):
                try:
                    bb_data = elem_data.get("bounding_box", {})
                    bb = BoundingBox(
                        x=float(bb_data.get("x", 0)),
                        y=float(bb_data.get("y", 0)),
                        width=float(bb_data.get("width", 0.1)),
                        height=float(bb_data.get("height", 0.05)),
                    )
                    elem_data["bounding_box"] = bb
                    elem_data.setdefault("id", f"elem_{i+1}")
                    elem_data.setdefault("confidence", 1.0)
                    elements.append(UIElement(**elem_data))
                except Exception as e:
                    logger.warning("Skipping malformed element %d: %s", i, e)

            data["elements"] = elements
            return ScreenAnalysis(**data)

        except Exception as e:
            logger.error("Failed to parse screen analysis: %s\nRaw: %s", e, raw[:500])
            return ScreenAnalysis(
                page_description="Failed to parse Gemini response",
                task_progress="unknown",
                suggested_next_action="retry",
            )

    @staticmethod
    def _parse_json(raw: str) -> dict:
        """Extract and parse JSON from Gemini's response (strips markdown fences)."""
        # Strip markdown code fences if present
        cleaned = re.sub(r"```(?:json)?\s*", "", raw).strip()
        cleaned = cleaned.rstrip("`").strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            # Try to find a JSON object in the response
            match = re.search(r"\{.*\}", cleaned, re.DOTALL)
            if match:
                return json.loads(match.group())
            raise

    @staticmethod
    def _format_elements(elements: list[UIElement]) -> str:
        """Format elements list for the reasoning prompt."""
        lines = []
        for elem in elements:
            if elem.is_interactive:
                lines.append(
                    f"  [{elem.id}] {elem.element_type}: '{elem.label}' "
                    f"at ({elem.bounding_box.center_x:.2f}, {elem.bounding_box.center_y:.2f})"
                )
        return "\n".join(lines) if lines else "  (no interactive elements detected)"
