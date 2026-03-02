"""
Pydantic models for Gemini Operator tasks, actions, and agent state.
"""

from enum import Enum
from typing import Any, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# UI element / bounding-box models (returned by Gemini vision analysis)
# ---------------------------------------------------------------------------

class BoundingBox(BaseModel):
    """Normalized bounding box [0-1] as returned by Gemini vision."""
    x: float = Field(..., ge=0.0, le=1.0, description="Left edge (0–1)")
    y: float = Field(..., ge=0.0, le=1.0, description="Top edge (0–1)")
    width: float = Field(..., ge=0.0, le=1.0, description="Width (0–1)")
    height: float = Field(..., ge=0.0, le=1.0, description="Height (0–1)")

    @property
    def center_x(self) -> float:
        return self.x + self.width / 2

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2

    def to_pixel(self, viewport_width: int, viewport_height: int) -> "PixelBox":
        return PixelBox(
            x=int(self.x * viewport_width),
            y=int(self.y * viewport_height),
            width=int(self.width * viewport_width),
            height=int(self.height * viewport_height),
        )


class PixelBox(BaseModel):
    """Absolute pixel bounding box."""
    x: int
    y: int
    width: int
    height: int

    @property
    def center_x(self) -> int:
        return self.x + self.width // 2

    @property
    def center_y(self) -> int:
        return self.y + self.height // 2


class UIElement(BaseModel):
    """A UI element identified by Gemini vision analysis."""
    id: str = Field(..., description="Unique identifier for this element")
    element_type: str = Field(
        ...,
        description="Type: button, input, link, image, text, select, checkbox, etc.",
    )
    label: str = Field(..., description="Human-readable label or visible text")
    description: str = Field("", description="More context about the element")
    bounding_box: BoundingBox
    is_interactive: bool = Field(True, description="Whether the element can be clicked/typed into")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="Detection confidence")


class ScreenAnalysis(BaseModel):
    """Full analysis of a screenshot returned by Gemini vision."""
    page_title: str = Field("", description="Title or heading of the page")
    page_description: str = Field("", description="Brief description of what the page is showing")
    url: str = Field("", description="Current URL if visible")
    elements: list[UIElement] = Field(default_factory=list)
    task_progress: str = Field("", description="Assessment of task progress")
    suggested_next_action: str = Field("", description="Natural-language suggestion for next step")
    is_task_complete: bool = Field(False, description="Whether the task appears to be done")
    is_stuck: bool = Field(False, description="Whether the agent appears stuck / looping")


# ---------------------------------------------------------------------------
# Action models
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    WAIT = "wait"
    SCREENSHOT = "screenshot"
    CONFIRM = "confirm"       # human-in-the-loop confirmation gate
    COMPLETE = "complete"     # task finished
    ABORT = "abort"           # agent giving up


class Action(BaseModel):
    """An action the agent wants to perform."""
    action_type: ActionType
    # NAVIGATE
    url: Optional[str] = None
    # CLICK
    element_id: Optional[str] = None
    x: Optional[float] = None   # normalized 0–1 or pixel, depending on context
    y: Optional[float] = None
    # TYPE
    text: Optional[str] = None
    # SCROLL
    direction: Optional[str] = None   # "up" | "down"
    amount: Optional[int] = 300       # pixels
    # WAIT
    wait_ms: Optional[int] = 1000
    # Metadata
    reasoning: str = Field("", description="Why the agent chose this action")
    is_sensitive: bool = Field(
        False,
        description="True for commit actions (submit, purchase, delete) requiring human approval",
    )


class ActionResult(BaseModel):
    """Result of executing an action."""
    success: bool
    action: Action
    error: Optional[str] = None
    screenshot_b64: Optional[str] = None   # base64-encoded PNG taken after action
    new_url: Optional[str] = None


# ---------------------------------------------------------------------------
# Task / session models
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_CONFIRMATION = "waiting_confirmation"
    COMPLETE = "complete"
    FAILED = "failed"
    ABORTED = "aborted"


class Task(BaseModel):
    """A user-submitted automation task."""
    task_id: str
    description: str = Field(..., description="Natural-language task description")
    start_url: str = Field("https://www.google.com", description="URL to start from")
    status: TaskStatus = TaskStatus.PENDING
    max_steps: int = Field(30, description="Maximum agent loop iterations")
    current_step: int = 0
    steps: list[dict[str, Any]] = Field(default_factory=list)
    result: Optional[str] = None
    error: Optional[str] = None


class AgentStep(BaseModel):
    """A single step in the agent's execution trace."""
    step_number: int
    screenshot_b64: Optional[str] = None
    analysis: Optional[ScreenAnalysis] = None
    action: Optional[Action] = None
    action_result: Optional[ActionResult] = None
    timestamp: Optional[str] = None


# ---------------------------------------------------------------------------
# API request/response models
# ---------------------------------------------------------------------------

class CreateTaskRequest(BaseModel):
    description: str = Field(..., description="What should the agent do?")
    start_url: str = Field("https://www.google.com", description="Starting URL")
    max_steps: int = Field(30, ge=1, le=100)


class ConfirmActionRequest(BaseModel):
    task_id: str
    approved: bool


class TaskResponse(BaseModel):
    task_id: str
    status: TaskStatus
    current_step: int
    result: Optional[str] = None
    error: Optional[str] = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
