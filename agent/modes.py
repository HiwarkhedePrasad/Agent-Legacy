"""House modes for Agent-Legacy.

Four switchable operating modes named after the Hogwarts houses. Each mode
is a behaviour profile that changes how the agent plans, researches and
recovers — real mechanical advantages wired into the router, factory and
runner, not just a colour swap.

    🦁 GRYFFINDOR   brave  -> speed        (parallel research, fast recovery)
    🦡 HUFFLEPUFF   loyal  -> reliability  (forced QA + extra run retries)
    🦅 RAVENCLAW    wise   -> economy      (forced LLM routing, summarization)
    🐍 SLYTHERIN    cunning-> ambition     (mandatory delegation, big deliverables)

    ⚖  SORTING (neutral, default) -> no advantage until you pick a house.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class HouseConfig:
    """Runtime profile for one house mode."""

    key: str                    # registry key ("gryffindor" | ... | "sorting")
    name: str                   # display name ("Gryffindor")
    glyph: str                  # emoji / sigil shown in the UI
    color: str                  # accent colour (hex) used in the dashboard
    trait: str                  # canonical trait ("brave")
    advantage: str              # short UI copy ("Speed")
    description: str            # one-line advantage explanation
    # --- mechanical levers (consumed by router / factory / runner) ---
    force_llm_routing: bool = False       # Ravenclaw: never trust the cheap heuristic
    force_qa_review: bool = False         # Hufflepuff: deliverables must pass QA
    extra_run_retries: int = 0            # Hufflepuff: keep trying harder
    retry_wait: float | None = None       # Gryffindor: shorter recovery waits
    summarize_context: bool = False       # Ravenclaw: compress long histories
    parallel_research: bool = False       # Gryffindor: fire searches together
    mandatory_delegation: bool = False    # Slytherin: planner must delegate

    def banner(self) -> str:
        return f"{self.glyph} {self.name.upper()}"


HOUSES: dict[str, HouseConfig] = {
    "sorting": HouseConfig(
        key="sorting",
        name="Sorting",
        glyph="⚖",
        color="#9d9d9d",
        trait="neutral",
        advantage="balanced",
        description="No house advantage — the neutral default.",
    ),
    "gryffindor": HouseConfig(
        key="gryffindor",
        name="Gryffindor",
        glyph="🦁",
        color="#ae0001",
        trait="brave",
        advantage="speed",
        description="Brave: parallel research, short recovery waits — the fastest runs.",
        retry_wait=20.0,
        parallel_research=True,
    ),
    "hufflepuff": HouseConfig(
        key="hufflepuff",
        name="Hufflepuff",
        glyph="🦡",
        color="#ecb939",
        trait="loyal",
        advantage="reliability",
        description="Loyal: every deliverable is QA-verified before it ships, with extra retries.",
        force_qa_review=True,
        extra_run_retries=1,
    ),
    "ravenclaw": HouseConfig(
        key="ravenclaw",
        name="Ravenclaw",
        glyph="🦅",
        color="#6f8fd6",
        trait="wise",
        advantage="economy",
        description="Wise: smart model routing + concise outputs — cheapest capable run.",
        force_llm_routing=True,
        summarize_context=True,
    ),
    "slytherin": HouseConfig(
        key="slytherin",
        name="Slytherin",
        glyph="🐍",
        color="#2f9e57",
        trait="cunning",
        advantage="ambition",
        description="Cunning: mandatory delegation to specialists and bigger, cited deliverables.",
        mandatory_delegation=True,
    ),
}

# Tab-cycling order (neutral last).
CYCLE_ORDER: tuple[str, ...] = ("gryffindor", "hufflepuff", "ravenclaw", "slytherin", "sorting")

_DEFAULT = "sorting"
_current: str = _DEFAULT


def set_mode(key: str) -> HouseConfig:
    """Switch the active house mode. Unknown keys fall back to Sorting."""
    global _current
    key = (key or "").strip().lower()
    _current = key if key in HOUSES else _DEFAULT
    return HOUSES[_current]


def get_mode() -> HouseConfig:
    return HOUSES[_current]


def get_mode_key() -> str:
    return _current


def next_mode() -> HouseConfig:
    """Advance to the next house in CYCLE_ORDER (Tab key behaviour)."""
    try:
        idx = CYCLE_ORDER.index(_current)
    except ValueError:
        idx = -1
    return set_mode(CYCLE_ORDER[(idx + 1) % len(CYCLE_ORDER)])
