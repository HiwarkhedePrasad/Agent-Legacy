"""Slash commands + interactive overlays for the Agent-Legacy dashboard.

Three pieces of UI, all opencode-style:

1.  Autocomplete popup — typing "/" in the input bar opens a floating list of
    matching commands; keep typing to filter, Up/Down + Enter to pick one.
2.  Memory browser — `/memory` opens a side-panel screen listing long-term
    memory entries; scroll to switch between them, filter by query, press
    Escape to return.
3.  Session picker — `/sessions` lists every past session (memory store) on
    disk with its entry count, last activity and most recent task; click or
    Enter to open one and continue it.
"""
from __future__ import annotations

import datetime
import json

from rich.markdown import Markdown
from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, ListItem, ListView, Static

from agent.config import settings
from agent.memory.long_term import LongTermMemory
from agent.modes import CYCLE_ORDER, HOUSES

COMMANDS: dict[str, str] = {
    "/help": "list all commands",
    "/clear": "clear the log screen",
    "/status": "show status, routed tier and model",
    "/cost": "show token counts and estimated cost",
    "/files": "list deliverables written to the workspace",
    "/team": "show recent team handoffs",
    "/memory": "open the memory browser",
    "/sessions": "open the session picker (resume a past session)",
    "/mode": "switch house mode (Tab also cycles)",
    "/logs": "open the raw execution trace (debug view)",
    "/exit": "quit the app  (aliases: /quit /q)",
}

# Flat (name, hint) pairs for the "/" autocomplete popup, in display order.
SUGGESTIONS: list[tuple[str, str]] = [
    ("/help", "list all commands"),
    ("/clear", "clear the log screen"),
    ("/status", "status, tier & model"),
    ("/cost", "tokens & estimated cost"),
    ("/files", "deliverables written"),
    ("/team", "recent handoffs"),
    ("/memory", "open memory browser"),
    ("/sessions", "open session picker"),
    ("/mode", "switch house mode"),
    ("/logs", "raw execution trace"),
    ("/exit", "quit the app"),
]


def is_command(text: str) -> bool:
    return text.startswith("/")


def help_lines() -> list[str]:
    return [f"  {name:<16s} {desc}" for name, desc in COMMANDS.items()]


def list_sessions() -> list[dict]:
    """Read every persisted session (memory store) on disk.

    Returns dicts: {id, entries, last_active, last_task, preview}
    sorted newest-first.
    """
    sessions: list[dict] = []
    try:
        for path in sorted(settings.MEMORY_DIR.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(raw, list) or not raw:
                continue
            last = raw[-1]
            ts = last.get("created_at") or 0.0
            content = str(last.get("content", ""))
            task = ""
            for line in content.splitlines():
                if line.startswith("TASK:"):
                    task = line[5:].strip()
                    break
            when = (
                datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                if ts
                else "-"
            )
            preview = task or content[:60]
            sessions.append(
                {
                    "id": path.stem,
                    "entries": len(raw),
                    "last_active": when,
                    "ts": ts,
                    "last_task": task,
                    "preview": preview,
                }
            )
    except Exception:  # noqa: BLE001
        pass
    sessions.sort(key=lambda s: s.get("ts", 0.0), reverse=True)
    return sessions


# ---- 1) "/" autocomplete overlay -------------------------------------------
class SuggestionRow(ListItem):
    """One row in the autocomplete popup."""

    def __init__(self, name: str, hint: str) -> None:
        super().__init__()
        self._name = name
        self._hint = hint

    def compose(self) -> ComposeResult:
        t = Text()
        t.append(self._name, style="bold #ffffff")
        t.append("  ")
        t.append(self._hint, style="#6e6e6e")
        yield Static(t, markup=False, classes="sugg-row")


class SuggestionOverlay(Static):
    """Floating panel that lists commands matching the current input buffer."""

    DEFAULT_CSS = """
    SuggestionOverlay {
        layer: overlay;
        dock: bottom;
        margin: 0 0 4 2;
        width: 46;
        height: auto;
        max-height: 14;
        background: #0e0e0e;
        border: solid #3a3a3a;
        padding: 0 1;
        display: none;
    }
    SuggestionOverlay .sugg-row { height: 1; width: 100%; padding: 0; }
    SuggestionOverlay ListView { background: #0e0e0e; height: auto; max-height: 12; border: none; }
    SuggestionOverlay .sugg-title { text-style: bold; color: #8a8a8a; margin-bottom: 1; }
    SuggestionOverlay ListView > ListItem { height: 1; }
    SuggestionOverlay ListView > ListItem.--highlight { background: #2a2a2a; }
    SuggestionOverlay ListView > ListItem.--sugg-hl { background: #2a2a2a; }
    """

    def __init__(self) -> None:
        super().__init__(id="suggestions")
        self._rows: list[SuggestionRow] = []
        self._hl = 0  # highlight index managed by us (never needs the ListView's focus)

    def compose(self) -> ComposeResult:
        yield Static("COMMANDS", classes="sugg-title")
        yield ListView(id="sugg-list", disabled=True)

    # Called by the app whenever the prompt text changes.
    def refresh_for(self, text: str) -> None:
        text = text.strip()
        if not text.startswith("/"):
            self.display = False
            return
        matches = [(n, h) for n, h in SUGGESTIONS if n.startswith(text)]
        if not matches:
            matches = [(n, h) for n, h in SUGGESTIONS if text.rstrip("/") in n.lower()]

        lst = self.query_one("#sugg-list", ListView)
        self._rows = [SuggestionRow(n, h) for n, h in matches]
        for row in list(lst.children):
            row.remove()
        for row in self._rows:
            lst.append(row)

        if matches:
            self.display = True
            self._hl = 0
            self._apply_highlight()
        else:
            self.display = False

    def _apply_highlight(self) -> None:
        """Visually mark the highlighted row (we own the state, so the ListView
        never needs focus — the prompt keeps the caret)."""
        for i, row in enumerate(self._rows):
            row.set_class(i == self._hl, "--sugg-hl")

    def move(self, delta: int) -> None:
        n = len(self._rows)
        if n == 0:
            return
        self._hl = max(0, min(n - 1, self._hl + delta))
        self._apply_highlight()

    def selected_name(self) -> str | None:
        if not self._rows or not (0 <= self._hl < len(self._rows)):
            return None
        return self._rows[self._hl]._name

    def visible(self) -> bool:
        return self.display

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """Clicking a suggestion completes it (same as Tab), then returns
        focus to the prompt so a second Enter runs it."""
        item = event.item
        if isinstance(item, SuggestionRow):
            prompt = self.app.query_one("#prompt", Input)
            prompt.value = item._name + " "
            self.refresh_for(item._name)
            prompt.focus()


# ---- 2) Memory browser screen ----------------------------------------------
class MemoryRow(ListItem):
    def __init__(self, index: int, content: str) -> None:
        super().__init__()
        self._index = index
        self._content = content

    def compose(self) -> ComposeResult:
        short = self._content if len(self._content) <= 90 else self._content[:89] + "…"
        t = Text()
        t.append(f"#{self._index + 1:<3d}", style="bold #e3b341")
        t.append(" ")
        t.append(short, style="#d0d0d0")
        yield Static(t, markup=False, classes="mem-row")


class MemoryScreen(ModalScreen):
    """Full overlay that lets you browse & switch long-term memory entries.

    Click a row (or use ↑/↓) to switch the detail pane. Escape closes.
    """

    DEFAULT_CSS = """
    MemoryScreen {
        align: center middle;
    }
    MemoryScreen .mem-box {
        width: 90%; height: 80%;
        background: #0a0a0a;
        border: double #4a4a4a;
        padding: 1 2;
    }
    MemoryScreen .mem-head { text-style: bold; color: #ffffff; margin-bottom: 1; }
    MemoryScreen .mem-hint { color: #6e6e6e; margin-bottom: 1; }
    MemoryScreen ListView { height: 50%; border: solid #2a2a2a; background: #0e0e0e; padding: 0; }
    MemoryScreen ListView > ListItem { height: 1; padding: 0; }
    MemoryScreen ListView > ListItem.--highlight { background: #232323; }
    MemoryScreen .mem-detail {
        margin-top: 1; height: 38%;
        border: solid #2a2a2a; background: #0e0e0e;
        padding: 1 1; color: #cfcfcf;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(self, session_id: str, query: str = "") -> None:
        super().__init__(id="memory-screen")
        self.session_id = session_id
        self.filter_query = query

    def compose(self) -> ComposeResult:
        try:
            mem = LongTermMemory(self.session_id)
            entries = mem.search(self.filter_query, top_k=100) if self.filter_query else mem.recent(100)
        except Exception:  # noqa: BLE001
            entries = []
        self._entries = entries

        title = "MEMORY BROWSER" + (f"  ·  filter: {self.filter_query!r}" if self.filter_query else "")
        with VerticalScroll(classes="mem-box"):
            yield Static(title, classes="mem-head")
            yield Static("click a memory to switch to it · ↑/↓ to browse · Esc to close", classes="mem-hint")
            with ListView(id="mem-list"):
                for i, e in enumerate(entries):
                    yield MemoryRow(i, e.content)
            yield Static(self._detail_text(0) if entries else "", id="mem-detail", classes="mem-detail")

    def _detail_text(self, index: int) -> Text:
        t = Text()
        if not (0 <= index < len(self._entries)):
            t.append("no memories stored yet", style="#6e6e6e")
            return t
        e = self._entries[index]
        head = f"MEMORY #{index + 1}"
        t.append(head + "\n", style="bold #ffffff")
        t.append(f"type={e.memory_type}   importance={e.importance}   source={e.source}\n", style="#e3b341")
        t.append("created: ", style="#6e6e6e")
        try:
            import datetime

            when = datetime.datetime.fromtimestamp(e.created_at).strftime("%Y-%m-%d %H:%M:%S")
            t.append(when + "\n\n", style="#8a8a8a")
        except Exception:  # noqa: BLE001
            t.append("\n\n")
        t.append(e.content, style="#d0d0d0")
        return t

    def on_mount(self) -> None:
        lst = self.query_one("#mem-list", ListView)
        if self._entries:
            lst.index = 0
            self._show(0)
        lst.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:  # click to switch
        item = event.item
        if isinstance(item, MemoryRow):
            self._show(item._index)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:  # arrow keys
        item = event.item
        if isinstance(item, MemoryRow):
            self._show(item._index)

    def _show(self, index: int) -> None:
        self.query_one("#mem-detail", Static).update(self._detail_text(index))


# ---- 3) Session picker screen ----------------------------------------------
class SessionRow(ListItem):
    def __init__(self, session: dict) -> None:
        super().__init__()
        self.session = session

    def compose(self) -> ComposeResult:
        s = self.session
        t = Text()
        t.append(f"{s['id']:<20s}", style="bold #ffffff")
        t.append(f"  {s['entries']:>3d} memories", style="#e3b341")
        t.append(f"   last: {s['last_active']}", style="#6e6e6e")
        yield Static(t, markup=False, classes="sess-row")
        preview = s["preview"]
        if len(preview) > 80:
            preview = preview[:79] + "…"
        yield Static(Text(f"     └─ {preview}", style="#8a8a8a"), markup=False, classes="sess-prev")


class SessionPickerScreen(ModalScreen):
    """Overlay listing every persisted session; Enter/click resumes one."""

    DEFAULT_CSS = """
    SessionPickerScreen { align: center middle; }
    SessionPickerScreen > VerticalScroll {
        width: 90%; height: 80%;
        background: #0a0a0a;
        border: double #4a4a4a;
        padding: 1 2;
    }
    SessionPickerScreen .sess-head { text-style: bold; color: #ffffff; margin-bottom: 1; }
    SessionPickerScreen .sess-hint { color: #6e6e6e; margin-bottom: 1; }
    SessionPickerScreen ListView { height: auto; border: solid #2a2a2a; background: #0e0e0e; padding: 0; }
    SessionPickerScreen ListView > ListItem { height: 2; padding: 0; }
    SessionPickerScreen ListView > ListItem.--highlight { background: #232323; }
    SessionPickerScreen .sess-empty { color: #6e6e6e; margin-top: 1; }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        Binding("enter", "select", "Open session", priority=True),
    ]

    def __init__(self, current_id: str) -> None:
        super().__init__()
        self.current_id = current_id

    def compose(self) -> ComposeResult:
        sessions = list_sessions()
        self._sessions = sessions
        with VerticalScroll():
            yield Static("SESSIONS — pick one to resume", classes="sess-head")
            yield Static("↑/↓ or click to choose · Enter to open · Esc to close", classes="sess-hint")
            if sessions:
                with ListView(id="sess-list"):
                    for s in sessions:
                        yield SessionRow(s)
            else:
                yield Static(
                    "No past sessions found — run a task first and it will be saved here.",
                    classes="sess-empty",
                )

    def on_mount(self) -> None:
        if self._sessions:
            lst = self.query_one("#sess-list", ListView)
            lst.index = 0
            lst.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SessionRow):
            self.dismiss(event.item.session["id"])

    def action_select(self) -> None:
        try:
            lst = self.query_one("#sess-list", ListView)
        except Exception:  # noqa: BLE001
            return
        if lst.index is None or lst.index >= len(lst.children):
            return
        item = lst.children[lst.index]
        if isinstance(item, SessionRow):
            self.dismiss(item.session["id"])


# ---- 4) House mode picker screen -------------------------------------------
class HouseRow(ListItem):
    def __init__(self, house) -> None:
        super().__init__()
        self.house = house

    def compose(self) -> ComposeResult:
        h = self.house
        name = Text()
        name.append(f"{h.glyph} {h.name:<11s}", style=f"bold {h.color}")
        name.append(f"  ·  {h.trait:<8s}", style="#8a8a8a")
        name.append(f"  ·  {h.advantage.upper()}", style="bold #ffffff")
        yield Static(name, markup=False, classes="house-row")
        yield Static(Text(f"     {h.description}", style="#8a8a8a"), markup=False, classes="house-desc")


class HousePickerScreen(ModalScreen):
    """Overlay listing the four house modes; Enter/click switches to one."""

    DEFAULT_CSS = """
    HousePickerScreen { align: center middle; }
    HousePickerScreen > VerticalScroll {
        width: 86; height: 70%;
        background: #0a0a0a;
        border: double #4a4a4a;
        padding: 1 2;
    }
    HousePickerScreen .house-head { text-style: bold; color: #ffffff; margin-bottom: 1; }
    HousePickerScreen .house-hint { color: #6e6e6e; margin-bottom: 1; }
    HousePickerScreen ListView { height: auto; border: solid #2a2a2a; background: #0e0e0e; padding: 0; }
    HousePickerScreen ListView > ListItem { height: 2; padding: 0; }
    HousePickerScreen ListView > ListItem.--highlight { background: #232323; }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        Binding("enter", "select_house", "Switch house", priority=True),
    ]

    def __init__(self, current_key: str) -> None:
        super().__init__()
        self.current_key = current_key

    def compose(self) -> ComposeResult:
        houses = [HOUSES[k] for k in CYCLE_ORDER if k in HOUSES]
        self._houses = houses
        active = HOUSES.get(self.current_key)
        with VerticalScroll():
            yield Static("HOUSE MODES — pick your advantage", classes="house-head")
            hint = "Tab also cycles modes · ↑/↓ or click to choose · Enter to switch · Esc to close"
            if active:
                hint += f"\ncurrent: {active.glyph} {active.name} ({active.advantage})"
            yield Static(hint, classes="house-hint")
            with ListView(id="house-list"):
                for h in houses:
                    yield HouseRow(h)

    def on_mount(self) -> None:
        lst = self.query_one("#house-list", ListView)
        idx = next((i for i, h in enumerate(self._houses) if h.key == self.current_key), 0)
        lst.index = idx
        lst.focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, HouseRow):
            self.dismiss(event.item.house.key)

    def action_select_house(self) -> None:
        try:
            lst = self.query_one("#house-list", ListView)
        except Exception:  # noqa: BLE001
            return
        if lst.index is None or lst.index >= len(lst.children):
            return
        item = lst.children[lst.index]
        if isinstance(item, HouseRow):
            self.dismiss(item.house.key)
