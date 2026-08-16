"""Factory that builds the nested-orchestrator deep agent.

Uses deepagents.create_deep_agent() which compiles to a LangGraph graph,
so sub-agents and filesystem tools come for free. The model is chosen by
the multi-LLM router based on task complexity.

Default domain: "universal" (Agent-Legacy). Domain packs are drop-in prompt
+ sub-agent sets.
"""

from __future__ import annotations

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

from agent.config import settings
from agent.core.middlewares import build_guardrails, build_usage_middleware
from agent.cost import CostTracker
from agent.modes import get_mode
from agent.prompts.houses import HOUSE_PLANNER_PROMPTS
from agent.prompts.universal_ops import (
    DECISION_SYSTEM_PROMPT,
    EXECUTOR_SYSTEM_PROMPT,
    PLANNER_SYSTEM_PROMPT,
    QA_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
)
from agent.router import Tier, build_chat, get_model_spec
from agent.tools.registry import build_all_tools

_WEB_TOOLS = ("web_search", "fetch_url", "crawl_website", "extract_links")
_MEMORY_TOOLS = ("recall_memory", "remember")


def _web_and_memory_tools(all_tools: list) -> list:
    return [t for t in all_tools if getattr(t, "name", "") in _WEB_TOOLS + _MEMORY_TOOLS]


def build_agent(
    session_id: str = "default",
    tier: Tier | str = Tier.SIMPLE,
    cost: CostTracker | None = None,
    domain: str = "universal",
    checkpointer=None,
):
    """Create the compiled deep agent for the given session and complexity tier.

    checkpointer: an optional LangGraph BaseCheckpointSaver (e.g. a persistent
    SQLite saver). When provided, the graph is resumable — if the step budget
    is exhausted mid-run, the caller can continue from the saved checkpoint
    instead of losing the work.
    """
    all_tools = build_all_tools(session_id, cost=cost)

    main_spec = get_model_spec(tier)
    strong_spec = get_model_spec(Tier.COMPLEX)

    house = get_mode()

    # Real provider usage tracking (works even with cost=None-ish tiers) —
    # one tracker per agent (main + each sub-agent) so sub-agent calls are
    # attributed to the model that actually served them.
    if cost is None:
        cost = CostTracker()
    track_usage = build_usage_middleware(cost)

    subagents = [
        {
            "name": "research",
            "description": "Research agent. Web-searches and deep-crawls sources, gathers cited evidence.",
            "system_prompt": RESEARCH_SYSTEM_PROMPT,
            "tools": _web_and_memory_tools(all_tools),
            "middleware": [track_usage],
        },
        {
            "name": "executor",
            "description": "Tool Execution agent. Uses tools to produce files and gather data on request.",
            "system_prompt": EXECUTOR_SYSTEM_PROMPT,
            "tools": all_tools,
            "middleware": [track_usage],
        },
        {
            "name": "decision",
            "description": "Decision agent. Runs on the strongest model to reason about the hardest choices.",
            "system_prompt": DECISION_SYSTEM_PROMPT,
            "model": build_chat(strong_spec),
            "middleware": [track_usage],
        },
        {
            "name": "qa",
            "description": "QA agent. Independently reviews deliverables and returns a PASS/FAIL verdict with a score out of 10.",
            "system_prompt": QA_SYSTEM_PROMPT,
            "model": build_chat(strong_spec),
            "middleware": [track_usage],
        },
    ]

    # Each house mode gets its own planner persona (not the same prompt with a
    # tacked-on modifier) — see agent/prompts/houses.py.
    house_planner_prompt = HOUSE_PLANNER_PROMPTS.get(house.key, PLANNER_SYSTEM_PROMPT)

    return create_deep_agent(
        model=build_chat(main_spec),
        tools=all_tools,
        system_prompt=house_planner_prompt,
        subagents=subagents,
        middleware=[track_usage, *build_guardrails()],
        backend=FilesystemBackend(root_dir=str(settings.WORKSPACE_DIR)),
        checkpointer=checkpointer,
    )
