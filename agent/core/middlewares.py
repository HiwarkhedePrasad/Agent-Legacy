"""Agent middlewares: real token-usage tracking + rate limiting & retries + guardrails.

UsageTracker wraps every model call (planner AND sub-agents) and:
  - throttles ALL model calls through one global minimum-interval limiter, so
    parallel planner + sub-agent calls can't trip per-minute provider limits
    (TokenRouter free tier = 10 req/min);
  - retries transient failures (429 / rate limit / overloaded / network) with
    exponential backoff instead of letting a single bad call kill the run;
  - records the provider-reported usage_metadata attributed to the model that
    actually served the call — no more chars/4 guesses and no more billing
    sub-agent calls to the routed model.

Guardrails cap how many model/tool calls one run can make so a looping planner
can't burn a session.
"""
from __future__ import annotations

import asyncio
import os
import time

from langchain.agents.middleware.model_call_limit import ModelCallLimitMiddleware
from langchain.agents.middleware.tool_call_limit import ToolCallLimitMiddleware
from langchain.agents.middleware.types import wrap_model_call

from agent.cost import CostTracker

MAX_MODEL_CALLS = 80
MAX_TOOL_CALLS = 200

# Rate limiting & retries (tuned for the TokenRouter free tier: 10 req/min).
# 7s minimum between calls => ~8.5 calls/min with headroom for bursts.
MODEL_MIN_INTERVAL = float(os.getenv("MODEL_MIN_INTERVAL", "7.0"))
RETRY_ATTEMPTS = 4           # total tries per model call on transient errors
RETRY_BASE_WAIT = 30.0       # seconds, grows linearly each attempt (30s, 60s, 90s)

# Whole-run retries (last line of defence if the stream itself dies).
RUN_RETRIES = 2              # extra full-run attempts on transient failure
RUN_RETRY_WAIT = 45.0        # seconds, x attempt number (45s, 90s)

# LangGraph superstep budget: each model call / tool batch counts one step
# toward this limit. Research-heavy runs (many searches + fetches) exhaust the
# default of 25 quickly, so give the team real room to work.
RECURSION_LIMIT = int(os.getenv("RECURSION_LIMIT", "200"))

# How many times a run may RESUME from its saved checkpoint after exhausting
# the step budget (each continuation gets a fresh RECURSION_LIMIT budget).
# Together with RECURSION_LIMIT this caps total work per task at
# (1 + MAX_CONTINUATIONS) * RECURSION_LIMIT steps — a genuine ceiling, unlike
# a single arbitrary limit.
MAX_CONTINUATIONS = int(os.getenv("MAX_CONTINUATIONS", "5"))


class CallRateLimiter:
    """One process-wide minimum-interval gate shared by every model call."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                await asyncio.sleep(wait)
                now = time.monotonic()
            self._next_allowed = max(now, self._next_allowed) + self.min_interval


RATE_LIMITER = CallRateLimiter(min_interval=MODEL_MIN_INTERVAL)


def is_transient_error(exc: BaseException) -> bool:
    """True for errors worth waiting out (rate limits, overload, network hiccups)."""
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "429",
            "rate limit",
            "ratelimit",
            "too many requests",
            "request limit",
            "requests within",
            "overloaded",
            "timed out",
            "timeout",
            "connection",
            "econnreset",
            "temporarily unavailable",
            "server_error",
            "internal server error",
            " 502",
            " 503",
            " 504",
        )
    )


def _model_name(model) -> str:
    """Best-effort name of the chat model instance for cost attribution."""
    for attr in ("model", "model_name", "model_id"):
        value = getattr(model, attr, None)
        if isinstance(value, str) and value:
            return value
    return type(model).__name__


def build_usage_middleware(cost: CostTracker):
    """Middleware that throttles, retries, and records real provider usage."""

    @wrap_model_call
    async def track_usage(request, handler):
        await RATE_LIMITER.acquire()
        response = None
        last_exc: BaseException | None = None
        for attempt in range(RETRY_ATTEMPTS):
            try:
                response = await handler(request)
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if is_transient_error(exc) and attempt < RETRY_ATTEMPTS - 1:
                    wait = RETRY_BASE_WAIT * (attempt + 1)
                    await asyncio.sleep(wait)
                    continue
                raise
        if response is None:
            raise last_exc  # pragma: no cover
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
