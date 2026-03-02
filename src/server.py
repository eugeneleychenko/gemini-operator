"""
FastAPI backend for Gemini Operator.

Endpoints:
  POST /tasks                   — create and start a new task
  GET  /tasks/{task_id}         — get task status + step history
  POST /tasks/{task_id}/confirm — approve or reject a pending sensitive action
  GET  /tasks/{task_id}/stream  — SSE stream of real-time step updates
  GET  /health                  — health check

The agent loop runs in a background asyncio task per session.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from agent import AgentLoop
from browser import BrowserController
from gemini_vision import GeminiVisionClient
from models import (
    ConfirmActionRequest,
    CreateTaskRequest,
    Task,
    TaskResponse,
    TaskStatus,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Gemini Operator",
    description="Universal web task agent powered by Gemini 2.5 Flash vision",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# In-memory state (use Firestore in production)
# ---------------------------------------------------------------------------
# task_id → Task
_tasks: dict[str, Task] = {}
# task_id → AgentLoop
_agents: dict[str, AgentLoop] = {}
# task_id → asyncio.Queue for SSE events
_event_queues: dict[str, asyncio.Queue] = {}
# task_id → asyncio.Task (background runner)
_runner_tasks: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------
# Gemini client (singleton)
# ---------------------------------------------------------------------------
def _get_gemini_client() -> GeminiVisionClient:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="GEMINI_API_KEY not configured")
    return GeminiVisionClient(api_key=api_key)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/tasks", response_model=TaskResponse)
async def create_task(req: CreateTaskRequest, background_tasks: BackgroundTasks):
    """Create a new task and immediately start the agent loop."""
    task_id = str(uuid.uuid4())
    task = Task(
        task_id=task_id,
        description=req.description,
        start_url=req.start_url,
        max_steps=req.max_steps,
        status=TaskStatus.PENDING,
    )
    _tasks[task_id] = task
    _event_queues[task_id] = asyncio.Queue(maxsize=500)

    # Start agent in background
    background_tasks.add_task(_run_agent_task, task_id, req)

    logger.info("Task created: %s | %s", task_id, req.description)
    return _task_to_response(task)


@app.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    """Get current task status and step history."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return _task_to_response(task)


@app.post("/tasks/{task_id}/confirm", response_model=TaskResponse)
async def confirm_action(task_id: str, req: ConfirmActionRequest):
    """Approve or reject a pending sensitive action."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.WAITING_CONFIRMATION:
        raise HTTPException(status_code=400, detail="Task is not waiting for confirmation")

    agent = _agents.get(task_id)
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not found")

    agent.confirm_action(req.approved)
    logger.info("Task %s confirmation: approved=%s", task_id, req.approved)
    return _task_to_response(task)


@app.get("/tasks/{task_id}/stream")
async def stream_task(task_id: str):
    """
    Server-Sent Events stream of agent step updates.
    Connect and receive real-time progress for a running task.
    """
    if task_id not in _tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    queue = _event_queues.get(task_id)
    if not queue:
        raise HTTPException(status_code=404, detail="Event queue not found")

    async def event_generator():
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=30)
                if event is None:  # sentinel: task finished
                    yield "data: {\"type\": \"done\"}\n\n"
                    break
                yield f"data: {json.dumps(event)}\n\n"
            except asyncio.TimeoutError:
                # Keep-alive ping
                yield ": ping\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.delete("/tasks/{task_id}")
async def cancel_task(task_id: str):
    """Cancel a running task."""
    runner = _runner_tasks.get(task_id)
    if runner and not runner.done():
        runner.cancel()
    task = _tasks.get(task_id)
    if task:
        task.status = TaskStatus.ABORTED
        task.error = "Cancelled by user."
    return {"cancelled": True}


# ---------------------------------------------------------------------------
# Serve frontend (optional, if built)
# ---------------------------------------------------------------------------
_frontend_dir = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(_frontend_dir):
    app.mount("/static", StaticFiles(directory=_frontend_dir), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def root():
        with open(os.path.join(_frontend_dir, "index.html")) as f:
            return HTMLResponse(content=f.read())
else:
    @app.get("/")
    async def root():
        return {"message": "Gemini Operator API", "docs": "/docs"}


# ---------------------------------------------------------------------------
# Background agent runner
# ---------------------------------------------------------------------------

async def _run_agent_task(task_id: str, req: CreateTaskRequest):
    """Background coroutine: runs the browser + agent loop for a task."""
    task = _tasks[task_id]
    queue = _event_queues[task_id]

    gemini = _get_gemini_client()

    async with BrowserController(headless=True) as browser:
        def on_step(step):
            """Called by agent loop after each step — push to SSE queue."""
            try:
                event_data = {
                    "type": "step",
                    "task_id": task_id,
                    "step": step.step_number,
                    "status": task.status.value,
                    "screenshot": step.screenshot_b64,
                    "analysis": step.analysis.model_dump() if step.analysis else None,
                    "action": step.action.model_dump() if step.action else None,
                    "action_result": {
                        "success": step.action_result.success,
                        "error": step.action_result.error,
                        "new_url": step.action_result.new_url,
                    } if step.action_result else None,
                    "timestamp": step.timestamp,
                }
                # Drop screenshot from queue event to save bandwidth
                # Frontend can fetch full step data via GET /tasks/{id}
                queue.put_nowait({
                    **event_data,
                    "screenshot": event_data["screenshot"],  # keep for live preview
                })
            except asyncio.QueueFull:
                logger.warning("Event queue full for task %s", task_id)

        agent = AgentLoop(
            task=task,
            gemini=gemini,
            browser=browser,
            on_step=on_step,
        )
        _agents[task_id] = agent

        runner = asyncio.current_task()
        _runner_tasks[task_id] = runner

        await agent.run()

    # Send sentinel to close SSE stream
    await queue.put(None)
    logger.info("Agent runner finished for task %s | status=%s", task_id, task.status)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_to_response(task: Task) -> TaskResponse:
    return TaskResponse(
        task_id=task.task_id,
        status=task.status,
        current_step=task.current_step,
        result=task.result,
        error=task.error,
        steps=task.steps,
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        workers=1,  # Must be 1 — we use in-process shared state
    )
