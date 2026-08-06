"""Factory that builds the nested-orchestrator deep agent.

Uses deepagents.create_deep_agent() which compiles to a LangGraph graph,
so sub-agents and filesystem tools come for free. The model is chosen by
the multi-LLM router based on task complexity.

Default domain: "universal" (PROMPT-A-THON #10 — Universal AI Operations
Center). Domain packs are drop-in prompt + sub-agent sets.
"""

from __future__ import annotations

from deepagents import create_deep_agent
from deepagents.backends import FilesystemBackend

from agent.config import settings
from agent.cost import CostTracker
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
):
    """Create the compiled deep agent for the given session and complexity tier."""
    all_tools = build_all_tools(session_id, cost=cost)

    main_spec = get_model_spec(tier)
    strong_spec = get_model_spec(Tier.COMPLEX)

    subagents = [
        {
            "name": "research",
            "description": "Research agent. Web-searches and deep-crawls sources, gathers cited evidence.",
            "system_prompt": RESEARCH_SYSTEM_PROMPT,
            "tools": _web_and_memory_tools(all_tools),
        },
        {
            "name": "executor",
            "description": "Tool Execution agent. Uses tools to produce files and gather data on request.",
            "system_prompt": EXECUTOR_SYSTEM_PROMPT,
            "tools": all_tools,
        },
        {
            "name": "decision",
            "description": "Decision agent. Runs on the strongest model to reason about the hardest choices.",
            "system_prompt": DECISION_SYSTEM_PROMPT,
            "model": build_chat(strong_spec),
        },
        {
            "name": "qa",
            "description": "QA agent. Independently reviews deliverables and returns a PASS/FAIL verdict with a score out of 10.",
            "system_prompt": QA_SYSTEM_PROMPT,
            "model": build_chat(strong_spec),
        },
    ]

    return create_deep_agent(
        model=build_chat(main_spec),
        tools=all_tools,
        system_prompt=PLANNER_SYSTEM_PROMPT,
        subagents=subagents,
        backend=FilesystemBackend(root_dir=str(settings.WORKSPACE_DIR)),
    )
