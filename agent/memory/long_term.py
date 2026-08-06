"""Long-term & short-term memory for the agent.

Short-term memory is the in-context conversation handled by the harness.
Long-term memory is persisted to disk and retrieved via `recall_memory`.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agent.config import settings


@dataclass
class MemoryEntry:
    memory_type: str
    content: str
    importance: int = 3
    created_at: float = field(default_factory=time.time)
    source: str = "auto"

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_type": self.memory_type,
            "content": self.content,
            "importance": self.importance,
            "created_at": self.created_at,
            "source": self.source,
        }


class LongTermMemory:
    """File-backed long-term memory with semantic + keyword retrieval."""

    def __init__(self, session_id: str = "default") -> None:
        self.session_id = re.sub(r"[^\w\-.]", "_", session_id)
        self.path: Path = settings.MEMORY_DIR / f"{self.session_id}.json"
        self.entries: list[MemoryEntry] = self._load()

    def _load(self) -> list[MemoryEntry]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [MemoryEntry(**entry) for entry in raw]
        except (json.JSONDecodeError, TypeError):
            return []

    def _save(self) -> None:
        self.path.write_text(
            json.dumps([e.to_dict() for e in self.entries], indent=2),
            encoding="utf-8",
        )

    def add(
        self,
        content: str,
        memory_type: str = "episodic",
        importance: int = 3,
        source: str = "auto",
    ) -> None:
        # Skip exact duplicates so repeated runs/learnings don't bloat the store.
        content_norm = content.strip()
        for entry in self.entries:
            if entry.memory_type == memory_type and entry.content.strip() == content_norm:
                return
        self.entries.append(
            MemoryEntry(content=content, memory_type=memory_type, importance=importance, source=source)
        )
        self._save()

    def _tokenize(self, text: str) -> set[str]:
        return set(re.findall(r"[a-z0-9_]+", text.lower()))

    def _keyword_score(self, query_tokens: set[str], content: str) -> float:
        content_tokens = self._tokenize(content)
        if not content_tokens:
            return 0.0
        overlap = len(query_tokens & content_tokens)
        return overlap / max(len(query_tokens), 1)

    def search(self, query: str, top_k: int = 5) -> list[MemoryEntry]:
        query_tokens = self._tokenize(query)
        scored = []
        for entry in self.entries:
            keyword = self._keyword_score(query_tokens, entry.content)
            recency = 1.0 / (1.0 + (time.time() - entry.created_at) / 86400.0)
            score = (0.7 * keyword) + (0.2 * recency) + (0.1 * (entry.importance / 10))
            scored.append((score, entry))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [entry for score, entry in scored[:top_k]]

    def recent(self, n: int = 10) -> list[MemoryEntry]:
        return sorted(self.entries, key=lambda e: e.created_at, reverse=True)[:n]

    def to_summary(self, query: str | None = None, top_k: int = 5) -> str:
        entries = self.search(query, top_k) if query else self.recent(top_k)
        if not entries:
            return "No relevant memories found yet."
        lines = []
        for entry in entries:
            lines.append(f"[{entry.memory_type} | importance {entry.importance}] {entry.content}")
        return "\n".join(lines)


def summarize_conversation(user_input: str, final_output: str, artifacts: list[str]) -> str:
    lines = [
        f"TASK: {user_input[:300]}",
        f"RESULT: {final_output[:500]}",
    ]
    if artifacts:
        lines.append("FILES CREATED: " + ", ".join(artifacts))
    return "\n".join(lines)
