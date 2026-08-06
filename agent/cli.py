"""Minimal terminal UI: streams the agent's events to the console.

Used by run.bat so you can watch the whole agent work live without a UI.
"""

from __future__ import annotations

import asyncio
import sys


def _echo(ev: dict) -> None:
    t = ev["type"]
    if t == "routed":
        print(f"\n[route] tier={ev['tier']} model={ev['model']} ({ev['reason']})\n")
    elif t == "agent":
        print(f"\n[{ev['name']}] {ev['message']}\n")
    elif t == "token":
        print(ev["content"], end="", flush=True)
    elif t == "status":
        print(f"\n  \u25b8 #{ev['step']} [{ev['agent']}] {ev['text']}")
    elif t == "handoff":
        print(f"\n  >>> delegating to: {ev['to']}")
    elif t == "visit":
        print(f"  \u2b05 visited: {ev['url']}")
    elif t == "collected":
        print(f"  \ud83d\udccb collected {ev['chars']} chars :: {ev['preview']}")
    elif t in ("tool_call", "tool_result"):
        print(f"  [tool] {ev['name']} ok={ev.get('ok')}")
    elif t == "artifacts":
        print(f"\n[files] {ev['files']}")
    elif t == "cost":
        print(
            f"\n\n[cost] in={ev['tokens_in']} out={ev['tokens_out']} "
            f"total={ev['total_tokens']} est=${ev['est_cost_usd']}"
        )
    elif t == "complete":
        print(f"\n\n[done] {ev['final']}\n[artifacts] {ev['artifacts']}")
    elif t == "error":
        print(f"\n[ERROR] {ev['message']}")


async def _run(prompt_input: str, session_id: str) -> None:
    from agent.services.runner import run_agent

    async for ev in run_agent(prompt_input, session_id):
        _echo(ev)


def main() -> None:
    args = sys.argv[1:]
    prompt_input = " ".join(args) if args else input("What should the AI Operations Center do? ")
    session_id = "cli"
    asyncio.run(_run(prompt_input, session_id))


if __name__ == "__main__":
    main()