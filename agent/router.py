"""Multi-LLM router.

Classifies how hard a task is and picks an appropriately sized model:
    SIMPLE  -> fast, cheap model
    MEDIUM  -> mid-tier model
    COMPLEX -> strongest model

Each tier falls back to the global default if it isn't configured, so the
system works even with a single provider/key.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel as PydanticBaseModel, Field

from agent.config import settings


class Tier(str, Enum):
    SIMPLE = "simple"
    MEDIUM = "medium"
    COMPLEX = "complex"


@dataclass(frozen=True)
class ModelSpec:
    tier: Tier
    model: str
    base_url: str
    api_key: str
    temperature: float = 0.3
    timeout: float = 300.0   # per-request cap so a hung model can't freeze a run
    max_retries: int = 2


def _resolve(tier: Tier) -> ModelSpec:
    """Resolve a tier to concrete values, falling back to the global default."""
    base_url, api_key, model, temperature = (
        settings.BASE_URL,
        settings.API_KEY,
        settings.MODEL,
        settings.TEMPERATURE,
    )
    if tier == Tier.SIMPLE:
        model = settings.SIMPLE_MODEL or model
        base_url = settings.SIMPLE_BASE_URL or base_url
        api_key = settings.SIMPLE_API_KEY or api_key
    elif tier == Tier.MEDIUM:
        model = settings.MEDIUM_MODEL or model
        base_url = settings.MEDIUM_BASE_URL or base_url
        api_key = settings.MEDIUM_API_KEY or api_key
    elif tier == Tier.COMPLEX:
        model = settings.COMPLEX_MODEL or model
        base_url = settings.COMPLEX_BASE_URL or base_url
        api_key = settings.COMPLEX_API_KEY or api_key
    return ModelSpec(tier=tier, model=model, base_url=base_url, api_key=api_key, temperature=temperature)


def get_model_spec(tier: Tier | str) -> ModelSpec:
    t = Tier(tier) if isinstance(tier, str) else tier
    return _resolve(t)


def build_chat(spec: ModelSpec) -> ChatOpenAI:
    return ChatOpenAI(
        model=spec.model,
        api_key=spec.api_key or "none",
        base_url=spec.base_url,
        temperature=spec.temperature,
        timeout=spec.timeout,
        max_retries=spec.max_retries,
    )


COMPLEX_HINTS = [
    # strong signals of multi-step work / deliverables only. Generic verbs
    # like "write", "plan", "build" were removed: they routed nearly every
    # task to the strongest tier, defeating the cost-optimization point.
    # Ambiguous tasks land on MEDIUM, where auto mode spends one LLM call.
    "research", "crawl", "compare", "analy", "evaluat", "investigat",
    "report", "recommend", "case study", "audit", "roadmap",
    "deep dive", "feasib", "architecture", "multi-step", "swot",
    # signals that need real web research
    "news", "latest", "current", "today", "recent",
    "article", "find out about", "look up", "who is", "history of",
    # years: any bare year token suggests time-sensitive work
    "2020", "2021", "2022", "2023", "2024", "2025", "2026", "2027", "2028", "2029", "2030",
]
SIMPLE_HINTS = [
    "hello", "hi", "hey", "thanks", "what is 2", "define", "capital of",
    "weather", "time", "date", "spell", "meaning of",
    "joke", "poem", "haiku", "translate",
]

_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _has_hint(low: str, hints: list[str]) -> bool:
    """Hint match. Greetings are matched on word boundaries (so 'hi' doesn't
    fire inside 'machine'); everything else is substring (stem-ish) matching."""
    for hint in hints:
        if hint in ("hi", "hey", "hello"):
            if re.search(rf"\b{hint}\b", low):
                return True
        elif hint in low:
            return True
    return False


def heuristic_tier(text: str) -> Tier:
    low = text.lower()
    has_complex = _has_hint(low, COMPLEX_HINTS) or bool(_YEAR_RE.search(low))
    if len(text) < 140 and not has_complex:
        if _has_hint(low, SIMPLE_HINTS) or len(text) < 60:
            return Tier.SIMPLE
        return Tier.MEDIUM
    if has_complex or len(text) > 400:
        return Tier.COMPLEX
    return Tier.MEDIUM


class _TierSchema(PydanticBaseModel):
    tier: str = Field(description='One of: "simple", "medium", "complex".')
    reason: str = Field(description="One-line reason for the choice.")


async def classify_task(text: str, force_llm: bool = False) -> tuple[Tier, str]:
    """Cost-aware task classification.

    The deterministic heuristic is free, so it always runs first. An LLM call
    is ONLY spent when the heuristic is unsure, i.e. it landed on MEDIUM
    (genuinely ambiguous). Confident SIMPLE/COMPLEX results skip the LLM
    entirely (saves tokens/cost).

    ``force_llm=True`` (Ravenclaw house mode) always asks the classifier LLM
    so routing decisions are deliberate rather than heuristic guesses.
    """
    tier = heuristic_tier(text)
    reason = f"heuristic => {tier.value}"

    mode = settings.MODEL_ROUTING
    if force_llm:
        mode = "llm"
    if mode not in ("llm", "auto") or (mode == "auto" and tier is not Tier.MEDIUM):
        return tier, reason

    try:
        # Use at least a medium-quality model for the routing decision itself —
        # a cheap model consistently under-classifies research-heavy tasks.
        classifier = build_chat(get_model_spec(Tier.MEDIUM))
        clean_model: ChatOpenAI = classifier.with_structured_output(_TierSchema)
        result = await clean_model.ainvoke(
            [
                SystemMessage(
                    "You decide how complex a task is for an AI agent.\n"
                    "Classify as:\n"
                    "- simple: greeting, quick Q&A, single fact, no tools needed.\n"
                    "- medium: a few steps, light analysis or one tool call.\n"
                    "- complex: multi-step work, research/crawling, planning, "
                    "building files, evaluation, or open-ended problems.\n"
                    "Reply with json parsed as the schema."
                ),
                HumanMessage(text),
            ]
        )
        guess = Tier(result.tier.strip().lower())
        tier = guess
        reason = f"llm => {tier.value} ({result.reason})"
    except Exception:  # noqa: BLE001
        pass
    return tier, reason