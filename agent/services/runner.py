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
import os
import re
import uuid
from typing import AsyncGenerator

from agent.config import settings
from agent.core.agent_factory import build_agent
from agent.core.middlewares import (
    MAX_CONTINUATIONS,
    RECURSION_LIMIT,
    RUN_RETRIES,
    RUN_RETRY_WAIT,
    is_transient_error,
)
from agent.cost import CostTracker
from agent.memory.long_term import LongTermMemory, summarize_conversation
from agent.modes import get_mode
from agent.router import classify_task

_SUBAGENTS = ("research", "executor", "decision", "qa")
_WEB_TOOLS = (
    "fetch_url", "crawl_website", "extract_links", "web_search",
    "fetch_pdf", "call_api",
)

# Resilience: how long to wait for the next agent event before treating the run
# as stuck and recovering (see _timed / _RunTimeout below).
EVENT_TIMEOUT = 300.0

# Keep the checkpoint DB from growing forever: after each run, prune all but
# the most recent CHECKPOINT_KEEP_THREADS task threads (one thread per task).
CHECKPOINT_KEEP_THREADS = int(os.getenv("CHECKPOINT_KEEP_THREADS", "25"))

try:
    from langgraph.errors import GraphRecursionError
except Exception:  # noqa: BLE001

    class GraphRecursionError(Exception):  # fallback shim for older langgraph
        pass


async def _prune_checkpoints(conn, keep: int = CHECKPOINT_KEEP_THREADS) -> None:
    """Delete all but the `keep` most-recent task threads from the checkpoint
    DB so it doesn't grow unbounded (each task mints a fresh thread_id).
    checkpoint_id is a time-sortable uuid6, so MAX(checkpoint_id) orders
    threads by last activity. Best-effort: failures never break a run."""
    try:
        cursor = await conn.execute(
            "SELECT thread_id, MAX(checkpoint_id) AS latest FROM checkpoints "
            "GROUP BY thread_id ORDER BY latest DESC LIMIT -1 OFFSET ?",
            (keep,),
        )
        stale = [row[0] for row in await cursor.fetchall()]
        if not stale:
            return
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            for thread_id in stale:
                await conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,)
                )
        await conn.commit()
    except Exception:  # noqa: BLE001
        pass


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
        if name == "fetch_pdf":
            url = str(args.get("url", ""))
            return f"Reading PDF document {url}" if url else "Reading a PDF document"
        if name == "call_api":
            url = str(args.get("url", ""))
            return f"Calling API endpoint {url}" if url else "Calling an API"
        if name == "calculate":
            expr = str(args.get("expression", ""))
            return f"Calculating {expr}" if expr else "Calculating"
        if name == "get_datetime":
            return "Checking the current date and time"
        if name == "web_search":
            q = str(args.get("query", ""))
            return f'Searching the web for "{q}"'
        if name == "write_file":
            p = str(args.get("file_path") or args.get("path") or "")
            return f"Writing deliverable to {p}" if p else "Writing a deliverable file"
        if name == "edit_file":
            p = str(args.get("file_path") or args.get("path") or "")
            return f"Editing {p} (targeted change, no full rewrite)" if p else "Making a targeted file edit"
        if name == "read_file":
            p = str(args.get("file_path") or args.get("path") or "")
            return f"Reading {p}" if p else "Reading a file"
        if name in ("grep", "glob"):
            return "Searching the codebase"
        if name == "ls":
            return "Listing files"
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


async def _forced_qa_review(task: str, final: str, artifacts: list[str]) -> dict:
    """Mechanical QA verdict for Hufflepuff mode (strong model, no tools)."""
    from agent.router import Tier, build_chat, get_model_spec

    chat = build_chat(get_model_spec(Tier.COMPLEX))
    try:
        resp = await chat.ainvoke(
            [
                {"role": "system", "content": (
                    "You are the QA agent. Verdict only: reply with PASS or FAIL, "
                    "then one short sentence of reasoning, then a score N/10."
                )},
                {"role": "user", "content": (
                    f"Task: {task[:600]}\n\nDeliverables: {', '.join(artifacts)}\n\n"
                    f"Summary of final output:\n{final[:1500]}"
                )},
            ]
        )
        text = resp.content if isinstance(resp.content, str) else str(resp.content)
    except Exception as exc:  # noqa: BLE001
        return {"pass": True, "summary": f"QA skipped (review model error: {_preview(str(exc), 80)})"}
    return {"pass": not text.strip().upper().startswith("FAIL"), "summary": _preview(text, 200)}


async def run_agent(
    user_input: str,
    session_id: str = "default",
) -> AsyncGenerator[dict, None]:
    house = get_mode()
    tier, reason = await classify_task(user_input, force_llm=house.force_llm_routing)
    from agent.router import get_model_spec

    routed_model = get_model_spec(tier).model

    # House-mode resilience tuning: Gryffindor recovers faster, Hufflepuff
    # retries harder. Baseline constants come from agent.core.middlewares.
    run_retries = RUN_RETRIES + house.extra_run_retries
    retry_wait = house.retry_wait if house.retry_wait else RUN_RETRY_WAIT
    yield {
        "type": "routed",
        "tier": tier.value,
        "model": routed_model,
        "reason": reason,
        "mode": house.key,
        "mode_advantage": house.advantage,
    }

    cost = CostTracker()

    # Permanent recursion fix: the run is persisted to SQLite under a per-task
    # thread_id. If the step budget is exhausted, the run RESUMES from the saved
    # checkpoint with a fresh budget instead of losing the work. Each task gets
    # its own thread so checkpoint state never bleeds across unrelated runs.
    thread_id = f"{session_id}-{uuid.uuid4().hex[:8]}"
    continuations = 0
    _conn = None
    saver = None
    try:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        _conn = await aiosqlite.connect(str(settings.CHECKPOINT_DB))
        saver = AsyncSqliteSaver(_conn)
        await saver.setup()
    except Exception:  # noqa: BLE001
        # No persistence available — the run still works, it just can't resume.
        if _conn is not None:
            try:
                await _conn.close()
            except Exception:  # noqa: BLE001
                pass
            _conn = None

    agent = build_agent(session_id, tier=tier, cost=cost, checkpointer=saver)
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
    budget_exhausted = False
    payload = input_payload
    while True:
        attempt += 1
        try:
            async for event in _timed(
                agent.astream_events(
                    payload,
                    version="v2",
                    config={
                        "recursion_limit": RECURSION_LIMIT,
                        "configurable": {"thread_id": thread_id},
                    },
                )
            ):
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

        except GraphRecursionError:
            # The step budget ran out. With a persistent checkpointer this is
            # not a failure — it's a natural pause: resume from the saved
            # checkpoint with a fresh budget. Each task gets up to
            # MAX_CONTINUATIONS resumes (a genuine ceiling, not one big number).
            continuations += 1
            budget_exhausted = True
            if saver is not None and continuations <= MAX_CONTINUATIONS:
                yield {
                    "type": "retry",
                    "attempt": continuations,
                    "wait": 0,
                    "reason": (
                        f"step budget of {RECURSION_LIMIT} reached — "
                        "resuming from the saved checkpoint"
                    ),
                }
                yield next_step(
                    f"Step budget reached — resuming from the checkpoint "
                    f"({continuations}/{MAX_CONTINUATIONS})...",
                    "planner",
                )
                payload = None  # langgraph resumes from the checkpoint when input is None
                continue
            yield {
                "type": "warning",
                "message": (
                    f"Step budget of {RECURSION_LIMIT} reached "
                    f"({continuations} resumes) — the team stopped here and "
                    "is delivering what's already done."
                ),
            }
            yield next_step("Step budget exhausted — shipping what's complete.", "planner")
            if not final_text:
                final_text = (
                    "The team hit its step budget before finishing everything, so here is "
                    "everything completed so far. Run the follow-up for the remaining work."
                )
            break

        except _RunTimeout:
            # A stall is treated like a transient failure: recover and retry.
            if attempt <= run_retries:
                wait = retry_wait * attempt
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
            if is_transient_error(exc) and attempt <= run_retries:
                wait = retry_wait * attempt
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

    # Prune stale task threads, then close the persistent checkpoint
    # connection (the stream is done using it).
    if _conn is not None:
        await _prune_checkpoints(_conn)
        try:
            await _conn.close()
        except Exception:  # noqa: BLE001
            pass

    # Hufflepuff house mode: deliverables MUST get an independent review. If
    # the planner skipped QA, run a mechanical one-shot review on the strong
    # model here instead of shipping unreviewed work.
    if house.force_qa_review and artifacts and not qa_verified and not failed:
        yield {"type": "handoff", "to": "qa", "step": step}
        yield next_step("Hufflepuff mode: forcing an independent QA review...", "qa")
        verdict = await _forced_qa_review(user_input, final_text, artifacts)
        qa_verified = True
        yield {
            "type": "tool_result",
            "name": "qa",
            "ok": verdict["pass"],
            "summary": verdict["summary"],
            "agent": "qa",
        }
        if not verdict["pass"]:
            yield {
                "type": "warning",
                "message": "Forced QA review came back FAIL — check the feedback above before trusting this deliverable.",
            }

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
        "budget_exhausted": budget_exhausted,
    }
