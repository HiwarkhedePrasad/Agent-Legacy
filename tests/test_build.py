"""Smoke test: verifies the deep agent graph compiles without an API call."""

from agent.core.agent_factory import build_agent
from agent.memory.long_term import LongTermMemory
from agent.memory.memory_tools import build_memory_tools
from agent.tools.registry import build_all_tools


def main() -> None:
    tools = build_all_tools("test-session")
    print(f"[OK] {len(tools)} tools registered:")
    for t in tools:
        print(f"    - {t.name}")

    mem = LongTermMemory("test-session")
    mem.add("Learned: TinyFish is the search API for this project.", importance=5)
    results = mem.search("what search api do we use", top_k=3)
    print(f"[OK] memory store has {len(mem.entries)} entries")
    print(f"[OK] memory retrieval returned {len(results)} result(s)")

    agent = build_agent("test-session")
    print("[OK] deep agent graph compiled (no API call made)")
    print(f"[OK] node names: {[n for n in agent.get_graph().nodes.keys()][:12]}")


if __name__ == "__main__":
    main()
