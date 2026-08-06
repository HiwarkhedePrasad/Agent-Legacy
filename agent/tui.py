"""Rich terminal UI: a live dashboard for the AI Operations Center.

Renders the agent's event stream as a scrolling narration log, a browsing
feed (every website visited), collected-data previews, team activity, and a
live token/cost footer.
"""

from __future__ import annotations

import asyncio
import sys
import time

try:  # allow emoji/glyphs even on legacy Windows codepages
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from agent.services.runner import run_agent

console = Console()

MAX_LOG = 500


def _tier_style(tier: str) -> str:
    return {"simple": "green", "medium": "yellow", "complex": "magenta"}.get(tier, "white")


class TUI:
    def __init__(self) -> None:
        self.start = time.time()
        self.routed: dict = {}
        self.log: list = []
        self.browsed: list[str] = []
        self.collected: list[tuple[str, str]] = []
        self.activity: list[tuple[str, str]] = []
        self.artifacts: list[str] = []
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.pending_tokens = ""
        self.status_text = "idle"
        self.final_text = ""
        self.error: str | None = None

    # ---- state updates -----------------------------------------------------
    def _add_log(self, style: str, text: str) -> None:
        self.log.append(Text.from_markup(text, style=style))
        if len(self.log) > MAX_LOG:
            self.log = self.log[-MAX_LOG:]

    def consume(self, ev: dict) -> None:
        t = ev["type"]
        if t == "routed":
            self.routed = ev
            self.status_text = "planning"
            self._add_log(
                "cyan",
                f"[route] tier={ev['tier']} model={ev['model']} — {ev['reason']}",
            )
        elif t == "token":
            self.pending_tokens += ev["content"]
            if len(self.pending_tokens) > 140 or ev["content"].rstrip().endswith(("\n", ".", "?", "!")):
                self._add_log("bright_white", self.pending_tokens.strip())
                self.pending_tokens = ""
        elif t == "agent":
            self._add_log("bold green", f"[{ev['name']}] {ev['message']}")
        elif t == "handoff":
            self.activity.append((ev["to"], time.strftime("%H:%M:%S")))
            self._add_log("bold blue", f">>> delegating to {ev['to']}")
        elif t == "status":
            self.status_text = ev["text"]
            self._add_log("yellow", f">> #{ev['step']} [{ev['agent']}] {ev['text']}")
        elif t == "visit":
            self.browsed.append(ev["url"])
            self._add_log("blue", f"  -> visited: {ev['url']}")
        elif t == "collected":
            self.collected.append((ev["source"], ev["preview"]))
            self._add_log("magenta", f"  [data] collected {ev['chars']} chars")
        elif t == "tool_result" and not ev["ok"]:
            self._add_log("bold red", f"  !! tool {ev['name']} FAILED — retrying...")
        elif t == "artifacts":
            self.artifacts = ev["files"]
        elif t == "cost":
            self.tokens_in, self.tokens_out = ev["tokens_in"], ev["tokens_out"]
            self.cost_usd = ev["est_cost_usd"]
        elif t == "complete":
            self.final_text = ev["final"]
            self.status_text = "done"
            self._add_log("bold green", f"[done] {ev['final']}")
        elif t == "error":
            self.error = ev["message"]
            self.status_text = "error"
            self._add_log("bold red", f"[error] {ev['message']}")

    # ---- rendering ---------------------------------------------------------
    def _log_panel(self) -> Panel:
        if self.pending_tokens:
            head = [Text(self.pending_tokens.strip(), style="bright_white")]
        else:
            head = []
        body = Group(*head, *self.log[-28:])
        return Panel(
            body,
            title="[bold]AI Operations Center — live[/]",
            subtitle=f"{len(self.log)} lines",
            border_style="cyan",
        )

    def _side_panel(self) -> Panel:
        rows = []
        if self.browsed:
            rows.append(Text("BROWSING", style="bold blue"))
            for url in self.browsed[-6:]:
                rows.append(Text(f"  -> {url}", style="blue", overflow="ellipsis"))
        if self.collected:
            rows.append(Text("COLLECTED", style="bold magenta"))
            for src, prev in self.collected[-4:]:
                rows.append(Text(f"  [data] {prev[:60]}", style="magenta", overflow="ellipsis"))
        if self.activity:
            rows.append(Text("TEAM", style="bold green"))
            for name, ts in self.activity[-6:]:
                rows.append(Text(f"  >> {name:9s} {ts}", style="green"))
        if not rows:
            rows.append(Text("Waiting for activity...", style="dim"))
        return Panel(Group(*rows), title="[bold]Team & Data[/]", border_style="green")

    def _footer(self) -> Table:
        table = Table.grid(padding=(0, 1))
        model = self.routed.get("model", "-")
        table.add_column(justify="left")
        table.add_column(justify="right")
        table.add_row(
            f"[bold]model[/] {model}",
            f"[bold]tokens[/] {self.tokens_in} in / {self.tokens_out} out",
        )
        files = ", ".join(self.artifacts) or "-"
        table.add_row(
            f"[bold]files[/] {files}",
            f"[bold]cost[/] ${self.cost_usd:.6f}",
        )
        return table

    def render(self) -> Layout:
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=4),
        )
        layout["main"].split_row(
            Layout(name="log", ratio=2),
            Layout(name="side", ratio=1),
        )

        header = Text.assemble(
            ("AI OPERATIONS CENTER", "bold white on blue"),
            "   ",
            (self.status_text.upper(), "bold"),
            "   ",
            (
                f"tier: {self.routed.get('tier', '-')}",
                _tier_style(self.routed.get("tier", "")),
            ),
            f"   elapsed: {int(time.time() - self.start)}s",
        )
        layout["header"].update(Panel(header, border_style="bright_blue"))
        layout["log"].update(self._log_panel())
        layout["side"].update(self._side_panel())
        layout["footer"].update(Panel(self._footer(), border_style="dim"))
        return layout


async def _main(prompt_input: str, session_id: str = "tui") -> None:
    tui = TUI()
    with Live(tui.render(), console=console, refresh_per_second=12, screen=True) as live:
        async for ev in run_agent(prompt_input, session_id):
            tui.consume(ev)
            live.update(tui.render())
    console.print()


def main() -> None:
    import sys

    prompt_input = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None
    if not prompt_input:
        prompt_input = console.input("[bold cyan]What should the AI Operations Center do? [/]")
    asyncio.run(_main(prompt_input))


if __name__ == "__main__":
    main()