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
        src = "real" if ev.get("real_usage") else "est"
        print(
            f"\n\n[cost/{src}] in={ev['tokens_in']} out={ev['tokens_out']} "
            f"total={ev['total_tokens']} est=${ev['est_cost_usd']}"
        )
    elif t == "complete":
        print(f"\n\n[done] {ev['final']}\n[artifacts] {ev['artifacts']}")
    elif t == "error":
        print(f"\n[ERROR] {ev['message']}")
    elif t == "warning":
        print(f"\n[WARN] {ev['message']}")
    elif t == "retry":
        print(
            f"\n[retry] attempt {ev['attempt']} — waiting {int(ev['wait'])}s "
            f"({ev.get('reason', 'recovering')})"
        )


async def _run(prompt_input: str, session_id: str) -> None:
    from agent.services.runner import run_agent

    async for ev in run_agent(prompt_input, session_id):
        _echo(ev)


def main() -> None:
    args = sys.argv[1:]
    prompt_input = " ".join(args) if args else input("What should Agent-Legacy do? ")

    if prompt_input.startswith("/"):
        from agent.commands import COMMANDS, help_lines

        cmd = prompt_input.split()[0].lower()
        if cmd in ("/help", "/?"):
            for line in help_lines():
                print(line)
        elif cmd in ("/exit", "/quit", "/q"):
            return
        else:
            known = [c.split()[0] for c in COMMANDS]
            hint = "(known: " + ", ".join(known) + ")" if cmd not in known else "(use the dashboard for live commands)"
            print(f"command {cmd} handled by the dashboard {hint}")
        return

    session_id = "cli"
    asyncio.run(_run(prompt_input, session_id))


if __name__ == "__main__":
    main()