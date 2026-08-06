"""Backend runner: executes the agent and yields structured events.

Every event is consumed by a WebSocket relay, terminal UI, or web dashboard.
The stream is intentionally verbose so the demo SHOWS everything happening:

    {"type": "routed",   "tier": ..., "model": ..., "reason": ...}
    {"type": "agent",    "name": ..., "message": ...}          # narration
    {"type": "handoff",  "to": ..., "step": N}                 # subagent starts
    {"type": "status",   "step": N, "text": ..., "agent": ...} # running log line
    {"type": "visit",    "url": ..., "agent": ...}             # navigating to a site
    {"type": "tool_call","name": ..., "args": ..., "agent": ...}
    {"type": "collected","source": ..., "preview": ..., "chars": ..., "agent": ...}
    {"type": "token",    "content": ...}
    {"type": "tool_result","name": ..., "ok": ..., "summary": ..., "agent": ...}
    {"type": "cost",     "tokens_in": ..., "tokens_out": ..., "est_cost_usd": ...}
    {"type": "complete", "final": ..., "artifacts": [...]}
"""

from __future__ import annotations

import re
from typing import AsyncGenerator

from agent.core.agent_factory import build_agent
from agent.cost import CostTracker, estimate_tokens
from agent.memory.long_term import LongTermMemory, summarize_conversation
from agent.router import classify_task

_SUBAGENTS = ("research", "executor", "decision", "qa")
_WEB_TOOLS = ("fetch_url", "crawl_website", "extract_links", "web_search")


def _token_content(event: dict) -> str | None:
    """Extract text from an on_chat_model_stream chunk.

    Some models (e.g. DeepSeek, Qwen) emit their natural-language reasoning in
    `reasoning_content`/`reasoning` rather than `content`. Surface whichever exists.
    """
    try:
        chunk = event.get("data", {}).get("chunk")
        for attr in ("reasoning_content", "reasoning", "content"):
            content = getattr(chunk, attr, None)
            if isinstance(content, str) and content.strip():
                return content
    except Exception:  # noqa: BLE001
        pass
    return None


def _extract_final_text(event: dict) -> str | None:
    """Pull the final assistant text from the graph's on_chain_end output."""
    try:
        output = event.get("data", {}).get("output", {})
        messages = output.get("messages", [])
        if not messages:
            return None
        for message in reversed(messages):
            if getattr(message, "type", "") == "ai" and isinstance(message.content, str):
                return message.content
    except Exception:  # noqa: BLE001
        pass
    return None


def _owner(event: dict) -> str:
    """Figure out which agent (planner/research/executor/decision/qa) owns this event."""
    try:
        metadata = event.get("metadata", {})
        blob = " ".join(
            str(x) for x in (metadata.get("namespace", []), metadata.get("langgraph_node", ""))
        ).lower()
        for name in _SUBAGENTS:
            if name in blob:
                return name
    except Exception:  # noqa: BLE001
        pass
    return "planner"


def _tool_label(name: str, args: dict) -> str:
    """Human-readable, demo-friendly description of a tool invocation."""
    try:
        if name in ("fetch_url", "crawl_website"):
            url = str(args.get("url") or args.get("start_url") or "")
            return f"Opening website {url}" if url else "Fetching a page"
        if name == "extract_links":
            url = str(args.get("url", ""))
            return f"Extracting links from {url}"
        if name == "web_search":
            q = str(args.get("query", ""))
            return f'Searching the web for "{q}"'
        if name == "write_file":
            p = str(args.get("path", ""))
            return f"Writing deliverable to {p}"
        if name == "route_to_strong_llm":
            return "Escalating this step to the strongest LLM"
        if name == "remember":
            return "Remembering this for later"
        if name == "recall_memory":
            return "Recalling past memories"
        return name.replace("_", " ").title()
    except Exception:  # noqa: BLE001
        return name


def _text_of(output) -> str:
    """Coerce a tool output (string or CrawlResult-like object) to text."""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    for attr in ("content", "markdown", "text", "data"):
        val = getattr(output, attr, None)
        if isinstance(val, str) and val.strip():
            return val
    return str(output)


def _preview(text: str, limit: int = 300) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


async def run_agent(
    user_input: str,
    session_id: str = "default",
) -> AsyncGenerator[dict, None]:
    tier, reason = await classify_task(user_input)
    from agent.router import get_model_spec

    routed_model = get_model_spec(tier).model
    yield {
        "type": "routed",
        "tier": tier.value,
        "model": routed_model,
        "reason": reason,
    }

    cost = CostTracker()
    agent = build_agent(session_id, tier=tier, cost=cost)
    memory = LongTermMemory(session_id)

    try:
        prev_summary = memory.to_summary(user_input, top_k=3)
    except Exception:  # noqa: BLE001
        prev_summary = ""

    memory_context = ""
    if prev_summary and "No relevant memories" not in prev_summary:
        memory_context = f"Relevant past memories:\n{prev_summary}\n\n"

    system_text = (
        memory_context + "Solve the user's request using the whole team."
    )
    cost.add_in(system_text, model=routed_model)
    cost.add_in(user_input, model=routed_model)

    input_payload = {
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_input},
        ]
    }

    artifacts: list[str] = []
    final_text = ""
    step = 0

    def next_step(text: str, agent: str) -> dict:
        nonlocal step
        step += 1
        return {"type": "status", "step": step, "text": text, "agent": agent}

    yield {"type": "agent", "name": "planner", "message": "Planning the approach..."}

    try:
        async for event in agent.astream_events(input_payload, version="v2"):
            event_type = event["event"]

            if event_type == "on_chat_model_stream":
                content = _token_content(event)
                if content:
                    cost.add_out(content, model=routed_model)
                    yield {"type": "token", "content": content}

            elif event_type == "on_tool_start":
                name = event.get("name", "unknown")
                args = event.get("data", {}).get("input", {})
                if not isinstance(args, dict):
                    args = {}
                agent = _owner(event)
                cost.add_in(str(args), model=routed_model)

                label = _tool_label(name, args)
                if agent == "planner":
                    yield next_step(label, agent)
                else:
                    yield next_step(f"{agent.capitalize()} {label.lower()}", agent)

                if name == "write_file":
                    path = str(args.get("path", ""))
                    if path:
                        artifacts.append(path)
                yield {"type": "tool_call", "name": name, "args": args, "agent": agent}

                if name in ("fetch_url", "crawl_website", "extract_links"):
                    url = str(args.get("url") or args.get("start_url") or "")
                    if url:
                        yield {"type": "visit", "url": url, "agent": agent}

            elif event_type == "on_tool_end":
                name = event.get("name", "unknown")
                output = event.get("data", {}).get("output")
                agent = _owner(event)
                cost.add_in(str(output), model=routed_model)

                ok = not (
                    isinstance(output, str)
                    and output.strip().lower().startswith("error")
                )
                text = _text_of(output)

                if name in _WEB_TOOLS and ok and text.strip():
                    chars = len(text)
                    preview = _preview(text)
                    yield {
                        "type": "collected",
                        "source": name,
                        "preview": preview,
                        "chars": chars,
                        "agent": agent,
                    }

                summary = _preview(text, limit=400)
                if name == "write_file":
                    summary = "File written: " + str(
                        (output if isinstance(output, str) else "")
                    )
                if not ok:
                    yield next_step(
                        f"{_tool_label(name, {})} failed — the agent will retry from a different angle.",
                        agent,
                    )
                yield {
                    "type": "tool_result",
                    "name": name,
                    "ok": ok,
                    "summary": summary or "(no text output)",
                    "agent": agent,
                }

            elif event_type == "on_chain_start":
                chain_name = event.get("name", "")
                for sub in _SUBAGENTS:
                    if chain_name == sub or (sub in str(event.get("metadata", {}).get("namespace", ""))):
                        yield {
                            "type": "handoff",
                            "to": sub,
                            "step": step,
                        }
                        if sub == "decision":
                            msg = "Decision agent reasoning over the options..."
                        elif sub == "qa":
                            msg = "QA reviewing the deliverable before shipping..."
                        else:
                            msg = f"{sub.capitalize()} agent is now working..."
                        yield next_step(msg, sub)

            elif event_type == "on_chain_end":
                text = _extract_final_text(event)
                if text:
                    final_text = text

        if artifacts:
            yield {"type": "artifacts", "files": list(dict.fromkeys(artifacts))}

    except Exception as exc:  # noqa: BLE001
        yield {"type": "error", "message": str(exc)}
        final_text = f"Agent run failed: {exc}"

    summary = summarize_conversation(user_input, final_text or "", artifacts)
    try:
        memory.add(summary, memory_type="episodic", importance=3)
    except Exception:  # noqa: BLE001
        pass

    yield {
        "type": "cost",
        **cost.report(model=routed_model),
    }
    yield {
        "type": "complete",
        "final": final_text,
        "artifacts": list(dict.fromkeys(artifacts)),
    }
