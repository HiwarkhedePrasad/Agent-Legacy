"""Mid-run LLM escalation: a cheap orchestrator can hand ONE hard reasoning
subtask to a strong model without spawning a full sub-agent. Keeps most calls
on the cheap model (cost optimization) while enabling deep reasoning on demand.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.tools import tool

from agent.cost import CostTracker
from agent.router import build_chat, get_model_spec, Tier

_COST_PREFIX = "\n[strong-llm] "

# Kept *inside* a closure so the model spec is resolved at tool-build time and
# shared with the runner for cost attribution.


def _build_strong_chat():
    return build_chat(get_model_spec(Tier.COMPLEX))


def build_escalation_tool(cost: CostTracker | None = None):
    strong_chat = _build_strong_chat()

    @tool
    async def route_to_strong_llm(task: str, context: str = "") -> str:
        """Runs ONE hard reasoning/question subtask on the most capable LLM and
        returns its answer.

        Use ONLY for a self-contained reasoning or knowledge subtask that's too
        hard for you (the planner) — e.g. a tricky math/logic/analysis step.
        `context` is optional supporting material. Keep `task` as a complete,
        self-sufficient question. You only get back the model's text answer.
        """
        prompt = f"{context}\n\n{task}".strip() if context else task.strip()
        msg = [
            SystemMessage(
                "You are a highly capable reasoning model. Answer the task "
                "precisely and concisely. No tools available."
            ),
            HumanMessage(prompt),
        ]
        resp = await strong_chat.ainvoke(msg)
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
        if cost is not None:
            # This call bypasses the graph's usage middleware, so capture the
            # provider-reported usage here directly (falls back to chars/4).
            model_name = get_model_spec(Tier.COMPLEX).model
            usage = getattr(resp, "usage_metadata", None) or {}
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            if not input_tokens and not output_tokens:
                meta = getattr(resp, "response_metadata", None) or {}
                token_usage = meta.get("token_usage") or meta.get("usage") or {}
                input_tokens = int(
                    token_usage.get("prompt_tokens") or token_usage.get("input_tokens") or 0
                )
                output_tokens = int(
                    token_usage.get("completion_tokens")
                    or token_usage.get("output_tokens")
                    or 0
                )
            if input_tokens or output_tokens:
                cost.add_usage(model_name, input_tokens, output_tokens)
            else:
                cost.add_in(prompt, model=model_name)
                cost.add_out(text, model=model_name)
        return text

    route_to_strong_llm.__name__ = "route_to_strong_llm"
    route_to_strong_llm._escalation = True
    return route_to_strong_llm