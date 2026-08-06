"""Memory-backed tools the agent can call itself.

- `recall_memory`  → search long-term memory for the best past solutions
- `remember`       → write back a learning after completing a task
"""

from __future__ import annotations

from langchain_core.tools import tool

from agent.memory.long_term import LongTermMemory


def build_memory_tools(session_id: str) -> list:
    memory = LongTermMemory(session_id)

    @tool
    def recall_memory(query: str, top_k: int = 5) -> str:
        """Search long-term memory for the most relevant past tasks, solutions,
        or lessons learned. Use this when you need prior knowledge to solve the
        current task faster or better."""
        return memory.to_summary(query, top_k)

    @tool
    def remember(content: str, memory_type: str = "episodic", importance: int = 3) -> str:
        """Persist a key learning, fact, task outcome, or solution to long-term
        memory so future tasks can retrieve it. Call this after completing a
        meaningful unit of work."""
        memory.add(content, memory_type=memory_type, importance=importance, source="agent")
        return "Saved to long-term memory."

    return [recall_memory, remember]