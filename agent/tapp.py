"""Textual dashboard for Agent-Legacy — a RESEARCH COMMAND CENTER.

Design philosophy (progressive disclosure):

    Level 1  WHAT IS HAPPENING   -> status pill + active pipeline stage
    Level 2  WHAT DID IT DO      -> pipeline, findings, sources, result
    Level 3  WHAT EACH AGENT DID -> roster with live states (left panel)
    Level 4  RAW EXECUTION TRACE -> hidden by default, /logs to open

Layout:

    +-------------------------------------------------------------+
    | AGENT-LEGACY   ·  house sigil  ·  cheapest capable model    |
    | [RUNNING ◐]  tier · model · pipeline stage · T+elapsed      |
    +-----------+-------------------------------+-----------------+
    | AGENTS    |        PRIMARY WORKSPACE      | ACTIVITY        |
    | ◆ planner |  TASK "..."                   | timeline of     |
    | ⌕ research|  ✓ Plan  ● Research  ○ Build  | semantic steps  |
    | ▣ executor|  findings · sources · result  | (search opened..)|
    +-----------+-------------------------------+-----------------+
    | latest activity line (raw trace: /logs)                     |
    | ✓ 12 sources · 8 findings · files · tokens · cost           |
    | > type a task · Enter to run · Tab switches house           |
    +-------------------------------------------------------------+
"""
from __future__ import annotations

import asyncio
import time
from collections import deque

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, RichLog, Static

from agent.commands import (
    COMMANDS,
    HousePickerScreen,
    MemoryScreen,
    SessionPickerScreen,
    SuggestionOverlay,
    help_lines,
    is_command,
)
from agent.config import settings
from agent.memory.long_term import LongTermMemory
from agent.modes import get_mode, get_mode_key, next_mode, set_mode
from agent.services.runner import run_agent

QUIT_WORDS = ("exit", "quit", "q", "bye")

TIER_COLORS = {"simple": "#56d3a6", "medium": "#e3b341", "complex": "#ffffff"}

AGENT_META = {
    "planner": {"glyph": "◆", "role": "Planner", "color": "#ffffff", "desc": "coordinates the team"},
    "research": {"glyph": "⌕", "role": "Research", "color": "#79c0ff", "desc": "finds & reads sources"},
    "executor": {"glyph": "▣", "role": "Executor", "color": "#ecb939", "desc": "builds files"},
    "decision": {"glyph": "⚖", "role": "Decision", "color": "#d2a8ff", "desc": "hard reasoning"},
    "qa": {"glyph": "✓", "role": "QA", "color": "#3fb950", "desc": "verifies deliverables"},
}

STATE_GLYPHS = {"waiting": "○", "working": "◐", "done": "✓", "attention": "⚠", "failed": "✕"}

# Pipeline stages shown in the primary workspace.
STAGES = (
    ("plan", "Plan", "break the goal down"),
    ("research", "Research", "gather cited evidence"),
    ("build", "Build", "produce deliverables"),
    ("review", "Review", "independent QA"),
    ("deliver", "Deliver", "final answer"),
)

CSS = """
Screen { background: #000000; }

#banner { height: 2; padding: 0 1; background: #0a0a0a; border-bottom: solid #1c1c1c; content-align-vertical: middle; }

#statusbar { height: 2; padding: 0 1; background: #0e0e0e; content-align-vertical: middle; }
.pill { padding: 0 1; text-style: bold; }
.pill-idle    { background: #1c1c1c; color: #9d9d9d; }
.pill-planning{ background: #2a2a2a; color: #e0e0e0; }
.pill-running { background: #14271c; color: #56d3a6; }
.pill-done    { background: #17361d; color: #3fb950; }
.pill-retrying{ background: #3a2e14; color: #e3b341; }
.pill-error   { background: #3a1414; color: #f85149; }
.status-meta { color: #8a8a8a; margin-left: 2; }
.status-stage { color: #79c0ff; margin-left: 2; text-style: bold; }

#main { height: 1fr; }

#agents-panel {
    width: 28;
    background: #0a0a0a;
    border-right: solid #1c1c1c;
}
#workspace-scroll { width: 1fr; background: #000000; }
#activity-panel {
    width: 40;
    background: #0a0a0a;
    border-left: solid #1c1c1c;
}
#agents, #activity { padding: 0 1; }
#workspace { padding: 0 2; }
.panel-head { text-style: bold; color: #ffffff; }

#rawline {
    height: 2; padding: 0 1;
    background: #0a0a0a; border-top: solid #1c1c1c;
    content-align-vertical: middle;
}
#costbar { height: 2; padding: 0 1; background: #0e0e0e; border-top: solid #1c1c1c; content-align-vertical: middle; }
.cost-num   { color: #e3b341; text-style: bold; }
.cost-label { color: #8a8a8a; }
.cost-ok    { color: #3fb950; text-style: bold; }
.cost-warn  { color: #e3b341; }

#prompt { margin: 0; background: #0a0a0a; border-top: solid #2a2a2a; }
#prompt > .input--placeholder { color: #4a4a4a; }

LogScreen { align: center middle; }
LogScreen > VerticalScroll {
    width: 96%; height: 90%;
    background: #000000; border: double #3a3a3a; padding: 1 1;
}
"""


class LogScreen(ModalScreen):
    """Level-4 debug view: the raw execution trace, live-updated."""

    BINDINGS = [("escape", "dismiss", "Close"), ("q", "dismiss", "Close")]

    def __init__(self, buffer: deque) -> None:
        super().__init__()
        self._buffer = buffer

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static(id="raw-log", expand=True))

    def on_mount(self) -> None:
        self._refresh()
        self.set_interval(0.5, self._refresh)

    def _refresh(self) -> None:
        t = Text()
        t.append("RAW EXECUTION TRACE  ·  Esc closes\n\n", style="bold #8a8a8a")
        for line in list(self._buffer):
            t.append_text(line)
            t.append("\n")
        self.query_one("#raw-log", Static).update(t)


class AgentLegacyApp(App):
    """Research command center for the Agent-Legacy team."""

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        Binding("tab", "cycle_house", "Switch house", priority=True),
    ]

    CSS = CSS
    TITLE = "Agent-Legacy"
    SUB_TITLE = "cost-aware multi-agent deep research"

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
        # raw execution trace (Level 4 — hidden by default)
        self.raw: deque[Text] = deque(maxlen=600)
        self._reset_run("")

    def _reset_run(self, task: str) -> None:
        self.task_text = task
        self.token_buf = ""
        self.streamed_text = ""
        self.sources: list[str] = []           # unique urls visited
        self.findings: list[str] = []          # collected evidence previews
        self.artifacts: list[str] = []
        self.char_count = 0
        self.qa_state = "waiting"              # waiting / done / failed / skipped
        self.qa_note = ""
        self.final_md: Markdown | None = None
        self.error_text = ""
        # pipeline stage machine: fired -> done/active/skipped
        self.stage_state: dict[str, str] = {key: "waiting" for key, _, _ in STAGES}
        self.stage_stats: dict[str, str] = {}
        # per-agent live state
        self.agents: dict[str, dict] = {
            name: {"state": "waiting", "action": meta["desc"]}
            for name, meta in AGENT_META.items()
        }
        self.timeline: list[str] = []          # semantic activity feed

    # ---- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(self._banner_text(), id="banner", markup=False)
        yield Static(id="statusbar", markup=True)
        with Horizontal(id="main"):
            with VerticalScroll(id="agents-panel"):
                yield Static(id="agents", expand=True)
            with VerticalScroll(id="workspace-scroll"):
                yield Static(id="workspace", expand=True)
            with VerticalScroll(id="activity-panel"):
                yield Static(id="activity", expand=True)
        yield Static("", id="rawline", markup=False)
        yield Static(id="costbar", markup=True)
        yield Input(
            placeholder=" type a task · Enter to run · Tab switches house · /sessions · /logs",
            id="prompt",
        )
        yield SuggestionOverlay()
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(1.0, self._tick)
        self.set_interval(0.5, self._tick_heartbeat)
        self.query_one("#prompt").focus()
        self._render_all()
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
        house = get_mode()
        t = Text()
        t.append("AGENT-LEGACY", style="bold #ffffff")
        t.append("   cost-aware multi-agent deep research", style="#6e6e6e")
        t.append("    ")
        t.append(f"{house.glyph} {house.name.upper()}", style=f"bold {house.color}")
        t.append(f"  · {house.advantage}", style="#8a8a8a")
        return t

    # ---- rendering ----------------------------------------------------------
    def _render_all(self) -> None:
        self._render_status()
        self._render_agents()
        self._render_workspace()
        self._render_activity()
        self._render_cost()
        self._render_rawline()

    def _render_status(self) -> None:
        label = self.status.upper()
        pill_class = f"pill-{self.status if self.status in ('idle', 'done', 'error', 'running', 'retrying') else 'planning'}"
        tier_color = TIER_COLORS.get(self.tier, "#8a8a8a")
        beat = f" {self._heartbeat}" if self._heartbeat else ""
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        stage = self._active_stage_label()
        stage_part = f'  [status-stage]{stage}[/]' if stage else ""
        self.query_one("#statusbar").update(
            f'[{pill_class} pill] {label}{beat} [/] '
            f'[status-meta]tier[/] [bold {tier_color}]{self.tier}[/]   '
            f'[status-meta]model[/] [bold #e0e0e0]{self.model}[/]'
            + stage_part
            + f'[status-meta]   T+{mins:02d}:{secs:02d}[/]'
        )

    def _active_stage_label(self) -> str:
        for key, name, _ in STAGES:
            if self.stage_state[key] == "active":
                return f"● {name}"
        return ""

    def _render_agents(self) -> None:
        t = Text()
        t.append("AGENTS\n", style="bold #ffffff")
        for name, meta in AGENT_META.items():
            state = self.agents[name]["state"]
            glyph = STATE_GLYPHS[state]
            state_style = {
                "waiting": "#4a4a4a", "working": "#56d3a6",
                "done": "#3fb950", "attention": "#e3b341", "failed": "#f85149",
            }[state]
            t.append(f"{glyph} ", style=f"bold {state_style}")
            t.append(f"{meta['glyph']} {meta['role']:<10s}", style=f"bold {meta['color']}")
            t.append("\n")
            action = self.agents[name]["action"]
            t.append(f"    {action[:19]}\n", style="#8a8a8a" if state != "working" else state_style)
        house = get_mode()
        t.append("\nHOUSE MODE\n", style="bold #ffffff")
        t.append(f"  {house.glyph} {house.name}", style=f"bold {house.color}")
        t.append(f"  · {house.advantage}\n", style="#8a8a8a")
        t.append(f"    {house.trait} house — press Tab\n    to switch\n", style="#4a4a4a")
        self.query_one("#agents", Static).update(t)

    def _render_workspace(self) -> None:
        parts: list = []

        # TASK ----------------------------------------------------------
        p = Text()
        p.append("TASK\n", style="bold #ffffff")
        p.append(f"  “{self.task_text or 'no task yet — type one below'}”\n\n", style="#e0e0e0")
        parts.append(p)

        # PIPELINE -------------------------------------------------------
        # QUICK START — only while idle; real work replaces it once a task runs
        if not self.task_text:
            p = Text()
            p.append("QUICK START\n", style="bold #ffffff")
            p.append("  Type a task below and press Enter. Try:\n", style="#c0c0c0")
            for sample in (
                "research AI agents and write a report",
                "compare LangGraph vs CrewAI, save findings",
                "latest news on ISRO and summarize it",
            ):
                p.append("    ▸ ", style="#e3b341")
                p.append(sample + "\n", style="#79c0ff")
            p.append("\n  /help commands · /sessions resume ·\n", style="#8a8a8a")
            p.append("  /memory recall · /logs raw trace\n", style="#8a8a8a")
            parts.append(Text())
            parts.append(p)

        p = Text()
        p.append("PIPELINE\n", style="bold #ffffff")
        for key, name, desc in STAGES:
            state = self.stage_state[key]
            glyph, style = {
                "waiting": ("○", "#4a4a4a"),
                "active": ("●", "#56d3a6"),
                "done": ("✓", "#3fb950"),
                "skipped": ("–", "#3d3d3d"),
                "failed": ("✕", "#f85149"),
            }[state]
            p.append(f"  {glyph} {name:<9s}", style=f"bold {style}")
            stat = self.stage_stats.get(key, "")
            p.append("  " + (stat if stat else (desc if state == "active" else "")), style="#8a8a8a")
            p.append("\n")
        parts.append(Text())
        parts.append(p)

        # FINDINGS -------------------------------------------------------
        p = Text()
        p.append(f"FINDINGS · {len(self.findings)}\n", style="bold #ffffff")
        if self.findings:
            for i, f in enumerate(self.findings[-6:], 1):
                p.append(f"  {i}. ", style="#e3b341")
                p.append(f + "\n", style="#c0c0c0")
        else:
            p.append("  no evidence collected yet\n", style="#4a4a4a")
        parts.append(Text())
        parts.append(p)

        # SOURCES --------------------------------------------------------
        p = Text()
        p.append(f"SOURCES · {len(self.sources)}\n", style="bold #ffffff")
        if self.sources:
            for u in self.sources[-6:]:
                shown = u if len(u) <= 70 else u[:69] + "…"
                p.append("  • ", style="#79c0ff")
                p.append(shown + "\n", style="#9d9d9d")
        else:
            p.append("  none yet\n", style="#4a4a4a")
        parts.append(Text())
        parts.append(p)

        # RESULT ----------------------------------------------------------
        p = Text()
        if self.final_md is not None:
            p.append("RESULT", style="bold #3fb950")
        elif self.status in ("running", "planning", "retrying"):
            p.append("RESULT", style="bold #ffffff")
            p.append("  generating…", style="#4a4a4a")
        if self.error_text:
            parts.append(Text())
            e = Text()
            e.append("ERROR\n", style="bold #f85149")
            e.append("  " + self.error_text, style="#f85149")
            parts.append(e)
        parts.append(Text())
        parts.append(p)
        content: object
        if self.final_md is not None:
            content = Group(*parts, self.final_md)
        else:
            stream = self.token_buf.strip() or (self.streamed_text.strip()[-600:] if self.streamed_text else "")
            if stream:
                s = Text()
                s.append("  " + stream, style="#d0d0d0")
                content = Group(*parts, s)
            else:
                content = Group(*parts)
        self.query_one("#workspace", Static).update(content)

    def _render_activity(self) -> None:
        t = Text()
        t.append("ACTIVITY\n", style="bold #ffffff")
        if self.timeline:
            for line in self.timeline[-24:]:
                t.append(line + "\n", style="#8a8a8a")
        else:
            t.append("  no activity yet this run\n\n", style="#4a4a4a")
            t.append("SESSION\n", style="bold #ffffff")
            stats = self._session_stats()
            t.append(f"  id        {self.session_id}\n", style="#8a8a8a")
            t.append(f"  memories  {stats['memories']} stored\n", style="#8a8a8a")
            t.append(f"  workspace {stats['files']} file(s)\n\n", style="#8a8a8a")
            t.append("HOW A RUN WORKS\n", style="bold #ffffff")
            t.append("  1. task routed to the\n     cheapest capable model\n", style="#8a8a8a")
            t.append("  2. specialists research,\n     build, decide, verify\n", style="#8a8a8a")
            t.append("  3. deliverables land in\n     workspace/\n", style="#8a8a8a")
        self.query_one("#activity", Static).update(t)

    def _session_stats(self) -> dict:
        """Cheap live stats for the idle panels: memories stored for this
        session + files currently in the shared workspace."""
        try:
            memories = len(LongTermMemory(self.session_id).entries)
        except Exception:  # noqa: BLE001
            memories = 0
        try:
            files = sum(1 for p in settings.WORKSPACE_DIR.rglob("*") if p.is_file())
        except Exception:  # noqa: BLE001
            files = 0
        return {"memories": memories, "files": files}

    def _render_rawline(self) -> None:
        """Latest raw event line + hint (the trace itself lives behind /logs)."""
        t = Text()
        if self.raw:
            last = self.raw[-1]
            t.append("▸ ", style="#4a4a4a")
            t.append_text(last)
        else:
            t.append("raw trace: /logs", style="#3d3d3d")
        self.query_one("#rawline", Static).update(t)

    def _render_cost(self) -> None:
        label = "real usage" if self.real_usage else "est"
        qa_glyph = {"waiting": "○", "done": "✓", "failed": "✕", "skipped": "–"}[self.qa_state]
        qa_style = {"waiting": "#4a4a4a", "done": "#3fb950", "failed": "#f85149", "skipped": "#4a4a4a"}[self.qa_state]
        files = ", ".join(self.artifacts) if self.artifacts else "-"
        self.query_one("#costbar").update(
            f"[cost-ok]✓[/] [cost-num]{len(self.sources)}[/] [cost-label]sources   "
            f"[/][cost-num]{len(self.findings)}[/] [cost-label]findings   "
            f"[/][{qa_style}]{qa_glyph}[/] [cost-label]qa   "
            f"[/][cost-label]tokens({label})[/] [cost-num]{self.tokens_in:,}[/][cost-label]/"
            f"[/][cost-num]{self.tokens_out:,}[/]   "
            f"[cost-label]cost[/] [cost-num]${self.cost_usd:.4f}[/]   "
            f"[cost-label]files[/] [cost-files]{files}[/]"
        )

    # ---- raw trace ----------------------------------------------------------
    def _raw(self, prefix: str, body: str, style: str = "#8a8a8a") -> None:
        t = Text()
        t.append(f"{time.strftime('%H:%M:%S')} ", style="#3d3d3d")
        t.append(prefix + " ", style=style)
        t.append(body)
        self.raw.append(t)

    def _note(self, line: str) -> None:
        self.timeline.append(f"{time.strftime('%H:%M:%S')}  {line}")

    def _set_stage(self, key: str, state: str, stat: str = "") -> None:
        self.stage_state[key] = state
        if stat:
            self.stage_stats[key] = stat

    def _set_agent(self, name: str, state: str, action: str | None = None) -> None:
        if name not in self.agents:
            return
        self.agents[name]["state"] = state
        if action:
            self.agents[name]["action"] = action[:40]

    # ---- event stream ------------------------------------------------------
    def handle_event(self, ev: dict) -> None:
        t = ev["type"]

        if t == "routed":
            self.status, self.tier, self.model = "planning", ev["tier"], ev["model"]
            self._set_stage("plan", "active")
            self._set_agent("planner", "working", "planning the approach")
            mode = ev.get("mode", "sorting")
            self._raw("[route]", f"tier={ev['tier']} model={ev['model']} house={mode} · {ev['reason']}")
            self._note(f"routed → {ev['tier']} · {ev['model']}")

        elif t == "token":
            self.token_buf += ev["content"]
            self.streamed_text += ev["content"]
            if len(self.token_buf) > 200 or ev["content"].rstrip().endswith(("\n", ".", "?", "!")):
                self._raw("|", self.token_buf.strip())
                self.token_buf = ""
                self._render_rawline()
            # Tokens arrive fast — repaint only the workspace, not every panel.
            self._render_workspace()
            return

        elif t == "agent":
            self._set_agent(ev["name"], "working", ev["message"])
            self._raw(f"[{ev['name']}]", ev["message"], style="#56d3a6")
            self._note(f"{ev['name']}: {ev['message'][:60]}")

        elif t == "handoff":
            to = ev["to"]
            if to == "research":
                self._set_stage("plan", "done")
                self._set_stage("research", "active")
            elif to == "executor":
                for k in ("plan", "research"):
                    if self.stage_state[k] != "done":
                        self._set_stage(k, "skipped")
                self._set_stage("build", "active")
            elif to == "qa":
                for k in ("plan", "research", "build"):
                    if self.stage_state[k] == "active":
                        self._set_stage(k, "done")
                self._set_stage("review", "active")
            self._set_agent(to, "working", "just started")
            self._raw("[handoff]", f"→ {to}", style="#79c0ff")
            self._note(f"delegated → {to}")

        elif t == "status":
            self.status = "running"
            self.step = ev["step"]
            agent = ev["agent"]
            text = ev["text"]
            self._set_agent(agent, "working", text)
            # tool-derived stage hints for work done without a formal handoff
            if text.startswith("Searching the web") or text.startswith("Research agent"):
                if self.stage_state["research"] == "waiting":
                    self._set_stage("plan", "done")
                    self._set_stage("research", "active")
                self._set_agent("research", "working", text)
            elif "Writing deliverable" in text:
                self._set_stage("build", "active")
                self._set_agent("executor", "working", text)
            self._raw(f"#{ev['step']}", f"[{agent}] {text}", style="#e3b341")
            self._note(f"#{ev['step']} [{agent}] {text[:55]}")

        elif t == "visit":
            url = ev["url"]
            if url not in self.sources:
                self.sources.append(url)
            self._set_stage("research", "active" if self.stage_state["research"] != "done" else "active",
                            f"{len(self.sources)} sources opened")
            self._raw("⌕ visit", url)
            self._note(f"opened {url[:48]}")

        elif t == "collected":
            self.char_count += ev["chars"]
            self.findings.append(ev["preview"][:80])
            self._set_stage("research", "active", f"{len(self.findings)} findings · {self.char_count:,} chars")
            self._raw("▤ data", f"+{ev['chars']:,} chars", style="#d2a8ff")
            self._note(f"collected +{ev['chars']:,} chars")

        elif t == "tool_result" and not ev["ok"]:
            self._set_agent(ev["agent"], "attention", f"{ev['name']} failed")
            self._raw("✗ failed", f"{ev['name']} — retrying", style="#f85149")
            self._note(f"{ev['name']} failed → retry")

        elif t == "artifacts":
            self.artifacts = list(dict.fromkeys(self.artifacts + ev["files"]))
            self._set_stage("build", "done", f"{len(self.artifacts)} file(s)")

        elif t == "cost":
            self.tokens_in, self.tokens_out = ev["tokens_in"], ev["tokens_out"]
            self.cost_usd = ev["est_cost_usd"]
            self.real_usage = bool(ev.get("real_usage"))
            self._raw("[cost]", f"{ev['tokens_in']} in / {ev['tokens_out']} out · real={ev.get('real_usage')}")

        elif t == "warning":
            if "QA" in ev["message"]:
                self.qa_state, self.qa_note = "failed", ev["message"]
            self._raw("⚠", ev["message"], style="#e3b341")
            self._note(f"⚠ {ev['message'][:60]}")

        elif t == "retry":
            self.status = "retrying"
            self._raw("[retry]", f"attempt {ev['attempt']} in {int(ev['wait'])}s — {ev.get('reason','')[:70]}", style="#e3b341")
            self._note(f"↻ recovery — retry {ev['attempt']} in {int(ev['wait'])}s")

        elif t == "complete":
            self.status = "done"
            if ev.get("artifacts"):
                self.artifacts = list(dict.fromkeys(self.artifacts + ev["artifacts"]))
            final = (ev.get("final") or "").strip()
            self.final_md = Markdown(final) if final else None
            qa = ev.get("qa_verified")
            if qa is True:
                self.qa_state = "done"
                self._set_stage("review", "done", "verified")
                self._set_agent("qa", "done", "PASS")
            elif qa is False:
                self.qa_state = "failed"
                self._set_stage("review", "failed", "not reviewed")
            else:
                self._set_stage("review", "skipped")
            self._set_stage("deliver", "done")
            for key, _, _ in STAGES:
                if self.stage_state[key] in ("waiting", "active"):
                    self._set_stage(key, "skipped")
            for name, meta in AGENT_META.items():
                if self.agents[name]["state"] == "working":
                    self._set_agent(name, "done")
            if ev.get("budget_exhausted"):
                self._note("budget exhausted — shipped what was complete")
            self._raw("[done]", "run complete", style="#3fb950")
            self._note("✓ run complete")

        elif t == "error":
            self.status = "error"
            self.error_text = ev["message"]
            self._raw("✗ error", ev["message"][:200], style="#f85149")
            self._note(f"✗ error")

        self._render_all()

    # ---- task driver (sequential REPL) -------------------------------------
    async def _drive(self) -> None:
        """Run queued prompts one at a time; the Input stays live between runs."""
        if self._driving:
            return
        self._driving = True
        try:
            while self.pending:
                prompt = self.pending.pop(0)
                self._reset_run(prompt)
                self.status = "running"
                self._raw("══ TASK", prompt, style="bold #ffffff")
                self._render_all()
                async for ev in run_agent(prompt, self.session_id):
                    self.handle_event(ev)
                self.status = "idle" if self.status == "done" else self.status
                self._render_all()
        finally:
            self._driving = False

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        prompt = event.value.strip()
        self.query_one("#prompt").value = ""
        self._hide_suggestions()
        if not prompt:
            return

        # opencode-style completion: unique-prefix commands complete on Enter.
        if is_command(prompt) and prompt not in {c.split()[0] for c in COMMANDS} | {"/?"}:
            from agent.commands import SUGGESTIONS

            matches = [n for n, _ in SUGGESTIONS if n.startswith(prompt)]
            if len(matches) == 1:
                self.query_one("#prompt", Input).value = matches[0] + " "
                self._suggestions().refresh_for(matches[0])
                self.query_one("#prompt", Input).focus()
                return
            if not matches:
                self._note(f"unknown command: {prompt}")
                self._render_activity()
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
            self._note(f"queued: {prompt[:50]}")
            self._render_activity()
        self.query_one("#prompt").focus()

    # ---- "/" autocomplete popup ------------------------------------------------
    def _suggestions(self) -> SuggestionOverlay:
        return self.query_one("#suggestions", SuggestionOverlay)

    def _hide_suggestions(self) -> None:
        self._suggestions().display = False

    def on_input_changed(self, event: Input.Changed) -> None:
        overlay = self._suggestions()
        text = event.value
        if text.startswith("/") and "\n" not in text:
            overlay.refresh_for(text)
        else:
            overlay.display = False

    def key_tab(self) -> None:
        """Legacy hook — Tab is handled by the priority binding below."""

    def action_cycle_house(self) -> None:
        """Tab = complete an open suggestion, or cycle house modes."""
        if isinstance(self.screen, ModalScreen):
            return
        try:
            overlay = self._suggestions()
        except Exception:  # noqa: BLE001
            overlay = None
        if overlay is not None and overlay.visible():
            name = overlay.selected_name()
            if name:
                self.query_one("#prompt", Input).value = name + " "
                overlay.refresh_for(name)
            return
        house = next_mode()
        self._apply_mode(f"Tab → {house.glyph} {house.name} ({house.advantage})")

    def _apply_mode(self, note: str = "") -> None:
        # House switches update the banner + panels but intentionally stay
        # OUT of the ACTIVITY timeline (it's for run events, not settings).
        self.query_one("#banner").update(self._banner_text())
        self._render_agents()
        self._render_status()

    def key_enter(self) -> None:
        overlay = self._suggestions()
        if overlay.visible():
            text = self.query_one("#prompt", Input).value.strip()
            name = overlay.selected_name()
            if name and text != name:
                self.query_one("#prompt", Input).value = name + " "
                overlay.refresh_for(name)
                return
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
            self._note("/help")
            for line in help_lines():
                self._note("   " + line.strip())

        elif cmd == "/clear":
            self.raw.clear()
            self._note("log cleared")

        elif cmd == "/status":
            self._note(f"status={self.status} tier={self.tier} house={get_mode_key()} queued={len(self.pending)}")

        elif cmd == "/cost":
            self._note(f"tokens {self.tokens_in:,}/{self.tokens_out:,} · ${self.cost_usd:.6f}")

        elif cmd == "/files":
            if self.artifacts:
                for f in self.artifacts:
                    self._note(f"▸ {f}")
            else:
                self._note("no files written in this run")

        elif cmd == "/team":
            for name, meta in AGENT_META.items():
                st = self.agents[name]
                self._note(f"{meta['glyph']} {name:<9s} {st['state']:<8s} {st['action']}")

        elif cmd in ("/memory", "/mem"):
            self.push_screen(MemoryScreen(self.session_id, query=arg))

        elif cmd in ("/sessions", "/session", "/open"):
            if arg:
                self._switch_session(arg)
            else:
                self.push_screen(SessionPickerScreen(self.session_id), self._switch_session)

        elif cmd in ("/mode", "/house"):
            if arg:
                house = set_mode(arg)
                if house.key == "sorting" and arg.lower() not in ("sorting", "neutral", "none"):
                    self._note(f"unknown house: {arg}")
                else:
                    self._apply_mode(f"advantage: {house.advantage}")
            else:
                self.push_screen(HousePickerScreen(get_mode_key()), self._switch_mode)

        elif cmd == "/logs":
            self.push_screen(LogScreen(self.raw))

        elif cmd in ("/exit", "/quit", "/q"):
            self.exit()
            return

        else:
            self._note(f"unknown command: {cmd} — try /help")

        self._render_activity()
        self._render_status()

    # ---- session / mode switching ---------------------------------------------
    def _switch_session(self, session_id: str | None) -> None:
        from agent.commands import list_sessions

        target = (session_id or "").strip()
        if not target:
            return
        known = {s["id"] for s in list_sessions()}
        is_new = target not in known
        if self._driving:
            self._note("! a task is running — finish it before switching sessions")
            self._render_activity()
            return
        self.session_id = target
        self._reset_run("")
        verb = "started new session" if is_new else "resumed session"
        self._note(f"■ {verb}: {target}")
        self._render_all()
        self.query_one("#prompt", Input).focus()

    def _switch_mode(self, house_key: str | None) -> None:
        if not house_key:
            return
        house = set_mode(house_key)
        self._apply_mode(f"advantage: {house.advantage}")


def smoke() -> None:
    """Binary self-test (like `codex doctor`): verifies the packaged bundle
    imports cleanly and the agent graph compiles. Makes NO API calls."""
    from agent.config import PROJECT_ROOT, settings
    from agent.core.agent_factory import build_agent
    from agent.memory.long_term import LongTermMemory
    from agent.tools.registry import build_all_tools

    print(f"[smoke] data dir: {PROJECT_ROOT}")
    tools = build_all_tools("smoke")
    print(f"[OK] {len(tools)} tools registered: " + ", ".join(t.name for t in tools))
    memory = LongTermMemory("smoke")
    print(f"[OK] memory store reachable ({len(memory.entries)} entries)")
    build_agent("smoke", tier="simple")
    print("[OK] deep agent graph compiled (no API call made)")
    print(f"[OK] workspace: {settings.WORKSPACE_DIR}")
    print(f"[OK] edit root: {settings.EDIT_ROOT}")
    try:
        import httpx

        resp = httpx.get("https://api.tokenrouter.com", timeout=15.0)
        print(f"[OK] TLS handshake + HTTPS works (HTTP {resp.status_code})")
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] TLS/HTTPS check failed: {exc}")


def main() -> None:
    import sys

    args = sys.argv[1:]
    if "--smoke" in args:
        smoke()
        return
    session_id = "tui"
    if "--session" in args:
        i = args.index("--session")
        if i + 1 < len(args):
            session_id = args[i + 1]
            args = args[:i] + args[i + 2 :]
    initial = " ".join(args) or None
    AgentLegacyApp(session_id=session_id, initial_prompt=initial).run()


if __name__ == "__main__":
    main()
