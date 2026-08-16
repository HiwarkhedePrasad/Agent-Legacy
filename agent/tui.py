"""Rich terminal UI: a live dashboard for Agent-Legacy.

Renders the agent's event stream as a scrolling narration log, a browsing
feed (every website visited), collected-data previews, team activity, and a
live token/cost footer.

Scrolling: the log is a scrollback buffer. While running it follows the latest
line, but you can scroll back (up/down arrows, PgUp/PgDn, Home/End, or the
mouse wheel). Scrolling UP freezes the view so you can read; scrolling all the
way back to the bottom resumes auto-follow. Only the visible slice is
re-rendered each frame so the UI stays smooth.
"""

from __future__ import annotations

import asyncio
import os
import queue
import sys
import threading
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

MAX_LOG = 3000


def _win_enable_vt_input() -> None:
    """Best-effort: enable virtual-terminal input on a Windows console."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = wintypes.DWORD()
        if k32.GetConsoleMode(h, ctypes.byref(mode)):
            k32.SetConsoleMode(h, mode.value | _ENABLE_VT_INPUT)
    except Exception:  # noqa: BLE001
        pass


def _enable_mouse() -> None:
    _win_enable_vt_input()
    try:
        os.write(1, _MOUSE_ON)
    except Exception:  # noqa: BLE001
        pass


def _disable_mouse() -> None:
    try:
        os.write(1, _MOUSE_OFF)
    except Exception:  # noqa: BLE001
        pass

# ---- input handling ------------------------------------------------------
# Each event is a tuple; the first element is the kind:
#   ("scroll", delta)  delta>0 = up/back, delta<0 = down/toward-latest
#   ("page", dir)      dir=+1 up a page, -1 down a page
#   ("home",)          jump to top of scrollback
#   ("end",)           jump to bottom (resume auto-follow)


def _map_win_scan(code: str) -> tuple | None:
    """Map the 2nd char of a Windows console extended key (after \\xe0)."""
    return {
        "H": ("scroll", 1),   # Up
        "P": ("scroll", -1),  # Down
        "I": ("page", 1),     # PgUp
        "Q": ("page", -1),    # PgDn
        "G": ("home",),       # Home
        "O": ("end",),        # End
    }.get(code)


def _map_vt(seq: str) -> tuple | None:
    """Map a collected escape sequence (arrows / PgUp / PgDn / SGR wheel)."""
    if seq.startswith("\x1b["):
        body = seq[2:]
    elif seq.startswith("\x1bO"):
        body = "SS3" + seq[2:]
    else:
        return None

    if body in ("A", "SS3A"):
        return ("scroll", 1)     # Up
    if body in ("B", "SS3B"):
        return ("scroll", -1)    # Down
    if body == "5~":
        return ("page", 1)       # PgUp
    if body == "6~":
        return ("page", -1)      # PgDn
    if body in ("H", "1~", "SS3H"):
        return ("home",)         # Home
    if body in ("F", "4~", "SS3F"):
        return ("end",)          # End
    if body.startswith("<"):
        # SGR mouse report: CSI < button ; col ; row M (or m on release)
        try:
            button = int(body[1:].split(";", 1)[0])
        except Exception:  # noqa: BLE001
            return None
        if button == 64:
            return ("scroll", 3)   # wheel up -> scroll back
        if button == 65:
            return ("scroll", -3)  # wheel down -> toward latest
        return None                # clicks / left-right wheel -> ignore
    return None


def _collect_seq() -> str:
    """Read the rest of an ESC sequence after the leading \\x1b byte."""
    import msvcrt

    seq = "\x1b"
    deadline = time.time() + 0.5
    while time.time() < deadline and len(seq) < 16:
        if msvcrt.kbhit():
            c = msvcrt.getwch()
            seq += c
            # Stop on a likely terminator: 'm'/'M' (mouse), '~' (PgUp/PgDn/Home/End).
            if c in ("m", "M", "~") or (not c.isdigit() and c not in ";[<>"):
                break
        else:
            time.sleep(0.004)
    return seq


def _input_thread(tui: "TUI") -> None:
    """Background reader translating keys/wheel into scroll commands."""
    if sys.platform == "win32":
        import msvcrt

        while True:
            try:
                ch = msvcrt.getwch()
                if ch in ("\xe0", "\x00"):
                    code = msvcrt.getwch()
                    ev = _map_win_scan(code)
                    if ev:
                        tui.on_input(ev)
                elif ch == "\x1b":
                    ev = _map_vt(_collect_seq())
                    if ev:
                        tui.on_input(ev)
                else:
                    tui.on_key_char(ch)  # printable / Backspace / Enter -> prompt box
            except Exception:  # noqa: BLE001
                # Never let one bad byte kill the reader thread.
                time.sleep(0.01)
    else:
        _unix_input_thread(tui)


def _unix_input_thread(tui: "TUI") -> None:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # raw-ish: every key is delivered immediately
        buf = b""
        while True:
            r, _, _ = select.select([sys.stdin], [], [], 0.05)
            if not r:
                continue
            buf += sys.stdin.buffer.read(1)
            if buf.startswith(b"\x1b"):
                deadline = time.time() + 0.2
                while time.time() < deadline and len(buf) < 16:
                    r, _, _ = select.select([sys.stdin], [], [], 0.01)
                    if not r:
                        break
                    buf += sys.stdin.buffer.read(1)
                ev = _map_vt(buf.decode("utf-8", "replace"))
                if ev:
                    tui.on_input(ev)
                buf = b""
            elif buf == b"\xe0":
                buf = b""  # rare on unix: extended-key prefix, ignore
            else:
                # Single plain key byte -> feed the prompt box.
                tui.on_key_char(buf.decode("utf-8", "replace"))
                buf = b""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _tier_style(tier: str) -> str:
    return {"simple": "green", "medium": "dark_orange", "complex": "bold white"}.get(tier, "white")


class TUI:
    # Liveness heartbeat glyphs — cycles while the agent is running so the
    # operator can see the system is alive even during a long but healthy run.
    _HEARTBEAT = "◐◑◒◓"

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
        self.scroll = 0           # lines scrolled up from the bottom (0 = follow)
        self.view_height = 28     # rows available in the log panel body
        self._heartbeat_frame = 0  # cycles through _HEARTBEAT while running

        # Persistent REPL input (bottom bar). The input thread writes here as the
        # user types; Enter submits a task into the queue; 'exit' quits.
        self.input_buf = ""
        self.submits: queue.Queue = queue.Queue()

    # ---- persistent REPL input ---------------------------------------------
    def on_key_char(self, ch: str) -> None:
        """Handle a single typed character (printable, Backspace, Enter)."""
        if ch in ("\r", "\n"):  # Enter: submit the current line as a task
            prompt = self.input_buf.strip()
            self.input_buf = ""
            if prompt:
                self.submits.put(prompt)
        elif ch in ("\x7f", "\x08"):  # Backspace
            self.input_buf = self.input_buf[:-1]
        elif ch.isprintable() or ch in (" ", "\t"):
            self.input_buf += ch

    def begin_run(self, prompt: str) -> None:
        """Reset per-run counters and mark a fresh task boundary in the log."""
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.pending_tokens = ""
        self.status_text = "planning"
        self.error = None
        self.final_text = ""
        self.scroll = 0
        self._add_log("bold white", f"========== NEW TASK: {prompt} ==========")

    def _tick_heartbeat(self) -> None:
        """Advance the liveness heartbeat one frame (called from render)."""
        if self.status_text not in ("idle", "done", "error"):
            self._heartbeat_frame = (self._heartbeat_frame + 1) % len(self._HEARTBEAT)

    def _heartbeat_glyph(self) -> str:
        """Current heartbeat character, or empty string when idle/done/error."""
        if self.status_text in ("idle", "done", "error"):
            return ""
        return self._HEARTBEAT[self._heartbeat_frame]

    def _input_bar(self) -> Panel:
        cursor = "█" if (int(time.time() * 2) % 2 == 0) else " "
        return Panel(
            Text(f" > {self.input_buf}{cursor}", style="bold bright_green"),
            title="[bold]INPUT — type a task, Enter to run, type 'exit' to quit[/]",
            border_style="green",
        )

    # ---- state updates -----------------------------------------------------
    def _add_log(self, style: str, text: str) -> None:
        self.log.append(Text.from_markup(text, style=style))
        if len(self.log) > MAX_LOG:
            self.log = self.log[-MAX_LOG:]
            if self.scroll > MAX_LOG:
                self.scroll = MAX_LOG

    def set_viewport(self, console_height: int) -> None:
        # header(3) + footer(5) + input bar(3) + panel borders/title (~4)
        self.view_height = max(5, console_height - 15)

    def _max_scroll(self) -> int:
        return max(0, len(self.log) - self.view_height)

    def on_input(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "scroll":
            self.scroll = min(self._max_scroll(), max(0, self.scroll + ev[1]))
        elif kind == "page":
            self.scroll = min(self._max_scroll(), max(0, self.scroll + ev[1] * self.view_height))
        elif kind == "home":
            self.scroll = self._max_scroll()
        elif kind == "end":
            self.scroll = 0

    def consume(self, ev: dict) -> None:
        t = ev["type"]
        if t == "routed":
            self.routed = ev
            self.status_text = "planning"
            self._add_log(
                "bold white",
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
            self._add_log("bold white", f">>> delegating to {ev['to']}")
        elif t == "status":
            self.status_text = ev["text"]
            self._add_log("dark_orange", f">> #{ev['step']} [{ev['agent']}] {ev['text']}")
        elif t == "visit":
            self.browsed.append(ev["url"])
            self._add_log("grey74", f"  -> visited: {ev['url']}")
        elif t == "collected":
            self.collected.append((ev["source"], ev["preview"]))
            self._add_log("dark_orange", f"  [data] collected {ev['chars']} chars")
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
    def _visible_log(self) -> list:
        """Return the log slice for the current viewport + scroll offset."""
        total = len(self.log)
        self.scroll = min(self.scroll, self._max_scroll())
        end = max(0, total - self.scroll)
        start = max(0, end - self.view_height)
        return self.log[start:end]

    def _scrollbar(self) -> Text:
        """Draw a vertical scrollbar (thumb + track + top/bottom markers)."""
        total = len(self.log)
        view = self.view_height
        if total <= view:
            # Nothing to scroll yet: show an empty track.
            return Text("\n".join(["│"] * view), style="dim")

        max_scroll = total - view
        self.scroll = min(self.scroll, max_scroll)

        # Thumb length proportional to the fraction of content visible.
        thumb_len = max(1, round(view * (view / total)))
        thumb_len = min(thumb_len, view - 2)

        frac = self.scroll / max_scroll if max_scroll else 0.0
        thumb_top = round(frac * (view - thumb_len))

        rows: list[tuple[str, str]] = []
        for i in range(view):
            if thumb_top <= i < thumb_top + thumb_len:
                rows.append(("█", "bright_white"))
            else:
                rows.append(("│", "dim"))

        # Top/bottom indicators: more content above / below the visible window.
        # ▲ = older lines above (can scroll up) ; ▼ = newer lines below (can
        # scroll down toward latest).
        if self.scroll < max_scroll:
            rows[0] = ("▲", "bold white")
        if self.scroll > 0:
            rows[-1] = ("▼", "bold white")

        bar = Text()
        for i, (ch, style) in enumerate(rows):
            bar.append(ch, style=style)
            if i < len(rows) - 1:
                bar.append("\n", style=style)
        return bar

    def _log_panel(self) -> Panel:
        visible = self._visible_log()
        parts: list = list(visible)
        if self.pending_tokens and self.scroll == 0:
            parts.append(Text(self.pending_tokens.strip(), style="bright_white"))
        if not parts:
            parts.append(Text("  [ waiting for activity... ]", style="dim"))

        title = "[bold]Agent-Legacy — live[/]"
        if self.scroll > 0:
            title = f"[bold]Agent-Legacy — scrollback {self.scroll} lines↑[/]"

        grid = Table.grid(expand=True)
        grid.add_column(ratio=1, overflow="fold")
        grid.add_column(width=2, justify="center", vertical="middle")
        grid.add_row(Group(*parts), self._scrollbar())

        return Panel(
            grid,
            title=title,
            subtitle=f"{len(self.log)} lines",
            border_style="white",
        )

    def _side_panel(self) -> Panel:
        rows = []
        if self.browsed:
            rows.append(Text("BROWSING", style="bold white"))
            for url in self.browsed[-6:]:
                rows.append(Text(f"  -> {url}", style="grey74", overflow="ellipsis"))
        if self.collected:
            rows.append(Text("COLLECTED", style="bold dark_orange"))
            for src, prev in self.collected[-4:]:
                rows.append(Text(f"  [data] {prev[:60]}", style="dark_orange", overflow="ellipsis"))
        if self.activity:
            rows.append(Text("TEAM", style="bold green"))
            for name, ts in self.activity[-6:]:
                rows.append(Text(f"  >> {name:9s} {ts}", style="green"))
        if not rows:
            rows.append(Text("Waiting for activity...", style="dim"))
        return Panel(Group(*rows), title="[bold]Team & Data[/]", border_style="green")

    def _footer(self) -> Group:
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
        mode = "[bold]LIVE (following)[/]" if self.scroll == 0 else f"[bold]scrollback {self.scroll}↑[/]"
        hint = Text(
            f"{mode}   scroll: ↑/↓  PgUp/PgDn  Home/End  mouse wheel",
            style="dim",
            overflow="ellipsis",
        )
        return Group(table, hint)

    def render(self) -> Layout:
        self._tick_heartbeat()
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=5),
            Layout(name="input", size=3),
        )
        layout["main"].split_row(
            Layout(name="log", ratio=2),
            Layout(name="side", ratio=1),
        )

        header = Text.assemble(
            ("AGENT-LEGACY", "bold white"),
            "   ",
            (self.status_text.upper(), "bold"),
            (" " + self._heartbeat_glyph(), "bold white"),
            "   ",
            (
                f"tier: {self.routed.get('tier', '-')}",
                _tier_style(self.routed.get("tier", "")),
            ),
            f"   elapsed: {int(time.time() - self.start)}s",
        )
        layout["header"].update(Panel(header, border_style="white"))
        layout["log"].update(self._log_panel())
        layout["side"].update(self._side_panel())
        layout["footer"].update(Panel(self._footer(), border_style="dim"))
        layout["input"].update(self._input_bar())
        return layout


async def _main(session_id: str = "tui", initial_prompt: str | None = None) -> None:
    tui = TUI()
    if initial_prompt:
        tui.submits.put(initial_prompt)
    _enable_mouse()
    reader = threading.Thread(target=_input_thread, args=(tui,), daemon=True)
    reader.start()
    try:
        with Live(tui.render(), console=console, refresh_per_second=12, screen=True) as live:
            while True:
                # Re-render the dashboard + input box every frame while idle so
                # typed text appears live. (Living on the auto-refresh alone
                # would re-draw a stale snapshot and hide your typing.)
                tui.set_viewport(console.height)
                live.update(tui.render())
                try:
                    prompt_input = tui.submits.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.05)
                    continue
                if prompt_input.strip().lower() in ("exit", "quit", "q", "bye"):
                    break
                tui.begin_run(prompt_input)
                async for ev in run_agent(prompt_input, session_id):
                    tui.consume(ev)
                    tui.set_viewport(console.height)
                    live.update(tui.render())
    except KeyboardInterrupt:
        pass
    finally:
        _disable_mouse()
    console.print()


def main() -> None:
    import sys

    initial = " ".join(sys.argv[1:]) or None
    asyncio.run(_main(session_id="tui", initial_prompt=initial))


if __name__ == "__main__":
    main()

