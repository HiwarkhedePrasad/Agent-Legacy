"""Agent middlewares: real token-usage tracking + run guardrails.

UsageTracker wraps every model call (planner AND sub-agents) and records the
provider-reported usage_metadata attributed to the model that actually served
the call — no more chars/4 guesses and no more billing sub-agent calls to the
routed model.

Guardrails cap how many model/tool calls one run can make so a looping planner
can't burn a session.
"""
from __future__ import annotations

from langchain.agents.middleware.model_call_limit import ModelCallLimitMiddleware
from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware
from langchain.agents.middleware.types import wrap_model_call

from agent.cost import CostTracker

MAX_MODEL_CALLS = 80
MAX_TOOL_CALLS = 200


def _model_name(model) -> str:
    """Best-effort name of the chat model instance for cost attribution."""
    for attr in ("model", "model_name", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def build_usage_middleware(cost: CostTracker):
    """Middleware that records real provider usage into `cost` per model call."""

    @wrap_model_call
    async def track_usage(request, handler):
        response = await handler(request)
        try:
            model = _model_name(request.model)
            messages = (
                response.result
                if hasattr(response, "result")
                else [response]
            )
            for message in messages:
                usage = getattr(message, "usage_metadata", None)
                if usage:
                    cost.add_usage(
                        model,
                        int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                    )
        except Exception:  # noqa: BLE001
            pass
        return response

    return track_usage


def build_guardrails() -> list:
    """Run-level caps so a runaway planner can't loop forever."""
    return [
        ModelCallLimitMiddleware(run_limit=MAX_MODEL_CALLS, exit_behavior="end"),
        ToolCallLimitMiddleware(run_limit=MAX_TOOL_CALLS, exit_behavior="continue"),
    ]
