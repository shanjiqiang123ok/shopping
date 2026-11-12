"""Agent API routes — one business endpoint: `POST /api/shopping-agent/run`."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from shopping_backend.agent_api.deps import get_runtime, require_token
from shopping_backend.agent_api.runtime import AgentRuntime
from shopping_backend.domain.schemas import AgentRunOut
from shopping_backend.guardrails.validate_input import parse_agent_run

router = APIRouter(prefix="/api/shopping-agent", tags=["agent"], dependencies=[Depends(require_token)])


@router.post("/run", response_model=AgentRunOut)
async def run_agent(request: Request, payload: dict) -> AgentRunOut:
    """L2 runs before the graph: a malformed request never consumes a checkpoint or a model call."""
    parsed = parse_agent_run(payload)
    runtime: AgentRuntime = get_runtime(request)
    return await runtime.run(parsed)
