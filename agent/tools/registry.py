"""Aggregates every tool available to the agent for a given session."""

from __future__ import annotations

from agent.cost import CostTracker


def build_all_tools(session_id: str, cost: CostTracker | None = None) -> list:
    from agent.memory.memory_tools import build_memory_tools
    from agent.tools.crawl import build_crawl_tools
    from agent.tools.route_llm import build_escalation_tool
    from agent.tools.web_search import web_search

    return [
        *build_crawl_tools(session_id),
        web_search,
        *build_memory_tools(session_id),
        build_escalation_tool(cost),
    ]