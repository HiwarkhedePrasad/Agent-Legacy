"""Temporary validation of the Textual dashboard (headless via run_test)."""
import asyncio

from agent import tapp


async def fake_run_agent(prompt, session_id):
    yield {"type": "routed", "tier": "simple", "model": "test-model", "reason": "heuristic => simple"}
    yield {"type": "token", "content": "Hello there. This is a test. "}
    yield {"type": "status", "step": 1, "agent": "planner", "text": "working on it"}
    yield {"type": "visit", "url": "https://example.com"}
    yield {"type": "collected", "source": "fetch_url", "preview": "some data", "chars": 123}
    yield {"type": "tool_result", "name": "fetch_url", "ok": False}
    yield {"type": "complete", "final": "Finished task.", "artifacts": ["a.md"]}


def _plain(s):
    if isinstance(s, str):
        return s
    t = getattr(s, "text", None)
    if isinstance(t, str):
        return t
    if hasattr(t, "plain"):
        return t.plain
    return str(s)


app = tapp.OpsCenterApp(session_id="tui", initial_prompt="do a thing")
tapp.run_agent = fake_run_agent


async def scenario():
    async with app.run_test() as pilot:
        # Let the initial_prompt driver run to completion.
        for _ in range(50):
            await asyncio.sleep(0.05)
            if not app._driving and not app.pending:
                break
        log = app.query_one("#log")
        text = "\n".join(_plain(s) for s in log.lines)
        assert "do a thing" in text, text
        assert "Finished task." in text, text
        assert text.count("NEW TASK") >= 1, text

        # REPL: submit a second task via the Input widget handler.
        from textual.widgets import Input

        inp = app.query_one("#prompt")
        ev = Input.Submitted(inp, value="second task")
        await app.on_input_submitted(ev)
        for _ in range(50):
            await asyncio.sleep(0.05)
            if not app._driving and not app.pending:
                break
        text2 = "\n".join(_plain(s) for s in log.lines)
        assert "second task" in text2, text2

        # Status / footer preserved the last values.
        assert app.status in ("idle", "done"), app.status
        assert app.model == "test-model", app.model
        assert app.tier == "simple", app.tier

        print("TEXTUAL TUI VALIDATION OK")
        print("log lines:", len(log.lines))
        print("queue empty:", not app.pending)


asyncio.run(scenario())
