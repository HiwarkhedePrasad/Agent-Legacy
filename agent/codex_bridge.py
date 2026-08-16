"""Local bridge so the OpenAI Codex CLI can talk to TokenRouter.

Since codex-cli 0.84+, custom providers MUST use the OpenAI Responses API
(`wire_api = "responses"`); chat/completions support was hard-removed. TokenRouter
(and OpenRouter/most gateways) still speak Chat Completions, so this bridge:

  1. accepts Responses API requests on `POST /v1/responses`
  2. translates them to `POST /v1/chat/completions`
  3. translates the streamed chat chunks back into Responses SSE events

Run:      python -m agent.codex_bridge
Then:     codex --profile tokenrouter
Wrapper:  codex.bat (starts the bridge, runs codex, stops the bridge)

The upstream URL/key come from the project `.env` (BASE_URL / API_KEY / MODEL),
or from BASE_URL_OVERRIDE / the incoming Bearer token.
"""

from __future__ import annotations

import json
import os
import time
import uuid

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

load_dotenv()

UPSTREAM_BASE = os.getenv("BASE_URL", "https://api.tokenrouter.com/v1").rstrip("/")
UPSTREAM_KEY = os.getenv("API_KEY") or os.getenv("TOKENROUTER_API_KEY") or ""
DEFAULT_MODEL = os.getenv("MODEL", "qwen/qwen3.8-max-free")
PORT = int(os.getenv("CODEX_BRIDGE_PORT", "8787"))

app = FastAPI(title="Agent-Legacy Codex bridge")

# Codex often chains turns via `previous_response_id` (stateful Responses API).
# The upstream chat API is stateless, so we keep the last few conversations'
# message lists and prepend them when Codex references an earlier response.
_CONV_CACHE: dict[str, list[dict]] = {}
_CONV_ORDER: list[str] = []


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _auth_headers(request: Request) -> dict:
    auth = request.headers.get("authorization")
    key = (auth[len("Bearer "):] if auth and auth.lower().startswith("bearer ") else "") or UPSTREAM_KEY
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def _translate_input(input_items, instructions: str | None) -> list[dict]:
    messages: list[dict] = []
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(input_items, str):
        if input_items.strip():
            messages.append({"role": "user", "content": input_items})
        return messages
    for item in input_items or []:
        t = item.get("type")
        if t == "message":
            text = []
            content = item.get("content")
            if isinstance(content, str):
                text.append(content)
            else:
                for part in content or []:
                    if isinstance(part, str):
                        text.append(part)
                    elif isinstance(part, dict) and part.get("text"):
                        if part.get("type") in ("input_text", "text", "output_text"):
                            text.append(part["text"])
            joined = "\n".join(text).strip()
            if joined:
                role = item.get("role", "user")
                # Codex sends memory/extra guidance as `developer`; many
                # OpenAI-compatible upstreams (incl. Qwen via TokenRouter) only
                # understand `system`. Map it, and we merge all system messages
                # into one below.
                if role == "developer":
                    role = "system"
                messages.append({"role": role, "content": joined})
        elif t == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": item.get("call_id") or f"call_{uuid.uuid4().hex[:8]}",
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments") or "",
                            },
                        }
                    ],
                }
            )
        elif t in ("function_call_output", "custom_tool_call_output"):
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output)
            messages.append(
                {"role": "tool", "tool_call_id": item.get("call_id", ""), "content": output}
            )
        else:
            continue  # reasoning/web_search/etc. items don't map to chat completions
    return messages


def _translate_tools(tools: list | None) -> list[dict]:
    chat_tools = []
    for tool in tools or []:
        if tool.get("type") == "function" and tool.get("name"):
            fn = {"name": tool["name"]}
            if tool.get("description"):
                fn["description"] = tool["description"]
            if tool.get("parameters"):
                fn["parameters"] = tool["parameters"]
            chat_tools.append({"type": "function", "function": fn})
    return chat_tools


def _chat_payload(body: dict, stream: bool) -> dict:
    model = body.get("model") or DEFAULT_MODEL
    messages = []
    prev = body.get("previous_response_id")
    if prev and prev in _CONV_CACHE:
        messages.extend(_CONV_CACHE[prev])
    messages.extend(_translate_input(body.get("input"), body.get("instructions")))
    # TokenRouter + Qwen rejects the "developer" role that Codex uses for
    # memory/extra guidance, and only accepts ONE system message. Merge all
    # system-level messages into a single system prompt.
    sys_msgs = [m for m in messages if m["role"] == "system"]
    other_msgs = [m for m in messages if m["role"] != "system"]
    if sys_msgs:
        merged_system = {"role": "system", "content": "\n\n".join(m["content"] for m in sys_msgs)}
        messages = [merged_system] + other_msgs
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if os.getenv("CODEX_BRIDGE_DEBUG"):
        try:
            skeleton = {
                "model": model,
                "stream": stream,
                "prev_id": prev,
                "input_item_types": [(i.get("type"), i.get("role")) for i in (body.get("input") or []) if isinstance(i, dict)],
                "messages": [
                    {
                        "role": m.get("role"),
                        "len": len(m.get("content") or ""),
                        "has_tool_calls": bool(m.get("tool_calls")),
                        "tool_call_id": m.get("tool_call_id"),
                        "preview": (m.get("content") or "")[:160] if isinstance(m.get("content"), str) else None,
                    }
                    for m in messages
                ],
            }
            with open(os.path.join(os.path.dirname(__file__), "..", "codex_bridge_debug.log"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "payload_skeleton", **skeleton}, default=str)[:6000] + "\n\n")
        except Exception:  # noqa: BLE001
            pass
    tools = _translate_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
        if body.get("tool_choice") == "auto":
            payload["tool_choice"] = "auto"
    if isinstance(body.get("temperature"), (int, float)):
        payload["temperature"] = body["temperature"]
    if body.get("max_output_tokens"):
        payload["max_tokens"] = body["max_output_tokens"]
    if stream:
        payload["stream_options"] = {"include_usage": True}
    return payload


def _remember(resp_id: str, messages: list[dict]) -> None:
    _CONV_CACHE[resp_id] = messages
    _CONV_ORDER.append(resp_id)
    while len(_CONV_ORDER) > 8:
        _CONV_CACHE.pop(_CONV_ORDER.pop(0), None)


def _response_shell(model: str, status: str = "in_progress") -> dict:
    return {
        "id": f"resp_{uuid.uuid4().hex}",
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
    }


async def _stream_to_responses(upstream: httpx.AsyncClient, url: str, headers: dict, payload: dict, model: str):
    resp = _response_shell(model)
    output = resp["output"]

    msg_id = f"msg_{uuid.uuid4().hex}"
    msg_open = False
    text_buf: list[str] = []
    fc: dict[int, dict] = {}

    def open_message():
        nonlocal msg_open
        if not msg_open:
            yield _sse("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": len(output),
                "item": {"type": "message", "id": msg_id, "role": "assistant", "status": "in_progress", "content": []},
            })
            yield _sse("response.content_part.added", {
                "type": "response.content_part.added",
                "item_id": msg_id, "output_index": len(output), "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            msg_open = True

    yield _sse("response.created", {"type": "response.created", "response": {**resp, "status": "in_progress"}})
    yield _sse("response.in_progress", {"type": "response.in_progress", "response": resp})

    async with upstream.stream("POST", url, json=payload, headers=headers, timeout=600.0) as r:
        if r.status_code >= 400:
            err_text = (await r.aread()).decode(errors="replace")
            if os.getenv("CODEX_BRIDGE_DEBUG"):
                try:
                    with open(os.path.join(os.path.dirname(__file__), "..", "codex_bridge_debug.log"), "a", encoding="utf-8") as f:
                        f.write(json.dumps({"type": "upstream_error", "status": r.status_code, "error": err_text[:1500], "payload": payload}, default=str)[:4000] + "\n\n")
                except Exception:  # noqa: BLE001
                    pass
            yield _sse("error", {"type": "error", "error": {"code": str(r.status_code), "message": err_text[:600]}})
            yield _sse("response.failed", {
                "type": "response.failed",
                "response": {**resp, "status": "failed"},
            })
            return
        buffer = ""
        async for chunk in r.aiter_text():
            buffer += chunk
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    continue
                try:
                    evt = json.loads(data)
                except json.JSONDecodeError:
                    continue

                usage = evt.get("usage")
                if usage:
                    resp["usage"] = {
                        "input_tokens": usage.get("prompt_tokens", 0),
                        "output_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    }
                for choice in evt.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content")
                    if text:
                        for e in open_message():
                            yield e
                        text_buf.append(text)
                        yield _sse("response.output_text.delta", {
                            "type": "response.output_text.delta",
                            "item_id": msg_id, "output_index": len(output),
                            "content_index": 0, "delta": text,
                        })
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = fc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

    if text_buf:
        full_text = "".join(text_buf)
        message_item = {
            "type": "message", "id": msg_id, "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": full_text, "annotations": []}],
        }
        output.append(message_item)
        yield _sse("response.output_text.done", {
            "type": "response.output_text.done",
            "item_id": msg_id, "output_index": len(output) - 1, "content_index": 0, "text": full_text,
        })
        yield _sse("response.content_part.done", {
            "type": "response.content_part.done",
            "item_id": msg_id, "output_index": len(output) - 1, "content_index": 0,
            "part": message_item["content"][0],
        })
        yield _sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": len(output) - 1, "item": message_item,
        })

    for idx in sorted(fc):
        slot = fc[idx]
        item = {
            "type": "function_call",
            "id": f"fc_{idx}_{uuid.uuid4().hex[:8]}",
            "call_id": slot["id"] or f"call_{uuid.uuid4().hex[:8]}",
            "name": slot["name"],
            "arguments": slot["arguments"],
            "status": "completed",
        }
        output.append(item)
        yield _sse("response.output_item.added", {
            "type": "response.output_item.added",
            "output_index": len(output) - 1, "item": {**item, "status": "in_progress"},
        })
        yield _sse("response.output_item.done", {
            "type": "response.output_item.done",
            "output_index": len(output) - 1, "item": item,
        })

    resp["output"] = output
    resp["status"] = "completed"
    _remember(resp["id"], payload["messages"])
    yield _sse("response.completed", {"type": "response.completed", "response": resp})


async def _nonstream_to_responses(upstream: httpx.AsyncClient, url: str, headers: dict, payload: dict, model: str):
    r = await upstream.post(url, json=payload, headers=headers, timeout=600.0)
    resp = _response_shell(model)
    if r.status_code >= 400:
        return JSONResponse(
            {"error": {"code": str(r.status_code), "message": r.text[:600]}}, status_code=r.status_code
        )
    body = r.json()
    output = []
    for choice in body.get("choices") or []:
        msg = choice.get("message") or {}
        if msg.get("content"):
            output.append({
                "type": "message", "id": f"msg_{uuid.uuid4().hex}", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": msg["content"], "annotations": []}],
            })
        for tc in msg.get("tool_calls") or []:
            output.append({
                "type": "function_call",
                "id": f"fc_{uuid.uuid4().hex[:8]}",
                "call_id": tc.get("id", ""),
                "name": (tc.get("function") or {}).get("name", ""),
                "arguments": (tc.get("function") or {}).get("arguments", ""),
                "status": "completed",
            })
    usage = body.get("usage") or {}
    resp["usage"] = {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }
    resp["output"] = output
    resp["status"] = "completed"
    _remember(resp["id"], payload["messages"])
    return JSONResponse(resp)


@app.get("/v1/models")
@app.get("/models")
async def models(request: Request):
    headers = _auth_headers(request)
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{UPSTREAM_BASE}/models", headers=headers, timeout=30.0)
            if r.status_code < 400:
                return r.json()
    except Exception:  # noqa: BLE001
        pass
    return {"object": "list", "data": [{"id": DEFAULT_MODEL, "object": "model"}]}


@app.post("/v1/responses")
async def responses(request: Request):
    body = await request.json()
    if os.getenv("CODEX_BRIDGE_DEBUG"):
        try:
            with open(os.path.join(os.path.dirname(__file__), "..", "codex_bridge_debug.log"), "a", encoding="utf-8") as f:
                f.write(json.dumps({"type": "request", "body": body}, default=str)[:4000] + "\n\n")
        except Exception:  # noqa: BLE001
            pass
    headers = _auth_headers(request)
    stream = bool(body.get("stream", True))
    model = body.get("model") or DEFAULT_MODEL
    payload = _chat_payload(body, stream)
    url = f"{UPSTREAM_BASE}/chat/completions"
    client = httpx.AsyncClient()
    if stream:
        gen = _stream_to_responses(client, url, headers, payload, model)
        return StreamingResponse(gen, media_type="text/event-stream", background=BackgroundTask(client.aclose))
    try:
        return await _nonstream_to_responses(client, url, headers, payload, model)
    finally:
        await client.aclose()


@app.get("/")
async def root():
    return {
        "service": "agent-legacy codex bridge",
        "upstream": UPSTREAM_BASE,
        "default_model": DEFAULT_MODEL,
        "hint": "Codex talks to http://127.0.0.1:%d/v1 with wire_api = responses" % PORT,
    }


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
