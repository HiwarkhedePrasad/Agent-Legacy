"""Textual (terminal UI) dashboard for Agent-Legacy.

Mission-control design: dark base, a single accent family, tight type
contrast between the wordmark, status pills and monospace data feeds.

Layout:
    +--------------------------------------------------------------+
    | AGENT-LEGACY   cost-aware multi-agent deep research   T+elapsed |
    | [RUNNING ◐]  tier: complex  model: ...  step #12              |
    +-------------------------------+------------------------------+
    | LIVESTREAM (scrolling log)    | TEAM / ACTIVITY              |
    |                               | BROWSING (urls visited)      |
    |                               | COLLECTED (data previews)    |
    +-------------------------------+------------------------------+
    | tokens in/out | est cost | files written                     |
    | > type a task, Enter to run ...                              |
    +--------------------------------------------------------------+

Run:  python -m agent.tapp ["initial task"]
"""
from __future__ import annotations

import asyncio
import time

from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.widgets import Footer, Input, RichLog, Static

from agent.commands import (
    COMMANDS,
    MemoryScreen,
    SessionPickerScreen,
    SuggestionOverlay,
    help_lines,
    is_command,
)
from agent.services.runner import run_agent

QUIT_WORDS = ("exit", "quit", "q", "bye")

TIER_COLORS = {"simple": "#56d3a6", "medium": "#e3b341", "complex": "#ffffff"}
AGENT_GLYPHS = {
    "planner": "◆",
    "research": "⌕",
    "executor": "▣",
    "decision": "⚖",
    "qa": "✓",
}

CSS = """
Screen { background: #000000; }

#banner {
    height: 1;
    padding: 0 1;
    background: #0a0a0a;
    border-bottom: solid #1c1c1c;
}
.banner-mark { color: #ffffff; text-style: bold; }
.banner-tag { color: #6e6e6e; margin-left: 2; }
.banner-clock { color: #4a4a4a; }

#statusbar { height: 2; padding: 0 1; background: #0e0e0e; content-align-vertical: middle; }
.pill { padding: 0 1; text-style: bold; }
.pill-idle    { background: #1c1c1c; color: #9d9d9d; }
.pill-planning{ background: #2a2a2a; color: #e0e0e0; }
.pill-running { background: #14271c; color: #56d3a6; }
.pill-done    { background: #17361d; color: #3fb950; }
.pill-retrying{ background: #3a2e14; color: #e3b341; }
.pill-error   { background: #3a1414; color: #f85149; }
.status-meta { color: #8a8a8a; margin-left: 2; }
.status-step { color: #e3b341; margin-left: 2; text-style: bold; }

#main { height: 1fr; }
#log {
    width: 2fr;
    background: #000000;
    border-right: solid #1c1c1c;
    scrollbar-color: #4a4a4a;
    scrollbar-background: #0e0e0e;
}
#side { width: 1fr; background: #0a0a0a; padding: 0 1; }
.side-head { text-style: bold; margin: 0; }
.head-team      { color: #56d3a6; }
.head-browsing  { color: #d0d0d0; }
.head-collected { color: #e3b341; }
.side-row       { color: #8a8a8a; margin: 0 0 0 2; }
.side-empty     { color: #4a4a4a; margin: 0 0 1 2; }

#costbar {
    height: 2; padding: 0 1;
    background: #0e0e0e; border-top: solid #1c1c1c;
    content-align-vertical: middle;
}
.cost-num   { color: #e3b341; text-style: bold; }
.cost-label { color: #8a8a8a; }
.cost-files { color: #ffffff; }

#prompt {
    dock: bottom; margin: 0;
    background: #0a0a0a; border-top: solid #2a2a2a;
}
#prompt > .input--placeholder { color: #4a4a4a; }
"""


class AgentLegacyApp(App):
    """Mission-control dashboard for the Agent-Legacy team."""

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
    ]

    CSS = CSS
    TITLE = "Agent-Legacy"
    SUB_TITLE = "cost-aware multi-agent deep research"

    # Reactive state shown in the status / cost bars.
    status = reactive("idle")
    tier = reactive("-")
    model = reactive("-")
    step = reactive(0)
    tokens_in = reactive(0)
    tokens_out = reactive(0)
    cost_usd = reactive(0.0)
    real_usage = reactive(False)
    _heartbeat = reactive("")

    def __init__(self, session_id: str = "tui", initial_prompt: str | None = None) -> None:
        super().__init__()
        self.session_id = session_id
        self.pending: list[str] = []
        if initial_prompt:
            self.pending.append(initial_prompt)
        self._driving = False
        self.start_time = time.time()
        self.token_buf = ""
        self.streamed_text = ""
        self.browsed: list[str] = []
        self.collected: list[tuple[str, str]] = []
        self.activity: list[tuple[str, str]] = []
        self.artifacts: list[str] = []
        self.visits = 0
        self.char_count = 0

    # ---- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(self._banner_text(), id="banner", markup=False)
        yield Static(id="statusbar", markup=True)
        with Horizontal(id="main"):
            yield RichLog(
                id="log",
                highlight=False,
                markup=False,
                min_width=40,
                auto_scroll=True,
                wrap=True,
                max_lines=800,
            )
            yield VerticalScroll(Static(id="side", expand=True))
        yield Static(id="costbar", markup=True)
        yield Input(
            placeholder=" type a task · Enter to run · /sessions to resume a past session",
            id="prompt",
        )
        yield SuggestionOverlay()
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#log").auto_scroll = True
        self.set_interval(1.0, self._tick)
        self.set_interval(0.5, self._tick_heartbeat)
        self.query_one("#prompt").focus()
        self._render_status()
        self._render_cost()
        self._render_side()
        if self.pending:
            asyncio.create_task(self._drive())

    # ---- ticks --------------------------------------------------------------
    def _tick(self) -> None:
        self._render_status()

    def _tick_heartbeat(self) -> None:
        glyphs = "◐◑◒◓"
        if self.status not in ("idle", "done", "error"):
            self._heartbeat = glyphs[int(time.time() * 2) % len(glyphs)]
        else:
            self._heartbeat = ""
        self._render_status()

    def _banner_text(self) -> Text:
        t = Text()
        t.append("AGENT-LEGACY", style="bold #ffffff")
        t.append("   cost-aware multi-agent deep research · each job routed to the cheapest capable model", style="#6e6e6e")
        return t

    # ---- rendering helpers -------------------------------------------------
    def _log(self, text: Text) -> None:
        self.query_one("#log").write(text, scroll_end=True)

    def _line(self, prefix: str, body: str, style: str = "#d0d0d0") -> None:
        t = Text()
        t.append(prefix, style=style)
        t.append(" " + body)
        self._log(t)

    def _render_status(self) -> None:
        label = self.status.upper()
        pill_class = f"pill-{self.status if self.status in ('idle', 'done', 'error', 'running', 'retrying') else 'planning'}"
        tier_color = TIER_COLORS.get(self.tier, "#8a8a8a")
        beat = f" {self._heartbeat}" if self._heartbeat else ""
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        self.query_one("#statusbar").update(
            f'[{pill_class} pill] {label}{beat} [/] '
            f'[status-meta]tier[/] [bold {tier_color}]{self.tier}[/]   '
            f'[status-meta]model[/] [bold #e0e0e0]{self.model}[/]'
            + (f'  [status-step]step #{self.step}[/]' if self.step else "")
            + f'[status-meta]   T+{mins:02d}:{secs:02d}[/]'
        )

    def _render_cost(self) -> None:
        files = ", ".join(self.artifacts) or "-"
        label = "real usage" if self.real_usage else "est"
        self.query_one("#costbar").update(
            f"[cost-label]tokens({label})[/] [cost-num]{self.tokens_in:,}[/] [cost-label]in /[/] "
            f"[cost-num]{self.tokens_out:,}[/] [cost-label]out    "
            f"cost[/] [cost-num]${self.cost_usd:.4f}[/]    "
            f"[cost-label]files[/] [cost-files]{files}[/]"
        )

    def _render_side(self) -> None:
        w = self.query_one("#side").size.width or 36
        lines: list[str] = []
        n = max(w - 4, 8)

        lines.append("[side-head head-team]TEAM ACTIVITY[/]")
        if self.activity:
            for name, ts in self.activity[-7:]:
                g = AGENT_GLYPHS.get(name, "·")
                lines.append(f"[side-row]{g} {name:<9s} [#545d68]{ts}[/][/]")
        else:
            lines.append("[side-empty]no delegations yet[/]")
        lines.append("")

        lines.append(f"[side-head head-browsing]BROWSING · {self.visits} url[/]")
        if self.browsed:
            for url in self.browsed[-6:]:
                shown = url if len(url) <= n else url[: n - 1] + "…"
                lines.append(f"[side-row]→ {shown}[/]")
        else:
            lines.append("[side-empty]no sites visited[/]")
        lines.append("")

        lines.append(f"[side-head head-collected]COLLECTED · {self.char_count:,} ch[/]")
        if self.collected:
            for _, prev in self.collected[-5:]:
                p = prev if len(prev) <= n else prev[: n - 1] + "…"
                lines.append(f"[side-row]“{p}”[/]")
        else:
            lines.append("[side-empty]nothing collected[/]")

        self.query_one("#side").update("\n".join(lines))

    # ---- event stream ------------------------------------------------------
    def handle_event(self, ev: dict) -> None:
        t = ev["type"]
        if t == "routed":
            self.status, self.tier, self.model = "planning", ev["tier"], ev["model"]
            self._line(
                "[route]",
                f"tier={ev['tier']}  model={ev['model']}  ({ev['reason']})",
                style="bold #ffffff",
            )
        elif t == "token":
            self.token_buf += ev["content"]
            self.streamed_text += ev["content"]
            if len(self.token_buf) > 140 or ev["content"].rstrip().endswith(("\n", ".", "?", "!")):
                text = Text("  | ", style="#4a4a4a")
                text.append(self.token_buf.strip())
                self._log(text)
                self.token_buf = ""
        elif t == "agent":
            g = AGENT_GLYPHS.get(ev["name"], "◇")
            self._line(f"{g} {ev['name']}", ev["message"], style="bold #56d3a6")
        elif t == "handoff":
            self.activity.append((ev["to"], time.strftime("%H:%M:%S")))
            g = AGENT_GLYPHS.get(ev["to"], "·")
            self._line(f"{g} handoff →", ev["to"], style="bold #ffffff")
        elif t == "status":
            self.status = "running"
            self.step = ev["step"]
            self._line(f"#{ev['step']:>2}", f"[{ev['agent']}] {ev['text']}", style="#e3b341")
        elif t == "visit":
            self.visits += 1
            self.browsed.append(ev["url"])
            self._line("⌕", ev["url"], style="#9d9d9d")
        elif t == "collected":
            self.collected.append((ev["source"], ev["preview"]))
            self.char_count += ev["chars"]
            self._line("▤ data", f"+{ev['chars']:,} chars", style="#e3b341")
        elif t == "warning":
            self._line("⚠", ev["message"], style="#e3b341")
        elif t == "retry":
            self.status = "retrying"
            self._line(
                "↻ retry",
                f"attempt {ev['attempt']} — waiting {int(ev['wait'])}s before continuing ({ev.get('reason', 'recovering')[:90]})",
                style="bold #e3b341",
            )
        elif t == "tool_result" and not ev["ok"]:
            self._line("✗ failed", f"{ev['name']} — retrying from another angle", style="bold #f85149")
        elif t == "artifacts":
            self.artifacts = ev["files"]
        elif t == "cost":
            self.tokens_in, self.tokens_out = ev["tokens_in"], ev["tokens_out"]
            self.cost_usd = ev["est_cost_usd"]
            self.real_usage = bool(ev.get("real_usage"))
        elif t == "complete":
            self.status = "done"
            if self.token_buf:
                text = Text("  | ", style="#4a4a4a")
                text.append(self.token_buf.strip())
                self._log(text)
                self.token_buf = ""
            if ev.get("artifacts"):
                self.artifacts = list(dict.fromkeys(self.artifacts + ev["artifacts"]))
            final = (ev.get("final") or "").strip()
            if final:
                if final in self.streamed_text:
                    self._line("■ done", "answer complete (fully streamed above)", style="bold #3fb950")
                else:
                    self._line("■ done", "", style="bold #3fb950")
                    self.query_one("#log").write(Markdown(final), scroll_end=True)
            qa = ev.get("qa_verified")
            if qa is True:
                self._line("✓ qa", "independently reviewed before shipping", style="bold #3fb950")
            elif qa is False and self.artifacts:
                self._line("✗ qa", "deliverable shipped without independent QA review", style="#e3b341")
        elif t == "error":
            self.status = "error"
            self._line("✗ error", ev["message"], style="bold #f85149")
        self._render_status()
        self._render_cost()
        self._render_side()

    # ---- task driver (sequential REPL) -------------------------------------
    async def _drive(self) -> None:
        """Run queued prompts one at a time; the Input stays live between runs."""
        if self._driving:
            return
        self._driving = True
        try:
            while self.pending:
                prompt = self.pending.pop(0)
                self.status = "running"
                self.step = 0
                self.artifacts = []
                self.tokens_in = self.tokens_out = 0
                self.cost_usd = 0.0
                self.token_buf = ""
                divider = Text()
                divider.append("── TASK ", style="#4a4a4a")
                divider.append(prompt, style="bold #f0f0f0")
                divider.append("  " + "─" * max(1, 46 - len(prompt)), style="#4a4a4a")
                self._log(divider)
                self._render_status()
                async for ev in run_agent(prompt, self.session_id):
                    self.handle_event(ev)
                self.status = "idle"
                self._render_status()
        finally:
            self._driving = False

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        self.query_one("#prompt").value = ""
        self._hide_suggestions()
        if not prompt:
            return

        # opencode-style completion: if the popup was open and the text is a
        # partial/unique prefix of a known command, complete it here and wait
        # for the next Enter to run it.
        if is_command(prompt) and prompt not in {c.split()[0] for c in COMMANDS} | {"/?"}:
            from agent.commands import SUGGESTIONS

            matches = [n for n, _ in SUGGESTIONS if n.startswith(prompt)]
            if len(matches) == 1:
                self.query_one("#prompt", Input).value = matches[0] + " "
                self._suggestions().refresh_for(matches[0])
                self.query_one("#prompt", Input).focus()
                return
            if not matches:
                self._line("/", f"unknown command: {prompt} — try /help", style="#f85149")
                self.query_one("#prompt", Input).focus()
                return

        if is_command(prompt):
            self._run_command(prompt)
            self.query_one("#prompt").focus()
            return

        if prompt.lower() in QUIT_WORDS:
            self.exit()
            return
        self.pending.append(prompt)
        if not self._driving:
            asyncio.create_task(self._drive())
        else:
            self._line("queued", prompt, style="#6e6e6e")
        self.query_one("#prompt").focus()

    # ---- "/" autocomplete popup ------------------------------------------------
    def _suggestions(self) -> SuggestionOverlay:
        return self.query_one("#suggestions", SuggestionOverlay)

    def _hide_suggestions(self) -> None:
        self._suggestions().display = False

    def on_input_changed(self, event: Input.Changed) -> None:
        """Refresh the opencode-style command popup as the user types '/'."""
        overlay = self._suggestions()
        text = event.value
        if text.startswith("/") and "\n" not in text:
            overlay.refresh_for(text)
        else:
            overlay.display = False

    def key_tab(self) -> None:
        """Tab completes the highlighted suggestion."""
        overlay = self._suggestions()
        if overlay.visible():
            name = overlay.selected_name()
            if name:
                self.query_one("#prompt", Input).value = name + " "
                overlay.refresh_for(name)
            return
        # popup closed: let Tab bubble normally (App has no key_tab to chain to)

    def key_enter(self) -> None:
        """Enter completes the highlighted suggestion first; a second Enter runs it."""
        overlay = self._suggestions()
        if overlay.visible():
            text = self.query_one("#prompt", Input).value.strip()
            name = overlay.selected_name()
            if name and text != name:
                self.query_one("#prompt", Input).value = name + " "
                overlay.refresh_for(name)
            return
        # popup closed: if focus wandered (e.g. user clicked the log to scroll),
        # Enter brings it back to the input bar; otherwise the Input submits itself.
        prompt = self.query_one("#prompt", Input)
        if self.focused is not prompt:
            prompt.focus()

    def key_up(self) -> None:
        if self._suggestions().visible():
            self._suggestions().move(-1)
            return

    def key_down(self) -> None:
        if self._suggestions().visible():
            self._suggestions().move(1)
            return

    # ---- slash commands -----------------------------------------------------
    def _run_command(self, text: str) -> None:
        parts = text.split()
        cmd = parts[0].lower()
        arg = " ".join(parts[1:]).strip()

        if cmd in ("/help", "/?"):
            self._line("/", "available commands:", style="bold #ffffff")
            for line in help_lines():
                self._line(" ", line, style="#9d9d9d")

        elif cmd == "/clear":
            self.query_one("#log").clear()
            self._line("/", "log cleared", style="#6e6e6e")

        elif cmd == "/status":
            tier_color = TIER_COLORS.get(self.tier, "#8a8a8a")
            self._line("/", f"status=[bold]{self.status}[/]  tier={self.tier}", style="bold #ffffff")
            self._line(" ", f"model={self.model}", style="#d0d0d0")
            self._line(" ", f"driving={self._driving}  queued={len(self.pending)}", style="#8a8a8a")

        elif cmd == "/cost":
            self._line("/", f"tokens: {self.tokens_in:,} in / {self.tokens_out:,} out", style="bold #ffffff")
            self._line(" ", f"est cost: ${self.cost_usd:.6f}", style="#e3b341")

        elif cmd == "/files":
            if self.artifacts:
                self._line("/", "deliverables:", style="bold #ffffff")
                for f in self.artifacts:
                    self._line(" ", f"[dim]▸[/dim] {f}", style="#d0d0d0")
            else:
                self._line("/", "no files written yet in this run", style="#8a8a8a")

        elif cmd == "/team":
            if self.activity:
                self._line("/", "recent handoffs:", style="bold #ffffff")
                for name, ts in self.activity[-8:]:
                    g = AGENT_GLYPHS.get(name, "·")
                    self._line(" ", f"{g} {name:<10s} [#6e6e6e]{ts}[/]", style="#d0d0d0")
            else:
                self._line("/", "no delegations yet", style="#8a8a8a")

        elif cmd in ("/memory", "/mem"):
            self.push_screen(MemoryScreen(self.session_id, query=arg))

        elif cmd in ("/sessions", "/session", "/open"):
            if arg:
                # Direct resume: /sessions tui
                self._switch_session(arg)
            else:
                self.push_screen(SessionPickerScreen(self.session_id), self._switch_session)

        elif cmd in ("/exit", "/quit", "/q"):
            self.exit()
            return

        else:
            self._line("/", f"unknown command: {cmd} — try /help", style="#f85149")

        self._render_status()

    # ---- session switching ---------------------------------------------------
    def _switch_session(self, session_id: str | None) -> None:
        """Callback from the session picker: resume the chosen session."""
        from agent.commands import list_sessions

        target = (session_id or "").strip()
        if not target:
            return  # dismissed with Esc
        known = {s["id"] for s in list_sessions()}
        is_new = target not in known
        if self._driving:
            self._line(
                "!", "a task is running — finish it before switching sessions", style="#e3b341"
            )
            return
        self.session_id = target
        # reset run-local dashboard state for the fresh context
        self.activity = []
        self.browsed = []
        self.collected = []
        self.artifacts = []
        self.visits = 0
        self.char_count = 0
        self.streamed_text = ""
        self.token_buf = ""
        verb = "started new session" if is_new else "resumed session"
        self._line("■ session", f"{verb}: {target}", style="bold #ffffff")
        self._render_side()
        self._render_cost()
        self._render_status()
        self.query_one("#prompt", Input).focus()


def main() -> None:
    import sys

    args = sys.argv[1:]
    session_id = "tui"
    # --session NAME  resumes a named session from the start
    if "--session" in args:
        i = args.index("--session")
        if i + 1 < len(args):
            session_id = args[i + 1]
            args = args[:i] + args[i + 2 :]
    initial = " ".join(args) or None
    AgentLegacyApp(session_id=session_id, initial_prompt=initial).run()


if __name__ == "__main__":
    main()
