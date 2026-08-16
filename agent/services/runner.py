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
    {"type": "retry",    "attempt": N, "wait": S, "reason": ...}  # recovering
    {"type": "cost",     "tokens_in": ..., "tokens_out": ..., "est_cost_usd": ...}
    {"type": "complete", "final": ..., "artifacts": [...]}
"""

from __future__ import annotations

import asyncio
import re
from typing import AsyncGenerator

from agent.config import settings
from agent.core.agent_factory import build_agent
from agent.core.middlewares import (
    RUN_RETRIES,
    RUN_RETRY_WAIT,
    is_transient_error,
)
from agent.cost import CostTracker
from agent.memory.long_term import LongTermMemory, summarize_conversation
from agent.router import classify_task

_SUBAGENTS = ("research", "executor", "decision", "qa")
_WEB_TOOLS = ("fetch_url", "crawl_website", "extract_links", "web_search")

# Resilience: how long to wait for the next agent event before treating the run
# as stuck and recovering (see _timed / _RunTimeout below).
EVENT_TIMEOUT = 300.0


class _RunTimeout(Exception):
    """Raised when no agent event arrives for EVENT_TIMEOUT seconds (recoverable)."""


async def _timed(agen, timeout: float = EVENT_TIMEOUT):
    """Yield every item from `agen`, but raise _RunTimeout if a single item takes
    longer than `timeout` to arrive. Prevents a hung model/network call from
    freezing the whole run forever; the caller turns it into a safe recovery."""
    it = agen.__aiter__()
    while True:
        try:
            yield await asyncio.wait_for(it.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            raise _RunTimeout() from None


def _token_content(event: dict) -> str | None:
    """Extract text from an on_chat_model_stream chunk.

    Some models (e.g. DeepSeek, Qwen) emit their natural-language reasoning in
    `reasoning_content`/`reasoning` rather than `content`. Only surface
    reasoning when SHOW_REASONING is on (off by default) — it's verbose.
    """
    try:
        chunk = event.get("data", {}).get("chunk")
        attrs: tuple[str, ...] = ("content",)
        if settings.SHOW_REASONING:
            attrs = ("reasoning_content", "reasoning", "content")
        for attr in attrs:
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
            p = str(args.get("file_path") or args.get("path") or "")
            return f"Writing deliverable to {p}" if p else "Writing a deliverable file"
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
        memory_context = f"[Relevant past memories]\n{prev_summary}\n\n"
    user_text = memory_context + (
        "[Task] Solve the request below using the whole team.\n\n" + user_input
    )
    # Fallback estimate only — real provider usage is recorded by the
    # UsageTracker middleware when available and takes precedence in reports.
    cost.add_in(user_text, model=routed_model)

    # NOTE: only a user message here — deepagents injects its own system prompt,
    # and some providers (TokenRouter/Qwen) reject system messages that are not
    # the very first message in the list.
    input_payload = {"messages": [{"role": "user", "content": user_text}]}

    artifacts: list[str] = []
    final_text = ""
    step = 0
    failed = False
    qa_verified = False

    def next_step(text: str, agent_name: str) -> dict:
        nonlocal step
        step += 1
        return {"type": "status", "step": step, "text": text, "agent": agent_name}

    def _is_tool_error(output) -> bool:
        if not isinstance(output, str):
            return False
        low = output.strip().lower()
        return low.startswith(("error", "failed to fetch", "failed to", "crawl of"))

    yield {"type": "agent", "name": "planner", "message": "Planning the approach..."}

    # Run-level resilience: if the whole agent stream dies on a transient error
    # (rate limit, overload, network, stall), wait and retry the run instead of
    # terminating. Individual model calls ALSO self-retry inside the middleware
    # (RateLimit + backoff) and are throttled to stay under the provider's
    # per-minute quota — this loop is the last line of defence.
    attempt = 0
    while True:
        attempt += 1
        try:
            async for event in _timed(agent.astream_events(input_payload, version="v2")):
                event_type = event["event"]

                if event_type == "on_chat_model_stream":
                    content = _token_content(event)
                    if content:
                        yield {"type": "token", "content": content}

                elif event_type == "on_tool_start":
                    name = event.get("name", "unknown")
                    args = event.get("data", {}).get("input", {})
                    if not isinstance(args, dict):
                        args = {}
                    agent_name = _owner(event)

                    label = _tool_label(name, args)
                    if agent_name == "planner":
                        yield next_step(label, agent_name)
                    else:
                        yield next_step(f"{agent_name.capitalize()} {label.lower()}", agent_name)

                    if name == "write_file":
                        path = str(args.get("file_path") or args.get("path") or "")
                        if path:
                            artifacts.append(path)
                    yield {"type": "tool_call", "name": name, "args": args, "agent": agent_name}

                    if name in ("fetch_url", "crawl_website", "extract_links"):
                        url = str(args.get("url") or args.get("start_url") or "")
                        if url:
                            yield {"type": "visit", "url": url, "agent": agent_name}

                elif event_type == "on_tool_end":
                    name = event.get("name", "unknown")
                    output = event.get("data", {}).get("output")
                    agent_name = _owner(event)

                    ok = not _is_tool_error(output)
                    text = _text_of(output)

                    if name in _WEB_TOOLS and ok and text.strip():
                        chars = len(text)
                        preview = _preview(text)
                        yield {
                            "type": "collected",
                            "source": name,
                            "preview": preview,
                            "chars": chars,
                            "agent": agent_name,
                        }

                    summary = _preview(text, limit=400)
                    if name == "write_file":
                        summary = "File written: " + str(
                            (output if isinstance(output, str) else "")
                        )
                    if not ok:
                        yield next_step(
                            f"{_tool_label(name, {})} failed — the agent will retry from a different angle.",
                            agent_name,
                        )
                    yield {
                        "type": "tool_result",
                        "name": name,
                        "ok": ok,
                        "summary": summary or "(no text output)",
                        "agent": agent_name,
                    }

                elif event_type == "on_chain_start":
                    chain_name = event.get("name", "")
                    for sub in _SUBAGENTS:
                        if chain_name == sub or (sub in str(event.get("metadata", {}).get("namespace", ""))):
                            if sub == "qa":
                                qa_verified = True
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

            # Stream completed cleanly.
            break

        except _RunTimeout:
            # A stall is treated like a transient failure: recover and retry.
            if attempt <= RUN_RETRIES:
                wait = RUN_RETRY_WAIT * attempt
                yield {
                    "type": "retry",
                    "attempt": attempt,
                    "wait": wait,
                    "reason": "agent went quiet — recovering, not stopping",
                }
                yield next_step(f"Recovered from a stall — retrying in {int(wait)}s...", "planner")
                await asyncio.sleep(wait)
                continue
            failed = True
            yield {
                "type": "error",
                "message": "The agent went quiet (no activity for a while) — recovering and stopping safely instead of hanging.",
            }
            final_text = "Agent went quiet; the run was stopped safely after a timeout to avoid hanging."
            break

        except Exception as exc:  # noqa: BLE001
            if is_transient_error(exc) and attempt <= RUN_RETRIES:
                wait = RUN_RETRY_WAIT * attempt
                yield {
                    "type": "retry",
                    "attempt": attempt,
                    "wait": wait,
                    "reason": _preview(str(exc), limit=200),
                }
                yield next_step(
                    f"Hit a temporary provider problem (rate limit / overload) — retrying in {int(wait)}s...",
                    "planner",
                )
                await asyncio.sleep(wait)
                continue
            failed = True
            yield {"type": "error", "message": str(exc)}
            final_text = f"Agent run failed: {exc}"
            break

    if artifacts and not qa_verified:
        yield {
            "type": "warning",
            "message": "Deliverable written without an independent QA review.",
        }

    if artifacts:
        yield {"type": "artifacts", "files": list(dict.fromkeys(artifacts))}

    # Failed runs still get a low-importance failure note (so the same question
    # isn't blindly retried with the same broken context), but the raw exception
    # is never stored as a successful RESULT.
    if failed:
        summary = f"TASK: {user_input[:300]}\nRESULT: FAILED — did not complete successfully."
        try:
            memory.add(summary, memory_type="episodic", importance=1)
        except Exception:  # noqa: BLE001
            pass
    else:
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
        "qa_verified": qa_verified,
    }
